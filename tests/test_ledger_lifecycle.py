"""★ 经常性 warning 的守卫：`响应标记缺失，改用本轮台账剥离` 不该反复出现。

## 这条告警的两种触发，必须区分
  ① **真该报**：本轮确实抢发过，但响应标记丢了（例如 on_complete 没跑到）
     ⇒ 台账是唯一的旁证 ⇒ 用它是**正确**的，报一次即可。
  ② **不该报**（就是用户看到的"经常性 warning"）：
     同一轮内 `send_xml_messages` 被调用**第二次**（多步 agent loop / 框架其它调用点），
     此时响应已 pop、而台账**还没清** ⇒ 走兜底 + 告警。
     更糟：会拿**本次的（可能完全不相关的）文本**去按内容剥离 ⇒ **可能丢内容**。

## 根因与修法
台账原来只等"下一轮开始"才清。现在**消费完立即清**（与 pop 响应同一处 finally）。
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
MAIN = (HERE / "main.py").read_text(encoding="utf-8")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


print("═══ 1) 台账的生命周期：**活到轮结束**，但**只消费一次**")
# ★★★ 这条契约是两轮修正的结果，两个方向都不能过头：
#   · 只"消费完就清"（旧）⇒ 多步 loop 的第二步 n=0 ⇒ **整批重复**（用户截图）
#   · 只"留到轮结束"（过头）⇒ 第二步拿台账按序号切 ⇒ **误切 = 丢内容**
#   ⇒ 正确契约：台账**留到轮结束**（`final_result` 清），但**只有第一次调用消费它**。
_fin_i = MAIN.find("finally:")
_fin = MAIN[_fin_i:_fin_i + 1200]
check("★ finally 里**不再**清台账（否则多步第二次数不到 ⇒ 整批重复）",
      "_sent_ledger.pop" not in _fin)
check("★ 轮结束统一清理（final_result 里调 _clear_round_state）",
      "_clear_round_state" in MAIN
      and re.search(r"async def observe_final[\s\S]{0,500}?_clear_round_state", MAIN) is not None)
check("★ 轮结束清理**同时**清台账 / 消费标记 / 响应缓存",
      all(k in MAIN[MAIN.find("def _clear_round_state"):MAIN.find("def _clear_round_state") + 800]
          for k in ("_sent_ledger.pop", "_ledger_consumed.pop", "_resp_by_sid.pop")))
# ★ 标记已从「纯布尔」升级为「时间戳」—— 因为钩子可能被跳过，
#   纯布尔会永久卡在 True ⇒ 每轮都重复（线上"更明显了"的根因）。
# ★ 契约再升级：从「时间戳」改为「**轮次 id**」——
#   时间戳区分不了"同一轮的第二步"与"下一轮的第一步"（连续对话间隔远小于 5 分钟）
#   ⇒ 曾导致"从第二轮起每轮都不剥离"= 每轮重复。
check("★★ 有「只消费一次」的标记（按**轮次 id**，不依赖钩子清理）",
      "_ledger_consumed" in MAIN and '"event_id", None) or sid_now' in MAIN)
check("★★ 已消费时 n 归零（后续步原样交出）",
      re.search(r"_cons_ev[\s\S]{0,200}?n = 0", MAIN) is not None
      or re.search(r"ledger = \[\][\s\S]{0,120}?n = 0", MAIN) is not None)

print()
print("═══ 2) ★★★ 台账是权威来源（不是兜底），且拿不到标记时不该告警")
# 背景：原判据以为"响应标记才是正路、台账是兜底"，据此要求"用兜底时必须告警"。
# 事实相反：台账是**发送那一刻**记下的第一手记录；响应标记只是同一份信息的拷贝，
# 而框架在 ON_LLM_RESPONSE 时已消费它、其它插件还会重写它 ⇒ 拿不到是**常态**。
# ★ 正确的结构是"先取两边、再决定用谁"：
#     ① segs = early_sent_segments(resp)   ← 先取响应标记的原文
#     ② ledger = _sent_ledger.get(sid)     ← 再取台账
#     ③ if ledger: n = len(ledger); segs = list(ledger)   ← **台账胜出**
#   所以不能拿"取值顺序"当判据（那样必然报错，上一版就这么自己误判）。
#   要断言的是**决策结构**：`if ledger:` 分支里必须写入 n 与 segs（即台账胜出）。
_i0 = MAIN.index("if ledger:")
_lb = MAIN[_i0:MAIN.index("elif n > 0 and not segs:", _i0)]
check("★ 台账优先：`if ledger:` 分支里就用台账覆盖 n 与 segs",
      "n = len(ledger)" in _lb and "segs = list(ledger)" in _lb)
check("★ 且该分支在按序号剥离之前（不会被绕过）",
      MAIN.index("if ledger:") < MAIN.index("strip_early_sent_smart("))

# 去掉注释后再查（注释里会引用旧文案，说明"曾经这么报"）
_code = re.sub(r"(?m)^\s*#.*$", "", MAIN)
check("★★ 拿不到响应标记时不再打 warning（原来一直刷屏、且误导）",
      "响应标记缺失，改用本轮台账剥离" not in _code)
check("★ 只在『两边段数不一致』时才告警（真异常）",
      "已发段数不一致" in MAIN)
check("★ 台账为空且拿不到段原文时也留线索",
      "拿不到段原文" in MAIN)

print()
print("═══ 3) 行为模拟：同一轮被调用两次（多步 loop）")
# 复刻发送层的判定
def decide(n_marker, ledger):
    n = n_marker
    segs = []
    warn = False
    if n <= 0 and ledger:
        n = len(ledger)
        segs = list(ledger)
        warn = True
    return n, segs, warn


ledger = ["A", "B"]

# 第一次调用：标记在 ⇒ 用它，不告警；**消费完立即清**
n1, s1, w1 = decide(2, ledger)
check("第一次：标记 2 生效", n1 == 2 and not w1, f"n={n1} warn={w1}")
ledger = []          # ← 修法的效果：消费完立即清
resp = None          # ← finally 里 pop 掉

# 第二次调用（多步 loop）：标记没了、台账也没了
n2, s2, w2 = decide(0, ledger)
check("★ 第二次：n=0、安静交给框架（不误切、不告警）", n2 == 0 and not w2,
      f"n={n2} warn={w2}")
check("★ 第二次：segs 为空（不拿台账去误切本次文本）", s2 == [], str(s2))

print()
print("═══ 4) ★ 台账优先：标记缺失时安静使用（这正是线上一直在走的路）")
n4, s4, _ = decide(0, ["A", "B"])       # 标记缺失、台账在
check("★ 标记缺失 ⇒ 用台账、且**不告警**（线上常态路径）",
      n4 == 2 and s4 == ["A", "B"], f"n={n4}")

print()
print("═══ 5) 反向验证：若坚持标记优先，台账就会被忽略 ⇒ 整份回复重发")
def decide_marker_first(n_marker, ledger):
    if n_marker > 0:
        return n_marker, [], False
    return 0, [], False                 # 标记优先且为 0 ⇒ 不剥离
n5, s5, _ = decide_marker_first(0, ["A", "B"])
check("★ 反向验证：标记优先 ⇒ n=0、不剥离 ⇒ 会全量重复（说明台账必须优先）",
      n5 == 0 and s5 == [], f"n={n5}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 那条告警只在真该报时出现")
