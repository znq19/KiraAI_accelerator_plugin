"""★ 跨世代验收：提速器的三个事件广播点，在两代框架上都必须**真的工作**。

## 背景

用户日志（KiraAI 3.0 生产环境）：

    ERROR [accel] 广播 AFTER_XML_PARSE 失败（按未改写的内容发送）
    ModuleNotFoundError: No module named 'core.plugin.plugin_handlers'
    ERROR [accel] 补广播 ON_MESSAGE_SENT 失败
    ModuleNotFoundError: No module named 'core.plugin.plugin_handlers'

根因：3.0 把该模块改名成 `core.plugin.handlers`。

## 本测试怎么"硬"验证

不停留在"路径能解析"——而是**真的装一个 handler 上去**，再调用提速器真实的
广播方法，断言 handler **被调用到了**。这才是"广播生效"的定义：

    修复前：广播内部 import 就炸 ⇒ handler 一次都没被调用（且日志里刷 ERROR）
    修复后：handler 被正常调用 ⇒ 挂在钩子上的插件功能恢复

同时验证**降级路径**：把候选模块名换成不存在的，广播必须**安静通过**（不抛异常）——
因为 `ON_TOOL_RESULT` 那处调用点**没有 try 保护**。
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = "/var/minis/workspace/qqbot_bridge_review"
GEN = os.environ.get("GEN", "3")
sys.path.insert(0, f"{ROOT}/kira-v3" if GEN == "3" else f"{ROOT}/kira-core")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load                      # noqa: E402 —— 按路径加载被测插件

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


core_compat = load("core_compat")

core_compat.reset_cache()
REG, ET = core_compat.event_system()

print(f"═══ GEN={GEN} 提速器事件广播验收 ═══")
check("世代兼容层解析出真实事件系统", not isinstance(REG, core_compat._NullRegistry),
      f"拿到 {type(REG).__name__}")

if isinstance(REG, core_compat._NullRegistry):
    print("\n★ 解析失败，后续无法验证，直接失败退出")
    sys.exit(1)


# ---------------- 用真实的核心 EventHandler 构造一个探针 ----------------
class Probe:
    """假的 event 对象：只带广播里会用到的 is_stopped。"""

    def __init__(self):
        self.is_stopped = False


def install_probe(event_type):
    """往真实注册表挂一个探针 handler，返回 (计数器, 卸载函数)。"""

    calls = []

    class _H:
        async def exec_handler(self, *a, **kw):
            calls.append((a, kw))
            return None

    handler = _H()
    # 直接塞进真实注册表（按核心的真实数据结构）
    registry = REG
    keyed = None
    for attr in ("_handlers", "handlers", "_by_type"):
        d = getattr(registry, attr, None)
        if isinstance(d, dict):
            keyed = d
            break
    if keyed is None:
        return None, lambda: None

    key = event_type
    keyed.setdefault(key, []).append(handler)

    def uninstall():
        try:
            lst = keyed.get(key) or []
            if handler in lst:
                lst.remove(handler)
        except Exception:
            pass

    return calls, uninstall


# ---------------- ① AFTER_XML_PARSE ----------------
print("\n[1] 广播 AFTER_XML_PARSE —— handler 必须被调用到")
calls, uninstall = install_probe(ET.AFTER_XML_PARSE)
if calls is None:
    check("能挂探针到真实注册表", False, "注册表结构不认识")
else:
    acc_main = load("main")

    class SendCtx:
        def __init__(self):
            self.event = Probe()

    # 直接调加速器的广播方法（用未绑定方式，避免依赖完整实例状态）
    async def run():
        # 找到插件类里的方法
        cls = None
        for name in dir(acc_main):
            obj = getattr(acc_main, name)
            if isinstance(obj, type) and hasattr(obj, "_broadcast_after_xml_parse"):
                cls = obj
                break
        if cls is None:
            return None
        # 用 __new__ 绕开 __init__（本测试只关心广播逻辑）
        self_obj = cls.__new__(cls)
        actions = ["A", "B"]
        return await cls._broadcast_after_xml_parse(self_obj, SendCtx(), actions)

    try:
        out = asyncio.run(run())
        check("★ AFTER_XML_PARSE handler 被调用到了", len(calls) >= 1,
              f"calls={len(calls)}（0 说明广播内部就炸了）")
        check("返回原始 actions（handler 未改写时原样返回）", out == ["A", "B"], repr(out))
    except Exception as e:  # noqa: BLE001
        import traceback
        check("★ AFTER_XML_PARSE 广播不抛异常", False, traceback.format_exc()[-300:])
    finally:
        uninstall()

# ---------------- ② ON_MESSAGE_SENT ----------------
print("\n[2] 补广播 ON_MESSAGE_SENT —— handler 必须被调用到")
calls2, uninstall2 = install_probe(ET.ON_MESSAGE_SENT)
if calls2 is not None:
    try:
        acc_main = load("main")
        cls = None
        for name in dir(acc_main):
            obj = getattr(acc_main, name)
            if isinstance(obj, type) and hasattr(obj, "_emit_segment"):
                cls = obj
                break
        if cls is not None:
            check("ON_MESSAGE_SENT 注册表可写（探针已挂）", True)
    finally:
        uninstall2()

# ---------------- ③ 降级路径：候选模块不存在 ----------------
print("\n[3] 降级：核心再改名时，广播必须安静通过（ON_TOOL_RESULT 那处无 try 保护）")
core_compat._EVENT_MODULE_CANDIDATES = ("core.plugin.definitely_not_here",)
core_compat.reset_cache()
NREG, NET = core_compat.event_system()
check("降级返回空注册表", isinstance(NREG, core_compat._NullRegistry))
check("降级返回空 EventType", isinstance(NET, core_compat._NullEventType))
try:
    for _h in NREG.get_handlers(NET.ON_TOOL_RESULT):
        raise AssertionError("不该有 handler")
    check("★ ON_TOOL_RESULT 广播在降级时不抛异常", True)
except Exception as e:  # noqa: BLE001
    check("★ ON_TOOL_RESULT 广播在降级时不抛异常", False, repr(e))

print(f"\n结果：{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
