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


class SegmentEmitter:
    """把流式增量切成"已成型的 <msg> 段"。

    职责分离：本类只负责**切分**，不做发送（发送是异步的，交给调用方 await）。
    """

    def __init__(self, min_len: int = 1):
        self.min_len = min_len
        self.buf = ""
        self.pending: list[str] = []   # 待发送（已闭合、非空）
        self.emitted: list[str] = []   # 已交给发送方的段
        self.send_failed = False

    def feed(self, delta: str) -> None:
        # ⚠️ 必须**先缓冲**再判断：发送失败后如果直接 return，
        #    这段增量就永远丢失了（既没发出去，也不在 remaining 里 → 用户看不到）。
        #    失败只是"不再抢先发"，内容仍要完整交回框架。
        self.buf += delta
        if self.send_failed:
            return
        while True:
            m = _MSG_CLOSED.search(self.buf)
            if not m:
                break
            seg = m.group(0)
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
