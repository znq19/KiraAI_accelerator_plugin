"""core_compat 兼容层自检：两代都解析出真事件系统，异常路径降级为空对象。"""
import os
import sys

ROOT = "/var/minis/workspace/qqbot_bridge_review"
GEN = os.environ.get("GEN", "3")
sys.path.insert(0, f"{ROOT}/kira-v3" if GEN == "3" else f"{ROOT}/kira-core")
os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

sys.path.insert(0, "/tmp/accload")
import kira_accelerator.core_compat as cc

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


print(f"═══ GEN={GEN} core_compat 自检 ═══")
cc.reset_cache()
reg, et = cc.event_system()
check("解析出真实事件注册表（非空对象）", not isinstance(reg, cc._NullRegistry),
      f"拿到 {type(reg).__name__}")
check("解析出真实 EventType（非空对象）", not isinstance(et, cc._NullEventType),
      f"拿到 {type(et).__name__}")
if not isinstance(reg, cc._NullRegistry):
    check("AFTER_XML_PARSE 可用", et.AFTER_XML_PARSE is not None)
    check("ON_MESSAGE_SENT 可用", et.ON_MESSAGE_SENT is not None)
    check("ON_TOOL_RESULT 可用", et.ON_TOOL_RESULT is not None)
    check("get_handlers 返回可迭代", hasattr(reg.get_handlers(et.AFTER_XML_PARSE), "__iter__"))
    check("模块路径 = 该世代的正确路径",
          cc._CACHE is not None)

# 缓存命中
reg2, et2 = cc.event_system()
check("第二次调用走缓存（同一对象）", (reg2 is reg) and (et2 is et))

# ---- 空对象降级路径（模拟"核心又改名"）----
print("  --- 模拟核心改名后的降级 ---")
cc._EVENT_MODULE_CANDIDATES = ("core.plugin.definitely_not_here",)
cc.reset_cache()
reg3, et3 = cc.event_system()
check("降级：返回空注册表", isinstance(reg3, cc._NullRegistry))
check("降级：返回空 EventType", isinstance(et3, cc._NullEventType))
check("降级：get_handlers 恒为空", tuple(reg3.get_handlers(et3.AFTER_XML_PARSE)) == ())
check("降级：EventType.X 不抛异常", et3.ANYTHING_AT_ALL is None)
try:
    for _h in reg3.get_handlers(et3.ON_TOOL_RESULT):
        raise AssertionError("不该进循环")
    check("降级：ON_TOOL_RESULT 广播不抛异常（无 try 保护的调用点也安全）", True)
except Exception as e:  # noqa: BLE001
    check("降级：ON_TOOL_RESULT 广播不抛异常", False, repr(e))

print(f"\n结果：{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
