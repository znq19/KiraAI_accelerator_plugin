"""HTTP 客户端复用：缓存的键必须覆盖 _build_client 用到的一切。

背景（2026-09-23 全量审计发现）：
  缓存键原本是 (base_url, api_key, id(provider_config))。两个问题：
    ① `id()` 会被解释器复用，不是可靠的标识；
    ② **漏了 headers** —— 用户改了提供商的自定义 header 后，只要
       base_url 与 api_key 没变就会命中旧客户端，新 header 一直不生效
       （要等插件重载才恢复）。

本测试用**真客户端**构造带不同 header 的两个配置，验证：
  · 同一份配置重复取 ⇒ 命中缓存（真的复用了）
  · header 变了 ⇒ 不命中（新配置立刻生效）
"""
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

from core.provider.provider import ModelInfo, ModelType                # noqa: E402
(OpenAICompatibleLLMClient,) = _env.resolve("OpenAICompatibleLLMClient")   # ★ 跨世代（3.0 在 core.provider.openai_compatible）

main_mod = _env.load("main")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def client(base_url="http://127.0.0.1:9/v1", key="sk-x", headers=None):
    pc = {"api_key": key, "base_url": base_url}
    if headers is not None:
        pc["section_advanced"] = {"headers": headers}
    info = ModelInfo(model_type=ModelType.LLM, model_id="m", provider_id="p",
                     provider_name="n", provider_config=pc, model_config={})
    return OpenAICompatibleLLMClient(info)


def main():
    print("0) 装上报废缓存补丁（走插件自己的 _install_client_cache）")
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin._client_cache = {}
    plugin.patches.uninstall_all()
    plugin._install_client_cache()
    check("补丁已安装", len(plugin._client_cache) == 0, "缓存初始为空")

    print("\n1) 同一份配置 ⇒ 应该复用同一个客户端")
    c1 = client(headers={"X-Test": "1"})
    a = c1._build_client()
    b = c1._build_client()
    check("★ 同一配置两次取到同一个对象（真的复用了）", a is b, f"{id(a)} vs {id(b)}")
    check("缓存里有 1 条", len(plugin._client_cache) == 1,
          str(list(plugin._client_cache.keys())))

    print("\n2) ★ 只改 header ⇒ 必须重建（旧键漏了 header）")
    c2 = client(headers={"X-Test": "2"})
    d = c2._build_client()
    check("★ 换了 header 不命中旧缓存（新 header 立刻生效）", d is not a,
          f"复用旧对象={d is a}")
    h1 = dict(getattr(a, "default_headers", {}) or {})
    h2 = dict(getattr(d, "default_headers", {}) or {})
    check("★ 新客户端确实带上了新 header", h2.get("X-Test") == "2",
          f"旧={h1.get('X-Test')} 新={h2.get('X-Test')}")
    check("缓存变成 2 条", len(plugin._client_cache) == 2, str(len(plugin._client_cache)))

    print("\n3) 改 base_url / api_key 也要重建")
    e = client(base_url="http://127.0.0.1:8/v1", headers={"X-Test": "1"})._build_client()
    check("改 base_url ⇒ 新客户端", e is not a)
    f = client(key="sk-y", headers={"X-Test": "1"})._build_client()
    check("改 api_key ⇒ 新客户端", f is not a and f is not e)

    print("\n4) 缓存不会无限增长")
    before = len(plugin._client_cache)
    for i in range(60):
        client(headers={"X-N": str(i)})._build_client()
    check("★ 超过上限会整体清掉重建（不会越用越大）",
          len(plugin._client_cache) <= 33, f"上限后 {len(plugin._client_cache)} 条")

    print("\n5) 键里不再有 id()")
    src = (_env.ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("def _install_client_cache")
    body = src[i:i + 1800]
    # 先剥掉注释行 —— 注释里正是在解释"以前用了 id()，为什么不对"
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("#"))
    check("★ 缓存键不含 id(...)（只看代码，不看注释）",
          "id(self.model.provider_config)" not in code)
    check("键里包含 headers", "headers" in body and "sorted(hdr.items())" in body)

    print("\n6) 剥离逻辑不再回退到实例变量（否则会拿错会话的响应 ⇒ 丢内容）")
    j = src.index("def _install_early_sent_strip")
    strip_body = src[j:j + 2200]
    check("★ 不再有 _current_resp 兜底",
          'getattr(plugin, "_current_resp", None)' not in strip_body)
    check("查不到就按「没抢发过」处理（n=0）",
          "plugin._resp_by_sid.get(_k) if _k else None" in strip_body)

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for x in FAIL:
            print("   ✗", x)
        sys.exit(1)
    print("🎉 全部通过 —— 客户端缓存与剥离都以安全为准")


main()
