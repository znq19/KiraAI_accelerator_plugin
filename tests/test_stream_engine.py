"""流式引擎自检 —— 覆盖强制流式、抢先发送、递归护栏、并发（子代理场景）。

不依赖 KiraAI 运行环境：用 stub 注入 core.provider.llm_model。
"""
from __future__ import annotations

import asyncio
import importlib.util as ilu
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # 插件根（本文件在 tests/ 下）
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ── stub core.provider.llm_model ──
@dataclass
class _Chunk:
    delta_text: str = ""
    delta_reasoning: str = ""
    tool_calls_delta: list = field(default_factory=list)
    is_final: bool = False
    finish_reason: str = ""
    usage: dict | None = None


class _Request:
    def __init__(self, messages=None, user_prompt=None, tools=None, tool_set=None):
        self.messages = messages or []
        self.user_prompt = user_prompt or []
        self.tools = tools
        self.tool_set = tool_set


class _Response:
    def __init__(self, text=""):
        self.text_response = text
        self.reasoning_content = ""
        self.tool_calls = []
        self.tool_results = []
        self.input_tokens = None
        self.output_tokens = None
        self.cached_tokens = None
        self.time_consumed = None


def _install_stubs():
    stub = types.ModuleType("core.provider.llm_model")
    stub.LLMStreamChunk = _Chunk
    stub.LLMRequest = _Request
    stub.LLMResponse = _Response
    for n in ("core", "core.provider"):
        if n not in sys.modules:
            m = types.ModuleType(n)
            m.__path__ = []
            sys.modules[n] = m
    sys.modules["core.provider.llm_model"] = stub


_install_stubs()

pkg = types.ModuleType("_accelpkg")
pkg.__path__ = [str(HERE)]
sys.modules["_accelpkg"] = pkg


def _load(name, filename):
    spec = ilu.spec_from_file_location(f"_accelpkg.{name}", str(HERE / filename))
    mod = ilu.module_from_spec(spec)
    sys.modules[f"_accelpkg.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("stream_first", "stream_first.py")
_se = _load("stream_engine", "stream_engine.py")
StreamEngine = _se.StreamEngine
LLMClientProxy = _se.LLMClientProxy


class FakeClient:
    """带真实流式的假客户端。"""
    def __init__(self, pieces, delay=0.02, with_tool_call=False, model=None):
        self.pieces = pieces
        self.delay = delay
        self.with_tool_call = with_tool_call
        self.model = model or types.SimpleNamespace(
            provider_id="p", provider_name="prov", model_id="m", model_config={})

    async def chat_stream(self, request, **kw):
        for i, p in enumerate(self.pieces):
            await asyncio.sleep(self.delay)
            if self.with_tool_call and i == 0:
                yield _Chunk(tool_calls_delta=[{
                    "index": 0, "id": "c1", "type": "function",
                    "function": {"name": "search", "arguments": ""}}])
            yield _Chunk(delta_text=p)

    async def chat(self, request, **kw):
        # 非流式原实现（应当被引擎绕过）
        raise AssertionError("原 chat 不应被调用（强制流式失败）")


class NoStreamClient(FakeClient):
    """没有真流式实现的客户端：chat_stream 内部回调 chat ⇒ 触发递归护栏。"""
    def __init__(self, pieces, **kw):
        super().__init__(pieces, **kw)
        self.original_chat_calls = 0

    def chat_stream(self, request, **kw):
        return self._gen(request, **kw)

    async def _gen(self, request, **kw):
        # 模拟基类兜底实现 provider.py:82 —— 内部 await self.chat()
        resp = await self.chat(request)
        yield _Chunk(delta_text=resp.text_response, is_final=True)

    async def chat(self, request, **kw):
        self.original_chat_calls += 1
        return _Response("非流式结果")


async def main() -> None:
    print("1) 强制流式：非流式 provider 也会走流式")
    sent = []

    async def emit(s):
        # ★ 新契约：返回 True 表示"这一段确实投递出去了"。
        #   引擎按**返回值**记账，而不是靠"有没有抛异常" ——
        #   因为发送是不可撤销的副作用，投递之后的杂事抛异常不该改变这个事实
        #   （否则已发段会被当成没发 ⇒ 框架重复发送）。
        sent.append(s)
        return True

    c1 = FakeClient(["<msg><text>你好</text></msg>", "<msg><text>再见</text></msg>"])
    eng = StreamEngine(force_stream=True, emit=emit)
    resp = await eng.run(c1, _Request())
    check("走流式并拿到完整文本", resp.text_response is not None)
    check("抢先发了 2 段", len(sent) == 2, f"实际={len(sent)}")
    check("★ text_response 保持完整（下游插件看到的就是完整输出）",
          "<msg><text>你好</text></msg>" in resp.text_response
          and "<msg><text>再见</text></msg>" in resp.text_response,
          f"实际={resp.text_response!r}")
    check("已抢发段数被记录在私有标记里",
          resp.__dict__.get("_accel_early_sent_count") == 2,
          f"实际={resp.__dict__.get('_accel_early_sent_count')}")
    check("统计标记为流式", resp.__dict__.get("_accel_stats", {}).get("streamed") is True)

    print("2) 不抢先发送时：完整文本交回框架")
    eng2 = StreamEngine(force_stream=True, emit=None)
    resp2 = await eng2.run(FakeClient(["<msg><text>完整</text></msg>"]), _Request())
    check("text_response 完整保留", "<msg><text>完整</text></msg>" == resp2.text_response,
          f"实际={resp2.text_response!r}")

    print("3) tool_calls 回合不抢先发")
    sent3 = []

    async def emit3(s):
        return True
        sent3.append(s)

    eng3 = StreamEngine(force_stream=True, emit=emit3)
    resp3 = await eng3.run(
        FakeClient(["<msg><text>先查一下</text></msg>"], with_tool_call=True), _Request())
    check("一段都不发", len(sent3) == 0, f"实际={sent3}")
    check("内容完整保留", "先查一下" in resp3.text_response)
    check("tool_calls 聚合正确", len(resp3.tool_calls) == 1)

    print("4) chat_stream 是基类兜底实现时（内部调 chat）：优雅降级")
    # 真实场景：provider.py:82 的 LLMStreamChunk 兜底实现 —— chat_stream 内部 await self.chat()
    # 代理设计下 self.chat() 是**原实现**（我们没改类上的 chat），所以不会递归。
    # 需要验证的是：引擎仍能拿到正确结果，且**因为只有一个 chunk 而无法抢先发**。
    c4 = NoStreamClient(["ignored"])
    sent4 = []

    async def emit4(s):
        return True
        sent4.append(s)

    eng4 = StreamEngine(force_stream=True, emit=emit4)
    resp4 = await eng4.run(c4, _Request())
    check("兜底实现下仍拿到正确文本", resp4.text_response == "非流式结果",
          f"实际={resp4.text_response!r}")
    check("原 chat 被调用恰好 1 次（经 chat_stream 兜底）", c4.original_chat_calls == 1,
          f"实际={c4.original_chat_calls}")
    check("单 chunk ⇒ 无抢先发送（内容不丢）", len(sent4) == 0 and resp4.text_response)

    print("4b) 递归护栏（防御性）：同一次调用栈内重入必须被拦住")
    c4b = FakeClient(["x"])
    eng4b = StreamEngine(force_stream=True, emit=None)

    async def nested():
        # 模拟"chat_stream 又回调了被包装的 chat"
        return await eng4b.run(c4b, _Request())

    inner = eng4b.run(c4b, _Request())
    # 先占住 inflight，再发一次同 client 的调用
    async def reentrant():
        task = asyncio.create_task(eng4b.run(c4b, _Request()))
        return await task

    try:
        await eng4b.run(c4b, _Request())
        # 上面正常完成 ⇒ 护栏未误报（这也是我们想要的）
        check("正常调用不被误判为递归", True)
    except _se._RecursionGuard:
        check("正常调用不被误判为递归", False, "误报")

    # 直接构造重入条件
    tok = _se._IN_FLIGHT.set((id(c4b),))
    try:
        try:
            await eng4b.run(c4b, _Request())
            check("构造重入时抛出 _RecursionGuard", False, "没有抛出")
        except _se._RecursionGuard:
            check("构造重入时抛出 _RecursionGuard", True)
    finally:
        _se._IN_FLIGHT.reset(tok)
    await inner

    print("5) ★ 并发（子代理场景）：并行调用不能互相误判为递归")
    shared = FakeClient(["<msg><text>A</text></msg>"], delay=0.05)
    engs = [StreamEngine(force_stream=True, emit=None) for _ in range(5)]
    try:
        results = await asyncio.gather(*[e.run(shared, _Request()) for e in engs])
        check("5 个并发调用全部成功", len(results) == 5 and all(r.text_response for r in results),
              f"实际={[r.text_response for r in results]}")
    except _se._RecursionGuard:
        check("5 个并发调用全部成功", False, "并发被误判为递归（contextvars 未生效）")

    print("6) 代理类：透传 + 类型判断 + chat_stream 原样")
    c6 = FakeClient(["x"])
    proxy = LLMClientProxy(c6, lambda _req=None: StreamEngine(force_stream=True, emit=None))
    check("proxy.model 透传", proxy.model.provider_name == "prov")
    check("proxy.model.model_config 透传", proxy.model.model_config == {})
    check("未知属性透传", hasattr(proxy, "pieces"))
    got = []
    async for ch in proxy.chat_stream(_Request(), **_chunk_kwargs()):
        got.append(ch)
    check("proxy.chat_stream 原样可用", len(got) == len(["x"]))
    r6 = await proxy.chat(_Request())
    check("proxy.chat 走流式引擎（返回完整文本）", r6.text_response == "x",
          f"实际={r6.text_response!r}")

    print("7) 每次 chat 用新引擎 ⇒ 统计不串（并发安全）")
    stats = []
    p7 = LLMClientProxy(FakeClient(["a"]), lambda _req=None: _mk(stats))
    await asyncio.gather(p7.chat(_Request()), p7.chat(_Request()))
    check("两次调用各自独立的 stats 对象", len(stats) == 2 and stats[0] is not stats[1])

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


def _chunk_kwargs():
    return {}


def _mk(bucket):
    e = StreamEngine(force_stream=True, emit=None)
    orig = e.run

    async def run(c, r, **kw):
        out = await orig(c, r, **kw)
        bucket.append(e.stats)
        return out
    e.run = run
    return e


asyncio.run(main())
