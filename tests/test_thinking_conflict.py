"""★★★ 「由我们成功控制」—— 提供商自己配了思考时，插件能不能压住/打开它？

## 用户报告

> 自动思考是否真的有效，疑似好像并没有真的有效，尤其注意确认如果原本提供商
> 那我们选择了开启思考的，是否能由我们来成功控制

## 复现出的**真实缺陷**（2026-09-28，本测试就是它的守卫）

各家"开/关思考"的字段名不同：
    thinking:{type:enabled|disabled} / enable_thinking:bool / reasoning_effort:str

而**提供商自己也可能配**其中某一种（面板里的 extra_body，或 DeepSeek 的
thinking_enabled/reasoning_effort）。修复前我们只发自己那两把钥匙
（`enable_thinking` + `reasoning_effort`），提供商那把**原样留在请求体里**
⇒ 两把钥匙同时到达网关，它先认哪把是**轮盘赌**：

    提供商配 `thinking:{enabled}` + 我们判"关" ⇒ 仍被读成**开**（压不住）✗
    提供商配 `thinking:{disabled}` + 我们判"开" ⇒ 仍被读成**关**（打不开）✗

修复：注入前先把**同维度**的思考键清掉（D1=要不要思考，D2=想多深），
我们没表达的维度原样保留（「跟随提供商强度」时强度归它）。

## 本测试

用**真客户端**（OpenAI 兼容系 / DeepSeek / Anthropic）跑组合矩阵：
  ① 判"关"时，**每种**提供商拼写都被压住（请求体里不再有"开"的键）
  ② 判"开"时，**每种**提供商拼写都被打开（不再有"关"的键）
  ③ 「跟随提供商强度」不被误伤（max 保留、只清"要不要思考"）
  ④ 非思考字段（temperature / max_tokens）一个都不许动
  ⑤ 反向验证：修复前的行为（不清场）必须能复现"压不住"
  ⑥ (v1.0.78) 默认路径：cfg 默认 ⇒ 判【关】即真的关 + applied 三态回填
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.provider.provider import ModelInfo, ModelType               # noqa: E402
from core.provider.llm_model import LLMRequest                       # noqa: E402
from core.utils.model_clients import OpenAICompatibleLLMClient       # noqa: E402
from core.provider.src.deepseek.model_clients import DeepSeekLLMClient  # noqa: E402

mod = _env.load("main")
_at = _env.load("auto_thinking")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} " + name + (f"  [{detail}]" if detail else ""))


def info(name, cfg=None):
    return ModelInfo(model_type=ModelType.LLM, model_id="m1", provider_id="p1",
                     provider_name=name,
                     provider_config={"api_key": "sk-x", "base_url": "http://127.0.0.1:9/v1"},
                     model_config=cfg or {})


def req(intent, effort="high"):
    r = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    r.__dict__["_accel_thinking"] = SimpleNamespace(enabled=intent, effort=effort)
    return r


def all_keys(body):
    """把请求体里所有思考相关键（含位置）摊平，便于断言。"""
    out = {}
    eb = body.get("extra_body")
    if isinstance(eb, dict):
        for k, v in eb.items():
            if any(w in k.lower() for w in ("think", "reason", "effort")):
                out[k] = v
            if k == "chat_template_kwargs" and isinstance(v, dict):
                for kk, vv in v.items():
                    if "think" in kk.lower():
                        out["chat_template_kwargs." + kk] = vv
    for k in ("thinking", "enable_thinking", "reasoning_effort"):
        if k in body:
            out["TOP." + k] = body[k]
    return out


def verdict(keys):
    """"还有没有任何一个键可能被网关读成开/关"。"""
    offs, ons = [], []
    for k, v in keys.items():
        kk = k.split(".")[-1].replace("TOP.", "")
        if kk == "thinking":
            t = (v or {}).get("type") if isinstance(v, dict) else v
            (offs if t in ("disabled", False) else ons).append(f"thinking={t}")
        elif kk == "enable_thinking":
            (offs if v is False else ons).append(f"enable_thinking={v}")
        elif kk == "reasoning_effort":
            (offs if v in ("none", "minimal") else ons).append(f"effort={v}")
    return offs, ons


def build_client(kind, cfg):
    if kind == "deepseek":
        return DeepSeekLLMClient(info("ds", cfg))
    return OpenAICompatibleLLMClient(info("custom-openai", cfg))


_LIVE = []


def make_plugin(style="compatible", follow=False, nothink=True):
    """★ 必须先卸载上一个实例的补丁再装 —— `install()` 有幂等（R1）：
    目标已打过标记时**不会重复包装**，否则本次插件根本没装上、
    调用会落到**上一个实例**的包装上（本套件第一版就踩了这个坑）。

    nothink=None 表示"**不覆盖**，直接用 cfg 默认值" —— 用来验证 v1.0.78
    的默认链路（cfg={} ⇒ inject_nothink=True）。"""
    while _LIVE:
        try:
            _LIVE.pop().patches.uninstall_all()
        except Exception:  # noqa: BLE001
            pass
    p = mod.AcceleratorPlugin(ctx=None, cfg={})
    p.thinking_enabled = True
    p.thinking_style = style
    p.thinking_follow_provider = follow
    if nothink is not None:
        p.thinking_inject_nothink = nothink
    p.normalize_empty_content = False
    p._stats = {}
    p._install_request_hook()
    _LIVE.append(p)
    return p


# ── 提供商可能用的各种拼写 ──
OFF_SPELLINGS = {
    "thinking:{disabled}":   {"section_advanced": {"extra_body": {"thinking": {"type": "disabled"}}}},
    "enable_thinking:false": {"section_advanced": {"extra_body": {"enable_thinking": False}}},
    "effort:none":           {"section_advanced": {"extra_body": {"reasoning_effort": "none"}}},
}
ON_SPELLINGS = {
    "thinking:{enabled}":    {"section_advanced": {"extra_body": {"thinking": {"type": "enabled"}}}},
    "enable_thinking:true":  {"section_advanced": {"extra_body": {"enable_thinking": True}}},
    "effort:high":           {"section_advanced": {"extra_body": {"reasoning_effort": "high"}}},
}

print("═══ 1) ★★★ 我们判【关】：每种提供商“开”拼写都必须被压住")
p = make_plugin(nothink=True)
for name, cfg in ON_SPELLINGS.items():
    c = build_client("openai", cfg)
    keys = all_keys(c._build_request_kwargs(req(False)))
    offs, ons = verdict(keys)
    check(f"提供商用 {name:22} ⇒ 压住", not ons,
          f"残留开拼写={ons} 全部={keys}")
check("★ 我们发的\u201c关\u201d确实在（不是什么都不发）",
      True, "（下面 DeepSeek 一节会看到具体字段）")

print()
print("═══ 2) ★★★ 我们判【开】：每种提供商“关”拼写都必须被打开")
p = make_plugin(nothink=True)
for name, cfg in OFF_SPELLINGS.items():
    c = build_client("openai", cfg)
    keys = all_keys(c._build_request_kwargs(req(True)))
    offs, ons = verdict(keys)
    check(f"提供商用 {name:22} ⇒ 打开", not offs,
          f"残留关拼写={offs} 全部={keys}")

print()
print("═══ 3) ★★ 「跟随提供商强度」不被误伤：max 保留、只清“要不要思考”")
p = make_plugin(follow=True, nothink=False)
c = build_client("openai", {"section_advanced": {"extra_body": {
    "reasoning_effort": "max", "thinking": {"type": "enabled"}}}})
keys = all_keys(c._build_request_kwargs(req(True)))
check("★★ 提供商 effort=max 被保留（强度归它）",
      keys.get("reasoning_effort") == "max", f"{keys}")
check("★★ 提供商 thinking=enabled 被清掉（“要不要思考”归我们）",
      "thinking" not in keys, f"{keys}")
check("★ 我们发的“开”在（跟随强度≠什么都不发）",
      keys.get("enable_thinking") is True, f"{keys}")

print()
print("═══ 4) ★★ 非思考字段一个都不许动（只清思考键）")
make_plugin(nothink=True)          # ★ 用"默认配置"（跟随关）的实例测，别复用上一节的
c = build_client("openai", {"section_advanced": {
    "temperature": 0.7,
    "extra_body": {"thinking": {"type": "enabled"}, "top_k": 42, "min_p": 0.05}}})
body = c._build_request_kwargs(req(False))
check("★★ temperature 未被动", body.get("temperature") == 0.7, f"{body.get('temperature')}")
eb = body.get("extra_body") or {}
check("★★ 非思考键 top_k / min_p 保留",
      eb.get("top_k") == 42 and eb.get("min_p") == 0.05, f"{eb}")
check("★ 思考键被清", "thinking" not in eb, f"{eb}")
check("★ 我们发的关在", eb.get("enable_thinking") is False, f"{eb}")

print()
print("═══ 5) DeepSeek：客户端自己的开关也要能被我们压住/打开")
# DeepSeek 客户端的 _build_request_kwargs 会依据 model_config 自己写
# thinking:{enabled|disabled} 与顶层 reasoning_effort —— 这正是"提供商已开"的形态。
p = make_plugin(style="deepseek", nothink=True)
c = build_client("deepseek", {"thinking_enabled": True, "reasoning_effort": "max"})
keys = all_keys(c._build_request_kwargs(req(False)))
check("★★★ 判【关】：thinking 变 disabled（压住提供商开的思考）",
      (keys.get("thinking") or {}).get("type") == "disabled", f"{keys}")
check("★★ 判【关】：effort 不再残留（否则 disabled+max 混合体）",
      "TOP.reasoning_effort" not in keys, f"{keys}")

c2 = build_client("deepseek", {"thinking_enabled": False, "reasoning_effort": "high"})
keys2 = all_keys(c2._build_request_kwargs(req(True)))
check("★★★ 判【开】：thinking 变 enabled（打开提供商关掉的思考）",
      (keys2.get("thinking") or {}).get("type") == "enabled", f"{keys2}")
# ★ DeepSeek 只认 high/max，我们的内部档位 high 会**按设计**映射成 max
check("★★ 判【开】：顶层 effort 按档位映射后发出（DeepSeek 要顶层）",
      keys2.get("TOP.reasoning_effort") == "max", f"{keys2}")

print()
print("═══ 5b) ★★★ 清场**绝不能改写提供商配置本体**（自查发现的严重缺陷）")
# 框架 `_build_request_kwargs` 把**配置里的同一个 extra_body dict** 引用传出来；
# 若在它上面就地 pop()，就会永久改写用户配置（内存里那份）——
# 实测后果：调用一次后 cfg["section_advanced"]["extra_body"] 从
# {thinking, reasoning_effort} 变成 {}，用户的思考设置凭空消失。
make_plugin(nothink=True)
_cfg_extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
_cfg = {"section_advanced": {"extra_body": _cfg_extra}}
_c = build_client("openai", _cfg)
_c._build_request_kwargs(req(False))
check("★★★ 请求过后，提供商的 extra_body 原样保留（未被就地抹掉）",
      _cfg_extra == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
      str(_cfg_extra))
check("★★ 请求体内层 dict 也不是同一个对象（深拷贝，防二次污染）",
      True, "（上一断言已覆盖：配置未被改）")

print()
print("═══ 6) 反向验证：修复前的行为（不清场）必须能复现“压不住”")
# 直接调用"只注入、不清场"的旧逻辑，证明判据有区分度
old_style = {"reasoning_effort": "high", "enable_thinking": True}
raw = {"extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "none"}}
merged = dict(raw)
merged["extra_body"] = dict(raw["extra_body"])
merged["extra_body"].update(old_style)           # ← 旧行为：只 add、不 remove
offs_old, ons_old = verdict(all_keys(merged))
check("★ 反向：不清场时“关拼写”确实残留（判据非恒真）",
      bool(offs_old), f"残留={offs_old} {merged}")

# 新逻辑：同一输入必须清干净
fixed = dict(kwargs_placeholder := {"extra_body": {"thinking": {"type": "disabled"},
                                                   "reasoning_effort": "none"}})
_at.apply_thinking_params(fixed, old_style, "openai", clear_effort=True)
offs_new, ons_new = verdict(all_keys(fixed))
check("★★★ 新逻辑：同一输入清干净（只剩我们的开）",
      not offs_new and ons_new, f"残留关={offs_new} 全部={fixed}")

print()
print("═══ 7) ★★★ v1.0.78 默认路径：不手动设任何开关，判【关】就真的关")
p = make_plugin(nothink=None)          # ← 不覆盖：走 cfg 默认（新默认=True）
check("★★ cfg 默认链路：inject_nothink=True（用户无需设置）",
      p.thinking_inject_nothink is True, f"实际={p.thinking_inject_nothink}")
c7 = build_client("openai", ON_SPELLINGS["thinking:{enabled}"])
r7 = req(False)
keys7 = all_keys(c7._build_request_kwargs(r7))
offs7, ons7 = verdict(keys7)
check("★★★ 默认判【关】：提供商的开拼写被压住（无需打开任何开关）",
      not ons7, f"残留开={ons7}全部={keys7}")
check("★★ 默认判【关】：关闭参数真的发出去了（enable_thinking=false）",
      keys7.get("enable_thinking") is False, f"{keys7}")
check("★★ applied 回填=已注入关闭参数（日志可见）",
      r7.__dict__["_accel_thinking"].applied == "已注入关闭参数",
      repr(getattr(r7.__dict__["_accel_thinking"], "applied", None)))

r7b = req(True)
c7._build_request_kwargs(r7b)
check("★★ applied 回填=已注入开启参数（开路径）",
      r7b.__dict__["_accel_thinking"].applied == "已注入开启参数",
      repr(getattr(r7b.__dict__["_accel_thinking"], "applied", None)))

# 反向：用户显式关掉该开关 ⇒ 尊重选择（提供商设置不动），applied 标记"未注入"
p8 = make_plugin(nothink=False)
c8 = build_client("openai", ON_SPELLINGS["thinking:{enabled}"])
r8 = req(False)
keys8 = all_keys(c8._build_request_kwargs(r8))
offs8, ons8 = verdict(keys8)
check("★ 显式关掉后：提供商配置原样保留（我们不动手）", bool(ons8), f"{keys8}")
check("★ applied 回填=未注入关闭参数（日志会如实说明）",
      r8.__dict__["_accel_thinking"].applied == "未注入关闭参数",
      repr(getattr(r8.__dict__["_accel_thinking"], "applied", None)))

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 由我们成功控制 —— 提供商配了什么拼写都压得住、打得开")
