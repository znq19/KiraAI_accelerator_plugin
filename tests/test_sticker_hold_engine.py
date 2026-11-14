"""端到端：真实 StreamEngine + 表情合并段不抢发。

不依赖 KiraAI 运行环境（stub 注入 core.provider.llm_model），走**真实引擎路径**：
feed → pop_pending → emit → 返回完整 LLMResponse。

守卫点：
  1. 合并段（文字+表情同 <msg>）不抢发，且 text_response 完整（下游插件照常改）
  2. 纯表情单条照常抢发（不牺牲速度）
  3. 合并段之前的段照常抢发；之后的段不再抢发（顺序不颠倒）
  4. 已抢发标记里**不包含**被 hold 的段（否则框架会把它剥离掉 ⇒ 丢内容）
  5. hold_sticker=False 时回到旧行为（对照）
"""
from __future__ import annotations

import asyncio
import importlib.util as ilu
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


@dataclass
class _Chunk:
    delta_text: str = ""
    delta_reasoning: str = ""
    tool_calls_delta: list = field(default_factory=list)
    is_final: bool = False
    finish_reason: str = ""
    usage: dict | None = None


class _Request:
    def __init__(self):
        self.messages = []
        self.user_prompt = []
        self.tools = None
        self.tool_set = None


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
_pkg = types.ModuleType("_accelpkg2")
_pkg.__path__ = [str(HERE)]
sys.modules["_accelpkg2"] = _pkg


def _load(name, filename):
    spec = ilu.spec_from_file_location(f"_accelpkg2.{name}", str(HERE / filename))
    mod = ilu.module_from_spec(spec)
    sys.modules[f"_accelpkg2.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("stream_first", "stream_first.py")
_se = _load("stream_engine", "stream_engine.py")
StreamEngine = _se.StreamEngine


class FakeClient:
    def __init__(self, pieces):
        self.pieces = pieces
        self.model = types.SimpleNamespace(
            provider_id="p", provider_name="prov", model_id="m", model_config={})

    async def chat_stream(self, request, **kw):
        for p in self.pieces:
            yield _Chunk(delta_text=p)


async def run(pieces, hold=True):
    sent = []

    async def emit(s):
        sent.append(s)
        return True

    eng = StreamEngine(force_stream=True, emit=emit, hold_sticker=hold)
    resp = await eng.run(FakeClient(pieces), _Request())
    return sent, resp


MERGED = "<msg><text>这表情，欠爱了</text><sticker>1</sticker></msg>"
STICKER_ONLY = "<msg><sticker>1</sticker></msg>"
TEXT = "<msg><text>第一句</text></msg>"
TAIL = "<msg><text>第三句</text></msg>"


def main():
    print("1) 合并段：不抢发，但内容完整交回框架")
    sent, resp = asyncio.run(run([MERGED], hold=True))
    check("一段都没抢发", sent == [], f"实际={sent}")
    check("text_response 完整", resp.text_response == MERGED, repr(resp.text_response))
    check("抢发标记为空", not resp.__dict__.get("_accel_early_sent_segments"),
          str(resp.__dict__.get("_accel_early_sent_segments")))

    print("2) 纯表情单条：照常抢发")
    sent, resp = asyncio.run(run([STICKER_ONLY], hold=True))
    check("抢发 1 段", sent == [STICKER_ONLY], f"实际={sent}")
    check("标记=1", resp.__dict__.get("_accel_early_sent_count") == 1,
          str(resp.__dict__.get("_accel_early_sent_count")))

    print("3) 前缀照发 / 合并段与之后的段不抢发 / 顺序不乱")
    sent, resp = asyncio.run(run([TEXT, MERGED, TAIL], hold=True))
    check("只抢发了前缀", sent == [TEXT], f"实际={sent}")
    check("完整文本包含三段", all(x in resp.text_response for x in (TEXT, MERGED, TAIL)))
    check("标记只含前缀", resp.__dict__.get("_accel_early_sent_segments") == [TEXT],
          str(resp.__dict__.get("_accel_early_sent_segments")))

    print("3b) 同一 chunk 里前缀+合并段+尾巴：只发前缀")
    sent, resp = asyncio.run(run([TEXT + MERGED + TAIL], hold=True))
    check("同一 chunk 也只发前缀", sent == [TEXT], f"实际={sent}")
    check("完整文本仍包含三段", all(x in resp.text_response for x in (TEXT, MERGED, TAIL)))

    print("3c) 合并段在最前 + 尾巴：一条都不发")
    sent, resp = asyncio.run(run([MERGED + TAIL], hold=True))
    check("一条都不发", sent == [], f"实际={sent}")
    check("尾巴没有丢", TAIL in resp.text_response)

    print("4) 一段两个表情：不抢发")
    two = "<msg><sticker>1</sticker><sticker>2</sticker></msg>"
    sent, resp = asyncio.run(run([two], hold=True))
    check("不抢发", sent == [], f"实际={sent}")

    print("5) 对照：hold_sticker=False（旧行为）")
    sent, resp = asyncio.run(run([MERGED], hold=False))
    check("合并段被抢发", sent == [MERGED], f"实际={sent}")
    check("text_response 仍完整", resp.text_response == MERGED)

    print("6) 含表情但只是空白文本：等同纯表情，照发")
    blank = "<msg><text>   </text><sticker>1</sticker></msg>"
    sent, resp = asyncio.run(run([blank], hold=True))
    check("照常抢发", sent == [blank], f"实际={sent}")

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
