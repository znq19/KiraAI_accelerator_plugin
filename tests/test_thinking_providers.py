"""自动思考的注入到底覆盖哪些 provider？（2026-09-23 用户提问引出）

用户问：「假设我提供商里给模型开了思考，我们自动思考那个开关还会生效帮调控吗？」

我的注入挂点：`install(h, OpenAICompatibleLLMClient, "_build_request_kwargs", ...)`
—— **只覆盖 OpenAICompatibleLLMClient 及其子类**。

而框架里：
  · DeepSeekLLMClient(LLMModelClient)          ← 自己的 _build_request_kwargs
  · AnthropicCompatibleLLMClient(LLMModelClient) ← 自己的 _build_request_body
两者都**不是** OpenAICompatibleLLMClient 的子类 ⇒ 注入根本到不了。

本测试用三个**真客户端**分别调用它们自己的请求构造方法，比对
「打补丁前」vs「打补丁后」的思考相关参数，看注入有没有落地。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自身
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.provider.provider import ModelInfo, ModelType              # noqa: E402
from core.provider.llm_model import LLMRequest                      # noqa: E402
from core.utils.model_clients import OpenAICompatibleLLMClient      # noqa: E402
from core.provider.src.deepseek.model_clients import DeepSeekLLMClient          # noqa: E402
from core.provider.src.anthropic.model_clients import AnthropicCompatibleLLMClient  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def info(name, cfg=None):
    return ModelInfo(model_type=ModelType.LLM, model_id="m1", provider_id="p1",
                     provider_name=name,
                     provider_config={"api_key": "sk-x", "base_url": "http://127.0.0.1:9/v1"},
                     model_config=cfg or {})


def req():
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


def thinking_params(d):
    """从请求体里挑出所有跟思考有关的键。"""
    out = {}
    for k, v in (d or {}).items():
        if any(w in k.lower() for w in ("think", "reason", "effort")):
            out[k] = v
        if k in ("extra_body", "reasoning") and isinstance(v, dict):
            for kk, vv in v.items():
                if any(w in kk.lower() for w in ("think", "reason", "effort")):
                    out["extra_body." + kk] = vv
    return out


def build(client, request=None):
    """调用该客户端自己的请求构造方法（各家名字不同）。

    ⚠️ 必须传入**同一个** request 对象 —— 自动思考的决定是挂在
       request.__dict__["_accel_thinking"] 上的，新建 request 就丢了。
    """
    r = request if request is not None else req()
    if hasattr(client, "_build_request_kwargs"):
        return client._build_request_kwargs(r)
    if hasattr(client, "_build_request_body"):
        return client._build_request_body(r)
    return {}


# ── 建三种真客户端 ──
print("0) 三种客户端 + 继承关系")
clients = {
    "openai兼容(阿里/硅基/火山)": OpenAICompatibleLLMClient(info("ali")),
    "deepseek": DeepSeekLLMClient(info("ds", {"thinking_enabled": True,
                                              "reasoning_effort": "high"})),
    "anthropic": AnthropicCompatibleLLMClient(info("anth")),
}
for n, c in clients.items():
    print(f"     {n:26} {type(c).__name__:32} 继承={[b.__name__ for b in type(c).__mro__[1:3]]}")
check("三种客户端都能构造", len(clients) == 3)

print("\n1) 补丁前：各家自己会发什么思考参数")
before = {n: thinking_params(build(c)) for n, c in clients.items()}
for n, v in before.items():
    print(f"     {n:26} {v}")

print("\n2) 装上插件（走插件自己的 _install_request_hook）")
import importlib
mod = _env.load("main")
schema = __import__("json").load(
    open(_env.ROOT / "schema.json", encoding="utf-8"))
cfg = {}
for sec in schema.values():
    if isinstance(sec, dict) and "fields" in sec:
        for k, f in sec["fields"].items():
            if isinstance(f, dict) and "default" in f:
                cfg["%s.%s" % (sec.get("key", ""), k)] = f["default"]
plugin = mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.thinking_enabled = True
plugin.thinking_style = "compatible"
plugin.thinking_inject_nothink = False
plugin.normalize_empty_content = False
plugin._proxy_cache = {}
plugin._stats = {}
plugin._install_request_hook()
check("插件补丁已安装", plugin.patches is not None, type(plugin.patches).__name__)

print("\n3) ★ 补丁后：自动思考=开 时，注入是否落地")
r = req()
r.__dict__["_accel_thinking"] = SimpleNamespace(enabled=True, effort="high")
after_on = {}
for n, c in clients.items():
    after_on[n] = thinking_params(build(c, r))
    print(f"     {n:26} {after_on[n]}")

check("★ OpenAI 兼容系：注入生效",
      after_on["openai兼容(阿里/硅基/火山)"] != before["openai兼容(阿里/硅基/火山)"],
      f"变化={after_on['openai兼容(阿里/硅基/火山)']}")

check("★★ DeepSeek：注入生效（当前是失效的）",
      after_on["deepseek"] != before["deepseek"],
      f"补丁前后一样={after_on['deepseek']}")
check("★★ Anthropic：注入生效（当前是失效的）",
      after_on["anthropic"] != before["anthropic"],
      f"补丁前后一样={after_on['anthropic']}")

print("\n4) ★ v1.0.78 新默认：判定为关时，默认路径就会注入关闭参数（三家）")
# 用**纯默认配置**再建一个实例 —— 验证"schema 默认值 → 插件读取 → 注入行为"全链路。
# 这正是本次改动的核心：以前默认关，判了"关"也不发关闭参数（模型照旧思考）。
fresh = mod.AcceleratorPlugin(ctx=None, cfg={})
check("★★ 默认配置 ⇒ inject_nothink=True（配置默认值链路）",
      fresh.thinking_inject_nothink is True, f"实际={fresh.thinking_inject_nothink}")
# 换 fresh 的补丁（install 有幂等：必须先卸载旧实例的，否则不会重新包装）
plugin.patches.uninstall_all()
fresh.thinking_enabled = True
fresh.thinking_style = "compatible"
fresh.normalize_empty_content = False
fresh._stats = {}
fresh._install_request_hook()
ali = "openai兼容(阿里/硅基/火山)"
r2 = req()
r2.__dict__["_accel_thinking"] = SimpleNamespace(enabled=False, effort="low")
off_default = {}
for n, c in clients.items():
    off_default[n] = thinking_params(build(c, r2))
    print(f"     {n:26} {off_default[n]}")
check("★★ OpenAI 兼容系：enable_thinking=false 已注入（无需任何手动开关）",
      off_default[ali].get("extra_body.enable_thinking") is False, str(off_default[ali]))
check("★★ DeepSeek：提供商开的思考被压成 disabled",
      (off_default["deepseek"].get("extra_body.thinking") or {}).get("type") == "disabled",
      str(off_default["deepseek"]))
check("★★ Anthropic：没有 enabled 状态的思考键残留",
      (off_default["anthropic"].get("thinking") or {}).get("type") != "enabled",
      str(off_default["anthropic"]))

print("\n5) 用户**显式关掉**该开关 ⇒ 尊重选择、不去动提供商的思考")
fresh.thinking_inject_nothink = False
r3 = req()
r3.__dict__["_accel_thinking"] = SimpleNamespace(enabled=False, effort="low")
optout = {n: thinking_params(build(c, r3)) for n, c in clients.items()}
for n, v in optout.items():
    print(f"     {n:26} {v}")
check("OpenAI 兼容系：没有我们的关闭键（enable_thinking 不存在或非 false）",
      optout[ali].get("extra_body.enable_thinking") is not False, str(optout[ali]))
check("DeepSeek：提供商自己的设置原样保留（thinking=enabled 还在）",
      (optout["deepseek"].get("extra_body.thinking") or {}).get("type") == "enabled",
      str(optout["deepseek"]))

print("\n6) ★ 新开关「开思考时跟随提供商的强度」（默认关）")
_at = _env.load("auto_thinking")

probe = SimpleNamespace(model_config={"reasoning_effort": "max"})
check("能读出提供商配的强度（DeepSeek 顶层字段）",
      _at.provider_effort(probe) == "max", str(_at.provider_effort(probe)))
probe2 = SimpleNamespace(model_config={
    "section_advanced": {"extra_body": {"reasoning_effort": "high"}}})
check("能读出提供商配的强度（网关 extra_body 写法）",
      _at.provider_effort(probe2) == "high", str(_at.provider_effort(probe2)))
check("没配时返回 None", _at.provider_effort(SimpleNamespace(model_config={})) is None)

ours = {"reasoning_effort": "low", "thinking": {"type": "enabled"}}
kept = _at.follow_provider_effort(ours, "max")
# ★★★ 2026-09-28 契约更正（用户："是否能由我们来成功控制"）：
#   旧契约是"把我们的 effort **去掉**，让提供商那份生效"。但那**名不副实**——
#   框架对 DeepSeek 只在 `thinking_enabled=True` 时才写 reasoning_effort
#   （core/provider/src/deepseek/model_clients.py），提供商"关着思考但配了 max"时
#   框架**根本不发**那个强度 ⇒ 我们把思考打开后，用户的 max 就丢了。
#   ⇒ 新契约：把提供商的强度**替进**我们的参数（谁该发由我们保证发出去）。
check("★★ 开关打开：effort **替成提供商的** max（不是删掉、也不再是 low）",
      kept.get("reasoning_effort") == "max", str(kept))
check("开关打开：仍然发「开思考」", "thinking" in kept, str(kept))
check("开关打开但提供商没配强度 ⇒ 用我们的（否则就没强度了）",
      _at.follow_provider_effort(ours, None).get("reasoning_effort") == "low",
      str(_at.follow_provider_effort(ours, None)))

src_main = (_env.ROOT / "main.py").read_text(encoding="utf-8")
check("默认关（默认不启用跟随）",
      'c_thk.get("follow_provider_effort", False)' in src_main)
check("★ 开关只在「开思考」那一路生效，不影响关思考",
      "if self.thinking_follow_provider:" in src_main
      and "elif self.thinking_inject_nothink:" in src_main)

print("\n7) ★ 端到端：提供商 effort=max，两种开关下各发生什么")
ds = DeepSeekLLMClient(info("ds", {"thinking_enabled": True, "reasoning_effort": "max"}))

# 默认（开关关）：插件按自己的判定发强度 —— 允许低于提供商（这是设计选择）
fresh.thinking_follow_provider = False
r_low = req(); r_low.__dict__["_accel_thinking"] = SimpleNamespace(enabled=True, effort="low")
d1 = thinking_params(build(ds, r_low))
print(f"     开关关 + 判 low ⇒ {d1}")
check("★ 默认：插件按判定发强度（可能低于提供商）",
      d1.get("reasoning_effort") == "high", str(d1.get("reasoning_effort")))

# 开关开：强度交给提供商 max
fresh.thinking_follow_provider = True
r_low2 = req(); r_low2.__dict__["_accel_thinking"] = SimpleNamespace(enabled=True, effort="low")
d2 = thinking_params(build(ds, r_low2))
print(f"     开关开 + 判 low ⇒ {d2}")
check("★★ 开关开：强度仍是用户设的 max（插件只负责开）",
      d2.get("reasoning_effort") == "max", str(d2.get("reasoning_effort")))

# 关思考在两种强度姿态下都要照常（inject_nothink 保持新默认=开）
fresh.thinking_inject_nothink = True
r_off = req(); r_off.__dict__["_accel_thinking"] = SimpleNamespace(enabled=False, effort="low")
d3 = thinking_params(build(ds, r_off))
check("★ 跟随开时，关思考依然生效",
      (d3.get("extra_body.thinking") or {}).get("type") == "disabled",
      str(d3.get("extra_body.thinking")))
fresh.thinking_follow_provider = False
d4 = thinking_params(build(ds, r_off))
check("★ 跟随关时，关思考也照常生效",
      (d4.get("extra_body.thinking") or {}).get("type") == "disabled",
      str(d4.get("extra_body.thinking")))

print("\n8) 自动思考没启用时，日志不能吓人")
check("★ 未启用时日志写「未启用」而不是「关」", '"未启用"' in src_main,
      "main.py 的 observe_final")
check("★ 判「关」时带上得分（让人看出这是本轮判定）", "关(得分" in src_main)

print("\n" + "=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过")
