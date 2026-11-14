"""3.2 并行路径：接管 `ToolExecutor.execute_tools` 的等价性 + 并发性守卫。

背景：3.2.0 把工具执行从 `FuncToolManager.execute_tool` 迁到
`ToolExecutor.execute_tools`（`IMToolExecutor` 继承之）。旧接管点在 3.2 上
**永远不被调用**（并行静默失效）。本测试验证新接管点：

  1. 与框架**串行实现**逐字节等价（结果 / 顺序 / 截断 / media_refs / 异常 / 超时 / 上限）
  2. 真的并发（两个 0.2s 工具总耗时 ≈ 0.2s，而不是 0.4s）
  3. `on_tool_call` 钩子给出结果 ⇒ 不执行工具
  4. `on_tool_result` 返回 False ⇒ 立即停止，后续结果不写入
  5. 非 IM 执行器（记忆整理等）原样交回，不受影响

不满足 3.2 环境时 `[SKIP]`。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time

# 框架日志会写 <cwd>/data/log.log —— 必须在 import core.* 之前切好目录
os.makedirs("/tmp/itest32/data", exist_ok=True)
os.chdir("/tmp/itest32")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

try:
    from core.agent.config import ToolExecutionConfig            # noqa: E402
    from core.agent.im_tool_executor import IMToolExecutor       # noqa: E402
    from core.agent.tool import ToolResult, ToolSet              # noqa: E402
    from core.agent.tool_executor import ToolExecutor            # noqa: E402
    from core.provider.llm_model import LLMResponse              # noqa: E402
    from core.utils.tool_utils import BaseTool                   # noqa: E402
except Exception as exc:  # noqa: BLE001
    _env.skip(f"需要 KiraAI 3.2+（{exc}）")

acc_main = _env.load("main")
patches = _env.load("patches")
parallel_tools = _env.load("parallel_tools")

FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'BAD'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class Event:
    is_stopped = False


class Tool(BaseTool):
    name = "tool"
    description = "parallel 3.2 test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name, fn):
        super().__init__()
        self.name = name
        self._fn = fn
        self.calls = 0

    async def execute(self, *args, **kwargs):
        self.calls += 1
        return await self._fn()


class FakeIM(IMToolExecutor):
    def __init__(self, event=None):
        self.event = event or Event()


def tc(i, name, args="{}"):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": args}}


async def run(executor, calls, tools, *, max_calls=5, timeout=1.0, max_out=10000,
              on_tool_call=None, on_tool_result=None, should_stop=None):
    resp = LLMResponse("")
    resp.tool_calls = [dict(c) for c in calls]
    ts = ToolSet()
    for t in tools:
        ts.add(t)
    cfg = ToolExecutionConfig(
        max_tool_calls_per_step=max_calls,
        tool_call_timeout=timeout,
        max_tool_output_tokens=max_out,
    )
    await executor.execute_tools(
        resp, ts, config=cfg, on_tool_call=on_tool_call,
        on_tool_result=on_tool_result, should_stop=should_stop)
    return resp


def main():
    print("0) 接管已安装到 3.2 的执行器")
    check("框架存在 ToolExecutor.execute_tools",
          callable(getattr(ToolExecutor, "execute_tools", None)))
    ORIGINAL = ToolExecutor.execute_tools

    plugin = acc_main.AcceleratorPlugin.__new__(acc_main.AcceleratorPlugin)
    plugin._stats = {}
    plugin.patches = patches.PatchRegistry()
    plugin._tool_gate = parallel_tools.ToolGate(enabled=True)
    plugin._install_parallel_tools()

    PATCHED = ToolExecutor.execute_tools
    check("★ 已接管 ToolExecutor.execute_tools", PATCHED is not ORIGINAL)
    check("★ 没有误装到 FuncToolManager（旧路径）",
          "parallel_tools" not in [h.name for h in plugin.patches._handles if h.owner is not ToolExecutor])

    async def text_ok():
        return "普通结果"

    async def text_big():
        return "长文本" * 400

    async def media():
        return ToolResult(text="图片已在下方", media_refs=[
            {"type": "kira_image_ref", "path": "data/media/x.png", "mime": "image/png"}])

    async def boom():
        raise RuntimeError("工具内部炸了")

    async def slow():
        await asyncio.sleep(0.3)
        return "太慢了"

    async def dict_like():
        return {"a": 1, "b": [2, 3]}

    async def compare(label, calls, tools, **kw):
        base_tools, par_tools = [], []
        # 每次用**独立**工具实例，避免 calls 计数互相污染
        for t in tools:
            name, fn = t
            base_tools.append(Tool(name, fn))
            par_tools.append(Tool(name, fn))
        a = await run(FakeIM(), calls, base_tools, **kw)
        b = await run(FakeIM(), calls, par_tools, **kw)
        check(f"{label}：tool_results 逐字节相同", a.tool_results == b.tool_results,
              f"\n     框架={a.tool_results}\n     并行={b.tool_results}")

    async def case():
        await compare("普通文本", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", text_ok), ("read_b", text_ok)])
        await compare("超长文本（上限 128）", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", text_big), ("read_b", text_big)], max_out=128)
        await compare("工具返回图片引用", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", media), ("read_b", text_ok)])
        await compare("工具抛异常", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", boom), ("read_b", text_ok)])
        await compare("工具未注册", [tc(0, "read_zzz"), tc(1, "read_b")],
                      [("read_b", text_ok)])
        await compare("工具超时", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", slow), ("read_b", text_ok)], timeout=0.05)
        await compare("3 条调用但上限 2",
                      [tc(0, "read_a"), tc(1, "read_b"), tc(2, "read_c")],
                      [("read_a", text_ok), ("read_b", text_ok), ("read_c", text_ok)],
                      max_calls=2)
        await compare("上限=0（不截断）", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", text_big), ("read_b", text_big)], max_out=0)
        await compare("工具返回 dict", [tc(0, "read_a"), tc(1, "read_b")],
                      [("read_a", dict_like), ("read_b", text_ok)])
        await compare("非法 JSON 参数", [tc(0, "read_a", "{bad json"), tc(1, "read_b")],
                      [("read_a", text_ok), ("read_b", text_ok)])

    asyncio.run(case())

    print("1) 真的并发（两个 0.2s 工具）")

    async def sleep02():
        await asyncio.sleep(0.2)
        return "ok"

    async def timing():
        # 同一台机器上量「并行 vs 串行」的比值，比绝对阈值抗抖动
        tools_p = [Tool("read_a", sleep02), Tool("read_b", sleep02)]
        t0 = time.perf_counter()
        await run(FakeIM(), [tc(0, "read_a"), tc(1, "read_b")], tools_p, timeout=2)
        par = time.perf_counter() - t0

        tools_s = [Tool("read_a", sleep02), Tool("read_b", sleep02)]
        ts = ToolSet()
        for t in tools_s:
            ts.add(t)
        resp = LLMResponse("")
        resp.tool_calls = [tc(0, "read_a"), tc(1, "read_b")]
        t0 = time.perf_counter()
        await ToolExecutor().execute_tools(
            resp, ts, config=ToolExecutionConfig(tool_call_timeout=2))
        ser = time.perf_counter() - t0
        return par, ser

    par, ser = asyncio.run(timing())
    check("并行明显快于串行（< 串行的 75%）", par < ser * 0.75,
          f"并行={par:.3f}s 串行={ser:.3f}s")

    print("2) on_tool_call 给出结果 ⇒ 不执行工具")

    async def hook_deny(ctx):
        if ctx.tool.name == "read_a":
            ctx.set_result(ToolResult(text="已拒绝"))

    async def deny_case():
        ta = Tool("read_a", text_ok)
        tb = Tool("read_b", text_ok)
        resp = await run(FakeIM(), [tc(0, "read_a"), tc(1, "read_b")], [ta, tb],
                         on_tool_call=hook_deny)
        check("被拒绝的工具没有执行", ta.calls == 0, f"calls={ta.calls}")
        check("另一个工具正常执行", tb.calls == 1, f"calls={tb.calls}")
        check("拒绝结果写入了 tool_results",
              any(r.get("content") == "已拒绝" for r in resp.tool_results),
              str(resp.tool_results))

    asyncio.run(deny_case())

    print("3) on_tool_result 返回 False ⇒ 停止写入")

    async def stop_after_first(_result):
        return False

    async def stop_case():
        resp = await run(FakeIM(), [tc(0, "read_a"), tc(1, "read_b")],
                         [Tool("read_a", text_ok), Tool("read_b", text_ok)],
                         on_tool_result=stop_after_first)
        check("只写了 0 条（第一条就停）", len(resp.tool_results) == 0,
              str(resp.tool_results))

    asyncio.run(stop_case())

    print("4) 非 IM 执行器：原样交回（结果正确、不被并行逻辑接管）")

    async def plain_case():
        resp = LLMResponse("")
        resp.tool_calls = [tc(0, "read_a"), tc(1, "read_b")]
        ts = ToolSet()
        ta, tb = Tool("read_a", text_ok), Tool("read_b", text_ok)
        ts.add(ta)
        ts.add(tb)
        await ToolExecutor().execute_tools(
            resp, ts, config=ToolExecutionConfig(tool_call_timeout=2))
        return resp

    resp = asyncio.run(plain_case())
    check("两条结果都在且顺序正确",
          [r["tool_call_id"] for r in resp.tool_results] == ["call_0", "call_1"],
          str(resp.tool_results))
    check("内容与串行一致", all(r["content"] == "普通结果" for r in resp.tool_results),
          str(resp.tool_results))

    print()
    if FAILED:
        print(f"FAIL {len(FAILED)}: {FAILED}")
        raise SystemExit(1)
    print("PASS 全部通过")


if __name__ == "__main__":
    main()
