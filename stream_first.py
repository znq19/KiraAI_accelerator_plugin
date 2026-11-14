"""L4 旁路流式层 —— 这是整个提速器里**唯一真正能让用户"感觉快了"**的部分。

原理（一句话）：
    框架用 `await model.chat(request)` 拿结果（非流式）。
    我们在 chat() 内部改用 chat_stream() 收 SSE，**边收边把已成型的 <msg> 段发出去**，
    收完后再 return 一个"完整的 LLMResponse"，让框架后续流程照常走。

对其它插件透明的设计点（关键）：
    ① 返回**完整** LLMResponse —— 框架的 ON_LLM_RESPONSE / XML 解析 / 记忆写入全照常，
       别的插件看到的 text_response 依然是完整语义（**绝不改写它**）。
    ② "哪些段已经抢先发出去了"只记在响应的私有属性上，**剥离推迟到发送层**做
       （见 main.py 的 `_install_early_sent_strip` 与 early_sent.py 的说明）。
    ③ 由调用方（main.py）补广播 ON_MESSAGE_SENT —— 绕过框架发送层就必须补这个钩子，
       否则 sustained-chat 那类订阅 ON_MESSAGE_SENT 的插件会"瞎掉"。

★ 2026-09-23 删除：原来这里还有一个 `StreamFirstRunner` + `make_chat_impl`，
  它把"已发段剥离"写进了 `text_response`，与 `StreamEngine` 的做法**不一致**，
  而它**从来没有被生产路径使用**（main.py 走的是 StreamEngine）。
  一个行为不同的僵尸副本迟早会被误用（它那套写法正是"下游插件误判"的来源），
  所以直接删掉 —— 只保留 `SegmentEmitter` 和 `_absorb` 这两个真正在用的部件。

风险与边界（诚实标注）：
    - tool_calls 回合不发：流里一旦出现工具调用增量，本回合就不抢先发（这不是最终回复）。
    - 发送失败立刻停手：不再继续抢先发，**所有内容完整交回框架**（不丢字）。
    - 熔断：连续失败 N 次永久旁路（复用 patches.Breaker）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Optional

from core.provider.llm_model import LLMRequest, LLMResponse, LLMStreamChunk

# 匹配一个**已闭合**的 <msg> 段：
#   完整：<msg ...> ... </msg>
#   空  ：<msg/> 或 <msg />
# 非贪婪 + DOTALL ⇒ "流到哪发到哪"：只要有一个完整段出现就立刻可以发。
_MSG_CLOSED = re.compile(r"<msg(?:\s[^>]*)?>.*?</msg>|<msg(?:\s[^>]*)?/>", re.DOTALL)

# 一个完整的 <sticker> 段（自闭合或带内文）
_STICKER_TAG = re.compile(
    r"<sticker(?:\s[^>]*)?(?:/>|>.*?</sticker>)", re.DOTALL | re.IGNORECASE)
# 段最外层的 <msg …> / </msg> 包裹
_MSG_WRAP = re.compile(r"^\s*<msg(?:\s[^>]*)?/?>|</msg>\s*$", re.IGNORECASE)


def sticker_restructure_needed(seg: str) -> bool:
    """这一段是否会被「表情独立成行」类插件在 XML 层**改写结构**。

    为什么抢发要看这个（线上事故，2026-11-14）：
        qq-enhance 的表情控制挂在 ON_LLM_RESPONSE 上改写 XML —— 它只能改
        **还没发出去**的文本。而抢发发生在流式过程中，比它更早：
        模型把 `<text>` 和 `<sticker>` 写在同一个 `<msg>` 里时，抢发会把这个
        **合并形态**直接发出去（QQ 客户端把它渲染成"文字套在表情气泡里"），
        之后插件再拆也晚了 ⇒ 「表情独立成行」静默失效。

    判定（只拦**结构会变**的段，纯表情单条照常抢发，不牺牲速度）：
        · 段内有 >=2 个 <sticker>            ⇒ 插件会拆成多条；
        · 段内有 <sticker> 且还有别的内容     ⇒ 插件会拆成「表情 + 正文」。
    其余情况（单个 <sticker> 独占一段）结构不变，抢发安全。
    """
    if "<sticker" not in (seg or "").lower():
        return False
    body = _MSG_WRAP.sub("", seg or "")
    stickers = _STICKER_TAG.findall(body)
    if not stickers:
        return False
    if len(stickers) > 1:
        return True
    rest = _STICKER_TAG.sub("", body)
    # 空白 <text> 解析后不产生任何元素（和内置标签一致）⇒ 不算"别的内容"
    rest = re.sub(r"<text(?:\s[^>]*)?>\s*</text>", "", rest, flags=re.IGNORECASE)
    return bool(rest.strip())


#: 已知的表情控制插件：plugin_id → 配置开关名（None = 插件启用即生效）
_STICKER_CONTROL_PLUGINS = (
    ("kira-ai-plugin-qq-enhance", "sticker_control_enabled"),
    ("sticker_control", None),
)


def sticker_control_active(plugin_mgr) -> bool | None:
    """表情控制是否真的开着（决定 `hold_sticker` 要不要生效）。

    返回：
        True  —— 有插件会改写表情 XML（qq-enhance 的「启用表情控制」打开，
                 或独立版 sticker_control 插件已启用）
        False —— 明确没开（没装 / 装了但开关是关的）
        None  —— 无法判断（框架没有 plugin_mgr 这类接口）

    为什么要判：`hold_sticker` 的代价是牺牲抢发速度。
    没开表情控制还 hold，就是白等 —— 用户明确要求「只在真的开了表情控制时才拦」。
    """
    if plugin_mgr is None:
        return None
    get_cfg = getattr(plugin_mgr, "get_plugin_config", None)
    if not callable(get_cfg):
        return None
    unknown = False
    try:
        for pid, key in _STICKER_CONTROL_PLUGINS:
            try:
                if key is None:
                    has = getattr(plugin_mgr, "has_plugin", None)
                    enabled = getattr(plugin_mgr, "is_plugin_enabled", None)
                    if callable(has) and callable(enabled) and has(pid) and enabled(pid):
                        return True
                    continue
                cfg = get_cfg(pid) or {}
                # 插件被禁用时，配置里的开关即使还留着 true 也不会生效
                has = getattr(plugin_mgr, "has_plugin", None)
                enabled = getattr(plugin_mgr, "is_plugin_enabled", None)
                if callable(has) and callable(enabled) and has(pid) and not enabled(pid):
                    continue
                if bool(cfg.get(key, False)):
                    return True
            except Exception:  # noqa: BLE001
                # 检测失败 ≠ 没开：交给调用方按"开启"处理（宁可多等，不要功能失灵）
                unknown = True
                continue
    except Exception:  # noqa: BLE001
        return None
    return None if unknown else False


class SegmentEmitter:
    """把流式增量切成"已成型的 <msg> 段"。

    职责分离：本类只负责**切分**，不做发送（发送是异步的，交给调用方 await）。
    """

    def __init__(self, min_len: int = 1, hold_sticker: bool = True):
        self.min_len = min_len
        # 是否启用「结构会被改写 ⇒ 不抢发」（配置项 hold_sticker，默认开）
        self.hold_sticker = hold_sticker
        self.buf = ""
        self.pending: list[str] = []   # 待发送（已闭合、非空）
        self.emitted: list[str] = []   # 已交给发送方的段
        self.send_failed = False
        # 遇到「结构会被下游插件改写」的段后置位：该段及之后**全部交回框架**。
        # 不能只跳过这一段继续抢发后面的 —— 已发段会排到没发段之前，顺序就错了。
        self.hold = False

    def feed(self, delta: str) -> None:
        # ⚠️ 必须**先缓冲**再判断：发送失败后如果直接 return，
        #    这段增量就永远丢失了（既没发出去，也不在 remaining 里 → 用户看不到）。
        #    失败只是"不再抢先发"，内容仍要完整交回框架。
        self.buf += delta
        if self.send_failed or self.hold:
            return
        while True:
            m = _MSG_CLOSED.search(self.buf)
            if not m:
                break
            seg = m.group(0)
            if self.hold_sticker and sticker_restructure_needed(seg):
                # ★ 结构会被 XML 层改写（表情独立成行/随机位置）⇒ 现在发出去就是
                #   **改写前**的形态，已经发出的消息收不回来。整段留给框架：
                #   它在 ON_LLM_RESPONSE 之后再解析，拿到的就是改写后的消息块。
                #   段留在 buf 里不动（remaining() 仍会把它交回框架），顺序不变。
                self.hold = True
                break
            # 从"未发送缓冲"里摘掉（无论后面发不发，都不该再重复出现在 remaining）
            self.buf = self.buf[:m.start()] + self.buf[m.end():]
            if len(seg) < self.min_len or seg.rstrip() in ("<msg/>", "<msg />"):
                self.emitted.append(seg)      # 空消息：算已处理，但不发送
                continue
            self.pending.append(seg)

    def pop_pending(self) -> list[str]:
        out = self.pending
        self.pending = []
        return out

    def mark_failed(self) -> None:
        self.send_failed = True

    def push_back_many(self, segs: list[str]) -> None:
        """按原顺序把多段放回缓冲（一次前置，避免逐段倒序出错）。

        ★ 为什么需要它：`pop_pending()` 会把候选段**一次性弹出**，
          随后逐个尝试发送。若第 i 段失败就 `break`，那么**第 i 段之后的段
          还留在调用方的局部列表里** —— 既不在缓冲、也没发出去 ⇒ **内容丢失**。
          所以退出时必须把"没处理到的"整批放回。
        """
        if segs:
            self.buf = "".join(segs) + self.buf

    def push_back(self, seg: str) -> None:
        """把一段放回「未发送缓冲」。

        ★ 发送失败时必须调用：这一段没投递，要由 `remaining()` 交回框架发送，
          否则它会**静默消失**（既没发出去，也不在交回的文本里）。
        """
        self.buf = seg + self.buf

    def remaining(self) -> str:
        """收流结束后，仍未发送的尾巴（交回框架，走框架原发送流程）。"""
        return self.buf


def _absorb(chunk: LLMStreamChunk, text_parts, reasoning_parts, tool_acc) -> None:
    if chunk.delta_text:
        text_parts.append(chunk.delta_text)
    if chunk.delta_reasoning:
        reasoning_parts.append(chunk.delta_reasoning)
    for frag in chunk.tool_calls_delta or []:
        idx = frag.get("index", 0)
        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if frag.get("id"):
            acc["id"] = frag["id"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            acc["name"] = fn["name"]
        if fn.get("arguments"):
            acc["arguments"] += fn["arguments"]



    """生成 chat() 的替代实现，供 patches.install 使用。"""

    async def impl(self, request: LLMRequest, **kwargs) -> Any:
        runner = runner_factory(self)
        return await runner.run(request, **kwargs)

    return impl
