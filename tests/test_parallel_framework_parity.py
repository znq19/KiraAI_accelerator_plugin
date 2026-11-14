"""★★ 并行路径 vs **真框架** execute_tool —— 漂移守卫

## 为什么需要这个测试

`test_parallel_equiv.py` 的「串行基线」是**手抄的副本**，不是框架本体：
框架改了什么它不会知道。3.1.0（#383）给 execute_tool 加了
`max_tool_output_tokens` 截断、以及 `ToolResult.build_content`（media_refs），
手抄副本没跟上 —— 于是并行路径**静默少做两件事**，而那条测试仍然「全绿」。

本测试直接 import 框架的 `core.agent.func_tool_manager`，拿**真实现**当基线，
逐字节比对 `resp.tool_results`。框架以后再改 execute_tool，这里会立刻红。

## 跳过规则

找不到框架源码时 `[SKIP]`（run_tests.sh 会显示 ⏭，不会伪装成通过）。

## 额外不变量

工具执行只允许写 `resp.tool_results`：不许改 `resp.text_response`、
不许在响应上产生任何 `_accel*` 标记（与抢发/剥离/重复投递路径零交集）。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent      # 插件根
sys.path.insert(0, str(HERE / "tests"))

import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码；设 KIRA_FW=/path/to/KiraAI 后重跑")
sys.path.insert(0, FW)

# 框架的日志 handler 会写 <cwd>/data/log.log；仓库里没有 data/ 时先补上
(HERE / "data").mkdir(exist_ok=True)

# ★ 3.2 起工具执行迁到 `ToolExecutor.execute_tools`（FuncToolManager.execute_tool
#   成了 deprecated 壳），本测试的"真框架基线"已不适用 —— 对应覆盖在
#   tests/test_parallel_32.py（同一套 10 个等价用例 + 并发性/钩子语义）。
try:
    import core.agent.tool_executor  # noqa: F401
    _env.skip("3.2 工具执行已迁到 ToolExecutor；见 tests/test_parallel_32.py")
except ImportError:
    pass

from core.agent.func_tool_manager import FuncToolManager      # noqa: E402
from core.agent.tool import ToolResult, ToolSet               # noqa: E402
from core.provider.llm_model import LLMResponse               # noqa: E402
from core.utils.tool_utils import BaseTool                    # noqa: E402

acc_main = _env.load("main")                                  # noqa: E402
patches = _env.load("patches")                                # noqa: E402
parallel_tools = _env.load("parallel_tools")                  # noqa: E402

FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'BAD'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class Cfg(dict):
    def get_config(self, key, default=None):
        return self.get(key, default)


class Event:
    is_stopped = False

    def stop(self):
        self.is_stopped = True


class Tool(BaseTool):
    name = "tool"
    description = "parity test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name, fn):
        super().__init__()
        self.name = name
        self._fn = fn

    async def execute(self, *args, **kwargs):
        return await self._fn()


# ── 拿「裸实现」：guard() 会在异常时回落原实现，会把差异掩盖掉 ──────────
ORIGINAL = FuncToolManager.execute_tool
patches.guard = lambda name, original, impl: impl

_plugin = acc_main.AcceleratorPlugin.__new__(acc_main.AcceleratorPlugin)
_plugin.kira_config = Cfg()
_plugin._stats = {}
_plugin.patches = patches.PatchRegistry()
_plugin._tool_gate = parallel_tools.ToolGate(enabled=True)
_plugin._install_parallel_tools()

PARALLEL = FuncToolManager.execute_tool
check("并行实现已安装到 FuncToolManager", PARALLEL is not ORIGINAL)

BASE_CFG = {
    "bot_config.agent.max_tool_calls_per_turn": 5,
    "bot_config.agent.tool_call_timeout": 60,
    "bot_config.agent.max_tool_output_tokens": 10000,
}
BIG = "长文本" * 400                                   # 1200 字 / 3600 字节
REF = {"type": "kira_image_ref", "path": "data/media/x.png", "mime": "image/png"}


async def run_case(label, calls, tools, cfg=None, timeout=None, max_calls=5):
    c = dict(BASE_CFG)
    c.update(cfg or {})
    c["bot_config.agent.max_tool_calls_per_turn"] = max_calls
    c["bot_config.agent.tool_call_timeout"] = timeout if timeout is not None else 60
    mgr = FuncToolManager(Cfg(c))

    ts = ToolSet()
    for t in tools:
        ts.add(t)

    a = LLMResponse("")
    a.tool_calls = [dict(x) for x in calls]
    b = LLMResponse("")
    b.tool_calls = [dict(x) for x in calls]

    await ORIGINAL(mgr, Event(), a, tool_set=ts)
    await PARALLEL(mgr, Event(), b, tool_set=ts)

    check(f"{label}：tool_results 逐字节相同", a.tool_results == b.tool_results,
          f"\n     框架={a.tool_results}\n     并行={b.tool_results}")

    # 不变量：工具执行绝不碰回复文本 / 绝不产生抢发标记
    check(f"{label}：未改写 text_response", a.text_response == "" and b.text_response == "")
    leaked = [k for resp in (a, b) for k in resp.__dict__ if k.startswith("_accel")]
    check(f"{label}：未产生抢发标记", not leaked, str(leaked))


def tc(i, name, args="{}"):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": args}}


async def main():
    async def text_ok():
        return "普通结果"

    async def text_big():
        return BIG

    async def media():
        return ToolResult(text="图片已在下方", media_refs=[dict(REF)])

    async def boom():
        raise RuntimeError("工具内部炸了")

    async def slow():
        await asyncio.sleep(0.3)
        return "太慢了"

    async def dict_like():
        return {"a": 1, "b": [2, 3]}

    await run_case("普通文本", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", text_ok), Tool("read_b", text_ok)])
    await run_case("超长文本（上限 128）", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", text_big), Tool("read_b", text_big)],
                   cfg={"bot_config.agent.max_tool_output_tokens": 128})
    await run_case("工具返回图片引用", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", media), Tool("read_b", text_ok)])
    await run_case("工具抛异常", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", boom), Tool("read_b", text_ok)])
    await run_case("工具未注册", [tc(0, "read_zzz"), tc(1, "read_b")],
                   [Tool("read_b", text_ok)])
    await run_case("工具超时", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", slow), Tool("read_b", text_ok)], timeout=0.05)
    await run_case("3 条调用但上限 2",
                   [tc(0, "read_a"), tc(1, "read_b"), tc(2, "read_c")],
                   [Tool("read_a", text_ok), Tool("read_b", text_ok), Tool("read_c", text_ok)],
                   max_calls=2)
    await run_case("上限=0（不截断）", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", text_big), Tool("read_b", text_big)],
                   cfg={"bot_config.agent.max_tool_output_tokens": 0})
    await run_case("工具返回 dict", [tc(0, "read_a"), tc(1, "read_b")],
                   [Tool("read_a", dict_like), Tool("read_b", text_ok)])
    await run_case("非法 JSON 参数", [tc(0, "read_a", "{bad json"), tc(1, "read_b")],
                   [Tool("read_a", text_ok), Tool("read_b", text_ok)])


asyncio.run(main())

print()
if FAILED:
    print(f"FAIL {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("PASS 全部通过")
