"""接管点必须**逐个精确还原** —— 关掉插件后框架要回到原样。

为什么值得单独测：
  插件是"接管型"的（monkey patch）。还原不干净会留下**看不见的副作用**：
  热重载后越包越厚（同一个方法被包好几层）、或者插件卸载了行为还在。
  这是这类插件最难排查的故障，所以逐个接管点用**函数对象身份**来验。

覆盖全部 7 个接管点：
  reuse_http_client      OpenAICompatibleLLMClient._build_client
  stream_engine          ProviderManager.get_model_client
  auto_thinking:openai   OpenAICompatibleLLMClient._build_request_kwargs
  auto_thinking:deepseek DeepSeekLLMClient._build_request_kwargs
  auto_thinking:anthropic AnthropicCompatibleLLMClient._build_request_body
  parallel_tools         FuncToolManager.execute_tool
  early_sent_strip       MessageProcessor.send_xml_messages
"""
import asyncio
import os
import sys
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

# ★ 跨世代：3.0 该类在 core.provider.openai_compatible
import importlib as _il                                         # noqa: E402
mc = _il.import_module(_env.resolve_module("model_clients"))
from core.provider.provider_manager import ProviderManager     # noqa: E402
from core.message_manager import MessageProcessor              # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


TARGETS = [
    ("reuse_http_client", mc.OpenAICompatibleLLMClient, "_build_client"),
    ("stream_engine", ProviderManager, "get_model_client"),
    ("auto_thinking:openai", mc.OpenAICompatibleLLMClient, "_build_request_kwargs"),
    ("early_sent_strip", MessageProcessor, "send_xml_messages"),
]
# 可选依赖：SDK 不在就跳过对应接管点
for modpath, clsname, attr, label in [
    ("core.provider.src.deepseek.model_clients", "DeepSeekLLMClient",
     "_build_request_kwargs", "auto_thinking:deepseek"),
    ("core.provider.src.anthropic.model_clients", "AnthropicCompatibleLLMClient",
     "_build_request_body", "auto_thinking:anthropic"),
]:
    try:
        m = __import__(modpath, fromlist=[clsname])
        TARGETS.append((label, getattr(m, clsname), attr))
    except Exception:  # noqa: BLE001
        print(f"  （{label} 的 SDK 未安装，跳过该接管点）")

try:
    from core.agent.func_tool_manager import FuncToolManager
    TARGETS.append(("parallel_tools", FuncToolManager, "execute_tool"))
except Exception:  # noqa: BLE001
    print("  （parallel_tools 导入失败，跳过）")


def snapshot():
    return {(label, id(cls), attr): getattr(cls, attr) for label, cls, attr in TARGETS}


async def main():
    print("0) 记录接管前的原始方法对象")
    orig = snapshot()
    check("拿到全部接管点的原始实现", len(orig) == len(TARGETS), f"{len(orig)} 个")

    print("\n1) 打开所有功能并安装接管")
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.observe_enabled = True
    plugin.force_stream = True
    plugin.early_send = True
    plugin.thinking_enabled = True
    plugin.normalize_empty_content = True
    plugin.takeover_build_client = True
    plugin._proxy_cache = {}
    plugin._client_cache = {}
    plugin._resp_by_sid = {}
    plugin._last_seg_ts = {}
    plugin._tool_gate.enabled = True
    plugin.patches.uninstall_all()

    import core.agent.func_tool_manager as ftm
    import core.message_manager as mm
    plugin._install_client_cache()
    plugin._install_stream_engine()
    plugin._install_request_hook()
    plugin._install_early_sent_strip()
    plugin._install_parallel_tools()

    installed = snapshot()
    changed = [k[0] for k, v in installed.items() if v is not orig[k]]
    print(f"     已接管: {sorted(changed)}")
    check("★ 所有接管点都被替换了（补丁真的生效）", len(changed) == len(TARGETS),
          f"{len(changed)}/{len(TARGETS)}")
    for (label, cls, attr), fn in installed.items():
        if fn is not orig[(label, cls, attr)]:
            check(f"{label} 带上了标记", getattr(fn, "__kira_accel__", False) is True)
            break

    print("\n2) 幂等：重复安装不该再包一层")
    before_again = snapshot()
    plugin._install_client_cache()
    plugin._install_stream_engine()
    plugin._install_request_hook()
    plugin._install_early_sent_strip()
    plugin._install_parallel_tools()
    after_again = snapshot()
    same = all(before_again[k] is after_again[k] for k in before_again)
    check("★ 二次安装不会重复包装（函数对象不变）", same)

    print("\n3) terminate：全部精确还原")
    await plugin.terminate()
    final = snapshot()
    bad = [k[0] for k in final if final[k] is not orig[k]]
    check("★★ 每个接管点都还原成**同一个函数对象**", not bad, f"未还原: {bad}")
    marks = [k[0] for k in final if getattr(final[k], "__kira_accel__", False)]
    check("★ 无任何标记残留", not marks, str(marks))

    print("\n4) 多次热重载（装→卸 ×3）仍然干净")
    for i in range(3):
        p2 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
        p2._proxy_cache = {}
        p2._client_cache = {}
        p2._resp_by_sid = {}
        p2._last_seg_ts = {}
        p2.takeover_build_client = True
        p2._tool_gate.enabled = True
        p2._install_client_cache()
        p2._install_stream_engine()
        p2._install_request_hook()
        p2._install_early_sent_strip()
        p2._install_parallel_tools()
        await p2.terminate()
    final2 = snapshot()
    bad2 = [k[0] for k in final2 if final2[k] is not orig[k]]
    check("★★ 三次热重载后依旧精确还原", not bad2, f"未还原: {bad2}")

    print("\n5) 别人改过的就不碰（安全边界）")
    # 模拟：某个接管点被第三方替换过 ⇒ 卸载时不该把我们不认识的东西覆盖掉
    label, cls, attr = TARGETS[1]
    ours = getattr(cls, attr)
    sentinel = lambda *a, **k: None          # noqa: E731
    setattr(cls, attr, sentinel)
    await plugin.terminate()
    check("★ 卸载不会覆盖第三方后来替换的实现", getattr(cls, attr) is sentinel)
    key = (label, id(cls), attr)                   # orig 的键用 id(cls)
    setattr(cls, attr, orig[key])                  # 复原，别污染后续测试
    check("现场已复原", getattr(cls, attr) is orig[key])

    print("\n6) ★ 认领：上一次 terminate 失败时，新实例必须能收拾残局")
    # 场景：A 装好补丁但 terminate 抛异常/被跳过 ⇒ 补丁留着；
    # 框架随后初始化 B。若 B 不"认领"，就再也没人能还原这个补丁
    # —— 表现为"插件关了行为还在"，且毫无报错。
    key = ("reuse_http_client", id(mc.OpenAICompatibleLLMClient), "_build_client")
    orig0 = orig[key]
    setattr(mc.OpenAICompatibleLLMClient, "_build_client", orig0)   # 从干净状态开始

    A = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    A._client_cache = {}
    A.takeover_build_client = True
    A._install_client_cache()
    check("A 装上补丁", mc.OpenAICompatibleLLMClient._build_client is not orig0)

    B = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    B._client_cache = {}
    B.takeover_build_client = True
    B._install_client_cache()          # 幂等 ⇒ 应当"认领"而不是无视
    check("★ B 认领了已有补丁（登记进自己的 registry）", len(B.patches._handles) >= 1,
          f"{len(B.patches._handles)} 个")

    await B.terminate()
    check("★★ B 的 terminate 能把 A 留下的补丁还原掉",
          mc.OpenAICompatibleLLMClient._build_client is orig0,
          f"当前={'已还原' if mc.OpenAICompatibleLLMClient._build_client is orig0 else '仍是补丁'}")
    check("现场干净", not getattr(mc.OpenAICompatibleLLMClient._build_client, "__kira_accel__", False))

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for x in FAIL:
            print("   ✗", x)
        sys.exit(1)
    print("🎉 全部通过 —— 接管点装得干净、卸得彻底")


asyncio.run(main())
