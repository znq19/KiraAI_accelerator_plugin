"""统一流式引擎 —— 把任意 provider 的 chat() 变成「流式收集 + 可选抢先发送」。

一个引擎覆盖两个需求（都是用户提的）：
  ① **强制流式**：不管 provider 默认怎么写，一律走 chat_stream() 收 SSE。
     收益：不再有"整段等待"，且流式端点往往首 token 更快。
  ② **抢先发送**（L4）：边收边把已成型的 <msg> 段发出去，用户 1 秒就能看到第一句。

对其它插件透明（三条铁律）：
  - 返回**完整** LLMResponse（ON_LLM_RESPONSE / XML 解析 / 记忆写入全照常）
  - 已发段**不改写 text_response**；剥离在发送层完成（框架不会重复发）
  - 由上层补广播 ON_MESSAGE_SENT

安全设计：
  - **递归护栏**：有些 client 的 chat_stream 基类实现是 `await self.chat()`（provider.py:82）。
    我们包装了 chat()，若直接调 chat_stream 就会无限递归。
    用 task-local 标记 + try/finally 保证一定清理。
  - 出现 tool_calls 增量 ⇒ 本轮不是最终回复 ⇒ 停止抢先发
  - 任何异常 ⇒ 交回原实现（由 patches.guard 兜底）
"""
from __future__ import annotations

import re
import contextvars
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from core.provider.llm_model import LLMRequest, LLMResponse

from .stream_first import SegmentEmitter
from .early_sent import mark_early_sent as _mark_early_sent

# ── 递归护栏 ──
# ⚠️ 必须用 contextvars 而不是全局 set：
#    有些 client 的 chat_stream 基类实现是 `await self.chat()`（provider.py:82）。
#    我们包装了 chat()，若直接调 chat_stream 就会无限递归。需要检测"同一次调用栈内重入"。
#    但**不能用全局集合** —— SubAgent 那类插件会**并发**用同一个 client 发起多个调用
#    （实测 KiraAI-subagent-plugin/main.py:1344），全局集合会把并发的第二个调用
#    误判成递归 ⇒ 流式能力随机失效。contextvars 是 task-local 的，天然正确。
_IN_FLIGHT: contextvars.ContextVar[tuple] = contextvars.ContextVar("accel_inflight", default=())

logger = logging.getLogger("kira_accelerator")


class LLMClientProxy:
    """把 wrapper 暴露成 client 的形状，让插件/框架照常读属性。

    实测插件依赖：`client.model.model_config` / `client.model.provider_id` /
    `client.model.provider_name`（sustained-chat queue_merge.py:136、
    midflight main.py:261、model-fallback main.py:35、subagent main.py 等），
    所以必须把 `.model` 原样透传。
    """

    __slots__ = ("_wrapped", "_engine_factory")

    def __init__(self, wrapped: Any, engine_factory: Callable[[Any], "StreamEngine"]):
        self._wrapped = wrapped
        self._engine_factory = engine_factory

    # ★ 冒充被包裹对象的类型，让 isinstance 认账。
    #   为什么必须有：框架（以及插件）到处用 isinstance 判断客户端种类，实测 20+ 处，例如
    #       core/provider/provider_manager.py:151
    #           if not isinstance(model_client, LLMModelClient):
    #               raise TypeError(f"Expected LLMModelClient, got {type(...).__name__}")
    #   而 get_default_llm / get_default_fast_llm / get_default_vlm 内部都调 get_model_client
    #   —— 正是我们打补丁的地方。不冒充类型 ⇒ 这些全抛 TypeError。
    #   ★ 更坑的是调用方把**任何异常**都报成
    #       "Default LLM model not configured, please configure it in Configuration"
    #     （core/message_manager.py:627 的 except 分支）⇒ 类型错误被伪装成"没配置模型"，
    #     排查方向被彻底带偏。
    #   返回值是 _wrapped.__class__ 而非固定类：父类判定也跟着对，负例仍是 False。
    @property
    def __class__(self):
        return self._wrapped.__class__

    # 插件/框架读属性 → 透传原 client
    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    @property
    def model(self):
        return self._wrapped.model

    def __repr__(self):
        return f"<LLMClientProxy {self._wrapped!r}>"

    # 真正被替换的方法
    async def chat(self, request: LLMRequest, **kwargs) -> LLMResponse:
        # 每次调用一个新引擎 ⇒ 统计不串（并发安全）。
        # ★ 必须把 request 传进工厂：发送上下文（会话 sid 等）挂在它身上，
        #   工厂拿不到就会退回实例变量 ⇒ 并发会话下消息会发串（线上事故）。
        return await self._engine_factory(request).run(self._wrapped, request, **kwargs)

    # chat_stream 原样透传（不要再包一层，避免互相干扰）
    def chat_stream(self, request: LLMRequest, **kwargs):
        return self._wrapped.chat_stream(request, **kwargs)


class StreamEngine:
    """一个引擎实例 = 一次 chat() 调用的执行体。

    :param force_stream: 是否强制走流式（provider 默认非流式时也走）
    :param emit: 可选的"抢先发送"回调（async，接一段 <msg> 字符串）
    """

    def __init__(
        self,
        force_stream: bool = True,
        emit: Optional[Callable[[str], Awaitable[None]]] = None,
        prefer_envelope: bool = False,
        on_complete: Optional[Callable[[Any], None]] = None,
    ):
        self.force_stream = force_stream
        self.emit = emit
        self.prefer_envelope = prefer_envelope
        # 构造完响应后回调（供插件记录"当前响应"，发送层要用它做剥离）
        self.on_complete = on_complete
        self.stats: dict[str, Any] = {}

    async def run(self, client: Any, request: LLMRequest, **kwargs) -> LLMResponse:
        stack = _IN_FLIGHT.get()
        if id(client) in stack:
            # ── 递归护栏命中：这个 client 正在**同一次调用栈**里被我们处理 ──
            # 说明它的 chat_stream 是「基类兜底实现」（内部调 chat），
            # 此时流式路径不可用 ⇒ 交回原实现（调用方会走 _original_chat）。
            # 注意：只在**当前 task 的调用栈**里判重，并发的其它子任务不受影响。
            raise _RecursionGuard()

        # ★★★ 2026-09-28（重复发送加固 —— 从框架 failover 读出来的**真实**路径）：
        #   框架的模型组会对**同一个 request 对象**逐个模型重试
        #   （core/agent/agent_executor.py: `for model_idx, model in enumerate(model_group):`
        #    里 `await model.chat(request)`；失败则 `continue` 换下一个模型）。
        #   而每个模型都是我们包装过的 proxy ⇒ 每次重试都会再跑一遍引擎。
        #   若上一次尝试**流到一半才失败**（网络断开 / 上游 5xx），
        #   我们可能已经抢发出了几段（不可撤销）；重试再抢发一遍
        #   ⇒ **同一段在聊天里出现两次**（本仓库实测复现：前两段各两遍）。
        #   ⇒ `_run_streaming` 在"半途失败且已抢发"时往 request 上打标记，
        #     本函数见到标记 ⇒ **本次不再抢发**。
        #     内容不会丢：重试的完整文本仍会返回，发送层按台账剥离后
        #     框架只补发"尚未发出"的部分（不重不漏）。
        #   ⚠️ 与"多步 loop"的区别（不能一刀切地"失败过就不再抢发"）：
        #     多步 loop 里上一步是**成功返回**的（不设标记）⇒ 照常抢发，
        #     那是设计意图（每一步的文本都是新的）。
        suppressed = False
        try:
            suppressed = bool(request.__dict__.get("_accel_partial_sent"))
        except Exception:  # noqa: BLE001
            suppressed = False

        token = _IN_FLIGHT.set(stack + (id(client),))
        try:
            resp = await self._run_streaming(
                client, request, _suppress_emit=suppressed, **kwargs)
        finally:
            _IN_FLIGHT.reset(token)
        # 本次**成功**返回 ⇒ 清掉"半途失败"标记（下一步/下一轮恢复抢发）
        if suppressed:
            try:
                request.__dict__.pop("_accel_partial_sent", None)
            except Exception:  # noqa: BLE001
                pass
        return resp

    async def _run_streaming(self, client: Any, request: LLMRequest,
                             _suppress_emit: bool = False, **kwargs) -> LLMResponse:
        # 优先用"原生 chat_stream"（有真实流式实现），否则才用 chat_stream 兜底
        stream_fn = getattr(client, "chat_stream", None)
        if stream_fn is None:
            raise _NoStreamSupport()

        kwargs.pop("stream", None)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        saw_tool_call = False

        emitter = SegmentEmitter() if (self.emit is not None and not _suppress_emit) else None
        if _suppress_emit:
            logger.info(
                "[accel] 本请求之前半途失败过（框架正在故障转移重试）⇒ "
                "本次不抢先发送，整段由框架发送（避免同一段重复）")

        async def _guarded(source):
            """★ 半途失败时把"已抢发"的事实钉在 request 上，供故障转移抑制（见 run）。"""
            try:
                async for _c in source:
                    yield _c
            except BaseException:
                if emitter is not None and emitter.emitted:
                    try:
                        request.__dict__["_accel_partial_sent"] = True
                        logger.warning(
                            "[accel] 流式调用半途失败，且已抢先发送 %d 段（不可撤销）"
                            " ⇒ 已标记本请求，重试将不再抢发（避免同一段重复）",
                            len(emitter.emitted))
                    except Exception:  # noqa: BLE001
                        pass
                raise


        started = time.perf_counter()
        first_seg_at: Optional[float] = None
        chunks = 0

        async for chunk in _guarded(stream_fn(request, **kwargs)):
            chunks += 1
            _absorb(chunk, text_parts, reasoning_parts, tool_acc)
            if chunk.tool_calls_delta:
                saw_tool_call = True
            if chunk.usage:
                usage = chunk.usage

            if emitter is None or saw_tool_call or emitter.send_failed:
                continue
            if chunk.delta_text:
                emitter.feed(chunk.delta_text)
                batch = emitter.pop_pending()
                for bi, seg in enumerate(batch):
                    if first_seg_at is None:
                        first_seg_at = time.perf_counter()
                    # ★★ 按**投递返回值**记账（而非"有没有抛异常"）。
                    #   emit 的契约：返回 True = 真的发出去了；False/异常 = 没投递。
                    #   为什么不能用异常判：发送是**不可撤销**的副作用，
                    #   投递成功之后的记账/补广播万一抛异常，就会把"已发出去的段"
                    #   误判成"没发出去" ⇒ 框架剥离少切一段 ⇒ **重复发送**（线上事故）。
                    try:
                        ok = await self.emit(seg)     # type: ignore[misc]
                    except Exception:                 # noqa: BLE001
                        ok = False
                        logger.exception("[accel] 抢先发送回调异常，本段交回框架")
                    if ok is None:
                        # ★★★ 2026-09-28（审计 P0 的**防复发**）：
                        #   `None` 几乎只有一个来路 —— 回调**忘了 return**。
                        #   （历史事故：`_make_engine` 的 `_emit` 写成了
                        #    `await self._emit_segment(...)` 却没 return ⇒
                        #    每次抢发都被判"没投递" ⇒ 整轮只剩首段 + 响应标记全空，
                        #    而 `None` 既不抛异常、又与"真没投递"无法区分 ⇒ 完全静默。）
                        #   行为保持不变（保守当"没投递"），但**留下线索**，
                        #   让同类断链下次能一眼看见 —— 不再有无声的灾难。
                        #   守卫：tests/test_production_emit_contract.py
                        logger.warning(
                            "[accel] 抢发回调返回 None（疑似忘了 return 投递结果）"
                            " —— 本段按未投递处理，请检查 emit 回调实现")
                    if ok is False or ok is None:
                        # ★ 没投递 ⇒ 这一段**以及后面还没处理的**全部放回缓冲，
                        #   稍后由 remaining() 交回框架发送。
                        #   ⚠️ 不能只放回当前这一段就 break —— 后面那些已经被
                        #   pop_pending 弹出、既不在缓冲也没发出去，会**静默丢内容**。
                        emitter.push_back_many(batch[bi:])
                        emitter.mark_failed()
                        break
                    emitter.emitted.append(seg)

        resp = LLMResponse("".join(text_parts))
        resp.reasoning_content = "".join(reasoning_parts)
        for idx in sorted(tool_acc):
            a = tool_acc[idx]
            resp.tool_calls.append({
                "id": a["id"],
                "type": "function",
                "function": {"name": a["name"], "arguments": a["arguments"]},
            })
        if usage:
            resp.input_tokens = usage.get("input_tokens")
            resp.output_tokens = usage.get("output_tokens")
            resp.cached_tokens = usage.get("cached_tokens")
        resp.time_consumed = round(time.perf_counter() - started, 2)

        # ★ 兼容性关键：**不改写 resp.text_response**
        #   实测框架内置 kira-ai 插件与 sustained-chat 插件都会在 ON_LLM_RESPONSE 里
        #   把它当作"模型的完整输出"来用（XML 校验 / 判断 AI 是否说话）。
        #   我们只打一个标记，"剥离已发段"推迟到真正发送时做（main.py 的发送层）。
        early = len(emitter.emitted) if emitter else 0
        if early:
            _mark_early_sent(resp, emitter.emitted, resp.text_response)

        # ★★★ 工具调用轮次：**先出文本、后出 tool_calls** 的情形。
        #
        #   问题：文本已经被抢发出去（不可撤回），但紧接着出现了 tool_calls
        #   ⇒ 本轮变成"工具轮"，框架**不会发送**这段文本
        #   ⇒ ① 这段文本对框架来说"没发过"，却已经出现在聊天里；
        #      ② 它仍留在 resp.text_response，进入下一轮上下文
        #   ⇒ 模型下一轮照着上下文复述 ⇒ **用户看到复读**；
        #      工具链也可能因为"文本没被当回事"而错乱。
        #
        #   修法：把**已抢发的段**如实记进 resp，并显式标注"本轮是工具轮"
        #   ⇒ 发送层据此剥离，不会把已发内容再发一次，也不留孤儿文本。
        # ★★ 诊断：工具轮里**已发出的文本含 <invoke> 之类标签**时，把它记下来。
        #   为什么要记：xml_tag_fixer 在 ON_LLM_RESPONSE 里会把**未注册的标签**
        #   当散落文本转义（`<invoke>` → `&lt;invoke&gt;`）。若这段文本恰好
        #   已被抢先发出，用户就会看到一行被转义的乱码。
        #   这条日志让"到底谁转义的"一眼可查（我们只 unescape，从不 escape）。
        if saw_tool_call and emitter is not None and emitter.emitted:
            _joined = "".join(emitter.emitted)
            if re.search(r"<\s*(invoke|tool_calls|parameter)\b", _joined):
                logger.warning(
                    "[accel] 工具轮内抢发内容含工具标签（可能在 ON_LLM_RESPONSE 被"
                    "下游插件转义）—— 已标记并交回框架；若用户看到 &lt;invoke&gt; "
                    "形态的乱码，请检查 xml_tag_fixer 的标签注册表是否含 invoke")
        if saw_tool_call and emitter is not None and emitter.emitted:
            _mark_early_sent(resp, emitter.emitted, resp.text_response)
            resp.__dict__["_accel_tool_turn_early"] = True
            # ★ 2026-10-05 降级为 debug：文本先于 tool_calls 到达是**正常且已被
            #   妥善处理**的情形（已标记、防重复），工具多的会话每轮都打 warning
            #   纯属噪音。
            logger.debug(
                "[accel] 工具轮内已抢先发送 %d 段（文本先于 tool_calls 到达）"
                "—— 已标记，避免重复发送与上下文残留", len(emitter.emitted))

        stats = {
            "chunks": chunks,
            "streamed": True,
            "early_sent": early,
            "first_seg_s": round(first_seg_at - started, 3) if first_seg_at else None,
            "elapsed_s": resp.time_consumed,
        }
        resp.__dict__["_accel_stats"] = stats

        # ★★ 把"哪些段已抢先发出"写进响应 —— **发送层的主判据**。
        #   踩过的坑（用户实测："响应标记缺失，改用本轮台账剥离"每句都报）：
        #     mark_early_sent() 写在 early_sent.py 里，但**从来没有任何地方调用它**，
        #     ⇒ 标记永远是 0 ⇒ 每次剥离都走"标记缺失 → 台账兜底"这条告警路径。
        #     结果两条：① 日志刷屏；② 主路径（标记优先）根本没被跑过。
        #   所以在这里直接写，不再依赖一个没人调用的辅助函数。
        #   ⚠️ 这里以前写的是 `full_text` —— 那个变量在本函数里**不存在**
        #      ⇒ 每次抢发都抛 NameError ⇒ 标记写不进去 ⇒ 发送层只能走台账兜底
        #      并逐条告警（用户实测："响应标记缺失…"每句都发，刷屏）。
        #      正确做法：用 resp.text_response（它就是模型的完整输出，我们从不改写它）。
        _segs = list(emitter.emitted) if emitter else []
        if _segs:
            resp.__dict__["_accel_early_sent_count"] = len(_segs)
            resp.__dict__["_accel_early_sent_segments"] = _segs
            resp.__dict__["_accel_full_text"] = resp.text_response
        self.stats = stats
        if self.on_complete is not None:
            try:
                self.on_complete(resp)
            except Exception:  # noqa: BLE001
                pass
        return resp


class _RecursionGuard(Exception):
    """chat_stream 会回调 chat ⇒ 不能再走流式，交回原实现。"""


class _NoStreamSupport(Exception):
    """该 client 没有 chat_stream。"""


def _absorb(chunk, text_parts, reasoning_parts, tool_acc) -> None:
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


def make_chat_wrapper(engine_factory: Callable[[Any], StreamEngine],
                      skip: Callable[[Any], bool] = lambda c: False):
    """构造一个 chat() 替代实现（供 patches.install 使用）。

    会先尝试流式；流式不可用（递归 / 无实现）时**回落原实现**。
    """

    async def wrapper(self, request: LLMRequest, **kwargs) -> Any:
        original = wrapper.__kira_accel_original__
        if skip(self):
            return await original(self, request, **kwargs)
        engine = engine_factory(self)
        try:
            return await engine.run(self, request, **kwargs)
        except (_RecursionGuard, _NoStreamSupport):
            # 该 provider 的流式路径不可用 ⇒ 老老实实走原实现
            return await original(self, request, **kwargs)

    return wrapper
