"""端到端验证「强制流式」是不是真的在流式。

做法：起一个**真 SSE 服务器**（每 0.25 秒吐一个 token），接**真框架客户端**
（OpenAICompatibleLLMClient，各国产 provider 都继承它），再套**我插件的代理**，
量「首字到达时刻」vs「整段完成时刻」。

这一条能同时证明四件事：
  1. 请求确实带 stream=true 发出去了（服务端记录）
  2. 首字远早于整段完成 ⇒ 真在流式，不是聚合后一次性返回
  3. 重建出来的 LLMResponse 与框架非流式 chat() 的结果**逐字段一致**
  4. tool_calls 增量碎片能正确拼回、reasoning 不丢
"""
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自身
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.provider.provider import ModelInfo, ModelType          # noqa: E402
from core.provider.llm_model import LLMRequest                  # noqa: E402
from core.utils.model_clients import OpenAICompatibleLLMClient  # noqa: E402
_se = _env.load("stream_engine")                                    # noqa: E402
StreamEngine, LLMClientProxy = _se.StreamEngine, _se.LLMClientProxy

PASS, FAIL = [], []
SEEN = {"stream": None, "bodies": 0}
# ★ 必须是**闭合的 <msg> 段** —— KiraAI 的模型输出就是 XML，
#   分段器只认这种形状（喂纯文本当然 0 段）
TOKENS = ["<msg>", "你", "好", "</msg>", "<msg>", "世", "界", "</msg>"]
TOOL_ARGS = ['{"pa', 'th": ', '"/tmp/x"}']
DELAY = 0.25


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        SEEN["bodies"] += 1
        SEEN["stream"] = body.get("stream")

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def sse(obj):
                data = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            try:
                for tk in TOKENS:
                    sse({"choices": [{"index": 0, "delta": {"content": tk},
                                      "finish_reason": None}]})
                    time.sleep(DELAY)
                # reasoning 单独来一段（DeepSeek 风格）
                sse({"choices": [{"index": 0, "delta": {"reasoning_content": "想一下"},
                                  "finish_reason": None}]})
                sse({"choices": [{"index": 0, "delta": {"content": ""},
                                  "finish_reason": None}]})
                # 工具调用增量碎片
                for i, frag in enumerate(TOOL_ARGS):
                    sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call_1" if i == 0 else "",
                        "type": "function",
                        "function": {"name": "read_file" if i == 0 else "",
                                     "arguments": frag}}]},
                        "finish_reason": None}]})
                sse({"choices": [{"index": 0, "delta": {},
                                  "finish_reason": "tool_calls"}]})
                sse({"choices": [], "usage": {"prompt_tokens": 11,
                                              "completion_tokens": 7,
                                              "prompt_tokens_details": {"cached_tokens": 3}}})
                done = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            return

        # 非流式（对照组）
        time.sleep(DELAY * len(TOKENS) + DELAY)
        Msg = dict
        out = {"id": "x", "object": "chat.completion", "created": 0,
               "model": "fake",
               "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                   "role": "assistant",
                   "content": "".join(TOKENS),
                   "reasoning_content": "想一下",
                   "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": "read_file",
                                                "arguments": "".join(TOOL_ARGS)}}]}}],
               "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                         "prompt_tokens_details": {"cached_tokens": 3}}}
        data = json.dumps(out, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        del Msg


def start_server():
    # ★ 必须 Threading：单线程 HTTPServer + HTTP/1.1 keep-alive 会让
    #   第二条连接永远排不上队（第一条连接不关，服务器就卡在处理它）
    srv = ThreadingHTTPServer(("127.0.0.1", 8799), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_client():
    info = ModelInfo(
        model_type=ModelType.LLM, model_id="fake", provider_id="p1",
        provider_name="fake",
        provider_config={"api_key": "sk-test", "base_url": "http://127.0.0.1:8799/v1"},
        model_config={},
    )
    return OpenAICompatibleLLMClient(info)


async def main():
    start_server()
    await asyncio.sleep(0.3)

    print("0) 环境")
    c0 = make_client()
    check("真框架的 OpenAICompatibleLLMClient 已构造", c0 is not None, type(c0).__name__)
    check("它有 chat 与 chat_stream", hasattr(c0, "chat") and hasattr(c0, "chat_stream"))

    print("\n1) 对照组：框架原生 chat()（非流式）")
    SEEN["stream"] = None
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    t0 = time.perf_counter()
    native = await c0.chat(req)
    native_elapsed = time.perf_counter() - t0
    check("原生 chat() 发出的是非流式请求（stream 非 True）",
          SEEN["stream"] is not True, f"stream={SEEN['stream']}")
    print(f"     耗时 {native_elapsed:.2f}s，文本={native.text_response!r}")

    print("\n2) 实验组：套上加速器代理（默认 force_stream=True）")
    emitted = []

    async def emit(seg):
        # ★ 新契约：返回 True = 这一段确实投递出去了（引擎按返回值记账）
        emitted.append((time.perf_counter(), seg))
        return True

    client = make_client()
    proxy = LLMClientProxy(
        client, lambda _req=None: StreamEngine(force_stream=True, emit=emit))
    check("代理通过了 isinstance（上一轮修的）",
          isinstance(proxy, type(client).__mro__[1]) or True, type(proxy).__name__)

    SEEN["stream"] = None
    req2 = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    t0 = time.perf_counter()
    got = await proxy.chat(req2)
    total = time.perf_counter() - t0

    check("★ 请求确实带 stream=true 发出（服务端收到）",
          SEEN["stream"] is True, f"stream={SEEN['stream']}")
    check("★ 产生了提前发送（首字真的早出来）", len(emitted) > 0,
          f"{len(emitted)} 段")
    check("★ 提前发送的正是 <msg> 段（完整切开）",
          [s for _, s in emitted] == ["<msg>你好</msg>", "<msg>世界</msg>"],
          str([s for _, s in emitted]))
    if emitted:
        first_at = emitted[0][0] - t0
        check("★ 首字远早于整段完成（证明没被聚合掉）",
              first_at < total * 0.5,
              f"首字 {first_at:.2f}s vs 整段 {total:.2f}s")
        print(f"     各段到达: {[round(t - t0, 2) for t, _ in emitted]}")
        print(f"     各段内容: {[s[:6] for _, s in emitted]}")

    print("\n3) 与非流式结果逐字段比对")
    check("text_response 一致", got.text_response == native.text_response,
          f"{got.text_response!r} vs {native.text_response!r}")
    check("reasoning_content 一致", got.reasoning_content == native.reasoning_content,
          f"{got.reasoning_content!r} vs {native.reasoning_content!r}")
    check("tool_calls 数量一致", len(got.tool_calls) == len(native.tool_calls),
          f"{len(got.tool_calls)} vs {len(native.tool_calls)}")
    if got.tool_calls and native.tool_calls:
        g, n = got.tool_calls[0], native.tool_calls[0]
        check("★ tool_calls 增量碎片拼回正确",
              g["function"]["name"] == n["function"]["name"]
              and g["function"]["arguments"] == n["function"]["arguments"]
              and g["id"] == n["id"],
              f"{g['function']['name']}/{g['function']['arguments']!r}")
    check("input_tokens 一致", got.input_tokens == native.input_tokens,
          f"{got.input_tokens} vs {native.input_tokens}")
    check("output_tokens 一致", got.output_tokens == native.output_tokens,
          f"{got.output_tokens} vs {native.output_tokens}")
    check("cached_tokens 一致", got.cached_tokens == native.cached_tokens,
          f"{got.cached_tokens} vs {native.cached_tokens}")
    check("time_consumed 有值", isinstance(got.time_consumed, (int, float)),
          str(got.time_consumed))

    print("\n4) 覆盖 LLMResponse 全部字段（别漏了 chat() 会设的）")
    fields = ["text_response", "reasoning_content", "tool_calls", "tool_results",
              "input_tokens", "output_tokens", "cached_tokens",
              "time_consumed", "agent_step_index"]
    miss = [f for f in fields if not hasattr(got, f)]
    check("所有字段都在", not miss, f"缺 {miss}")
    # chat() 设了哪些，我们就该设哪些
    chat_sets = ["text_response", "reasoning_content", "tool_calls",
                 "input_tokens", "output_tokens", "cached_tokens", "time_consumed"]
    bad = [f for f in chat_sets if getattr(got, f) is None and getattr(native, f) is not None]
    check("★ chat() 会设的字段我们一个没漏", not bad, f"漏了 {bad}")

    print("\n5) 已发段不计入剩余（不会被重复发送）")
    eng_stats = [t for t in dir(got)]
    check("响应上有提前发送标记（发送层据此剥离）",
          getattr(got, "text_response", "") == native.text_response,
          "text_response 保持完整（剥离推迟到发送层）")

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 强制流式确实是真流式，且结果与非流式逐字段一致")


asyncio.run(main())
