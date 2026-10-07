"""★★★ acc 全功能挂载链路：真跑 initialize()，断言每个功能**真的装上**。

## 为什么单列这一个（2026-10-07 教训）

之前验证 3.0 兼容时，我是**逐个手工调 `_install_*`** 去证明"修好了"——
而真实挂载流程（`initialize()`）里是否装上，**没验**。
bridge 那边就因此漏了一个大坑（`_patch_send_path` 被放在 2.x 分支之后，
3.0 提前 return ⇒ 从未调用）；acc 这边虽然结构上没有分支，也必须钉死。

本测试**只走真实入口 `initialize()`**，然后断言接管点清单 ——
且 2.x / 3.0 的清单必须**完全一致**（在运行时审查器里对比）。

与 runtime_audit.py 的区别：那边是逐个手工调 `_install_*`，
这里走**真实入口 `initialize()`**，断言每条功能在真实流程里都到位。

用法: python3 runtime_audit_accel_e2e.py <2|3>
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

GEN = sys.argv[1] if len(sys.argv) > 1 else "3"
ROOT = "/var/minis/workspace/qqbot_bridge_review"
FW = f"{ROOT}/kira-v3" if GEN == "3" else f"{ROOT}/kira-core"
sys.path.insert(0, FW)

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}  {extra}")


print("#" * 74)
print(f"# acc 全功能挂载链路审查  GEN={GEN}")
print("#" * 74)

# ---- 按插件的真实装载方式导入（命名空间包）----
pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ns = types.ModuleType("kira_accelerator")
ns.__path__ = [pkg_dir]
sys.modules["kira_accelerator"] = ns
import importlib                                     # noqa: E402
main_mod = importlib.import_module("kira_accelerator.main")
cc = importlib.import_module("kira_accelerator.core_compat")

ACC = None
for n in dir(main_mod):
    o = getattr(main_mod, n)
    if isinstance(o, type) and hasattr(o, "_install_client_cache"):
        ACC = o
        break
check("找到插件类", ACC is not None)
if ACC is None:
    sys.exit(1)


async def main():
    print("\n[1] 真跑 initialize()（全功能挂载入口）")
    plugin = ACC(ctx=None, cfg={})
    # 需要的最小上下文：插件的 __init__ 若要求参数则补
    try:
        await plugin.initialize()
        check("initialize() 无异常完成", True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        check("initialize() 无异常完成", False, traceback.format_exc()[-500:])
        return

    names = []
    try:
        names = [h.name for h in plugin.patches.handles]
    except Exception:
        try:
            names = [h["name"] for h in plugin.patches.health()]
        except Exception:
            names = []
    print(f"      已装接管点：{names}")

    print("\n[2] 逐功能落点（按配置默认值）")
    check("F1 客户端复用（takeover_build_client）",
          ("reuse_http_client" in names) if plugin.takeover_build_client else True,
          f"配置={plugin.takeover_build_client} 已装={names}")
    if plugin.takeover_build_client:
        check("F1b ★ HTTP 客户端复用真的挂上了", "reuse_http_client" in names,
              str(names))

    check("F2 流式引擎（force_stream/early_send）",
          True if not (plugin.force_stream or plugin.early_send) else bool(names),
          f"force_stream={plugin.force_stream} early_send={plugin.early_send}")

    check("F3 工具闸门（_tool_gate.enabled）",
          True if not plugin._tool_gate.enabled else any("tool" in n or "parallel" in n for n in names),
          f"enabled={plugin._tool_gate.enabled} names={names}")

    check("F4 记忆落盘（takeover_memory_dump）",
          ("memory_dump" in " ".join(names)) if plugin.takeover_memory_dump else True,
          f"配置={plugin.takeover_memory_dump} names={names}")

    print("\n[3] 事件广播三处（3.0 兼容的核心）")
    reg, et = cc.event_system()
    check("F5 AFTER_XML_PARSE 广播可用", getattr(et, "AFTER_XML_PARSE", None) is not None)
    check("F6 ON_MESSAGE_SENT 广播可用", getattr(et, "ON_MESSAGE_SENT", None) is not None)
    check("F7 ON_TOOL_RESULT 广播可用", getattr(et, "ON_TOOL_RESULT", None) is not None)

    # 真跑一次广播，断言 handler 被调用
    calls = []
    keyed = None
    for attr in ("_handlers", "handlers", "_by_type"):
        d = getattr(reg, attr, None)
        if isinstance(d, dict):
            keyed = d
            break
    if keyed is not None:
        class _H:
            async def exec_handler(self, *a, **kw):
                calls.append(1)
        h = _H()
        keyed.setdefault(et.AFTER_XML_PARSE, []).append(h)

        class SendCtx:
            event = type("E", (), {"is_stopped": False})()
        try:
            out = await ACC._broadcast_after_xml_parse(plugin, SendCtx(), ["X"])
            check("F8 ★ 真广播 → handler 被调用到", len(calls) >= 1, f"calls={len(calls)}")
        except Exception as exc:  # noqa: BLE001
            check("F8 ★ 真广播 → handler 被调用到", False, repr(exc))
        finally:
            try:
                keyed.get(et.AFTER_XML_PARSE, []).remove(h)
            except Exception:
                pass

    print("\n[4] 思考注入目标（三个 provider）")
    occ = cc.openai_compatible_client()
    check("F9 OpenAI 兼容客户端可解析", occ is not None)
    check("F9b 有 _build_request_kwargs", occ is not None and hasattr(occ, "_build_request_kwargs"))
    for modname, clsname in [
        ("core.provider.src.deepseek.model_clients", "DeepSeekLLMClient"),
        ("core.provider.src.anthropic.model_clients", "AnthropicCompatibleLLMClient"),
    ]:
        try:
            m = importlib.import_module(modname)
            ok = getattr(m, clsname, None) is not None
        except Exception as exc:  # noqa: BLE001
            ok = False
        check(f"F10 {clsname} 可解析（思考注入落点）", ok)

    await plugin.terminate()
    check("terminate() 无异常（可安全还原）", True)


asyncio.run(main())
print(f"\n{'=' * 74}\n结果：{PASS} passed, {FAIL} failed\n{'=' * 74}")
if FAILS:
    print("失败项：" + " / ".join(FAILS))
sys.exit(1 if FAIL else 0)
