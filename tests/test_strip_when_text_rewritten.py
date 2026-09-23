"""★★ 回归：别的插件改写了 text_response 之后，剥离仍然正确（不重复、不丢内容）。

用户点名要查的插件：`KiraAI_xml_tag_fixer_plugin`（znq19）。
它的 `on_llm_response`（Priority.HIGH）会改写 `resp.text_response`：

    fixed = self.fix_xml(original)
    if fixed != original:
        resp.text_response = fixed

而 `fix_xml` 会改变 `<msg>` 的**数量**：
  · 补全缺失的 `<msg>`（模型忘了写标签时，0 个 → N 个）
  · 空行分段（`split_blank_line_messages`，默认关）：**1 条 → N 条**
  · 跨消息标记对合并（`_merge_marker_spanning_blocks`）：**多条 → 1 条**
  · 裸特殊字符转义（`_escape_code_fences`）：内容里的 `&`/`<` 变成实体

我们原来的剥离是 `strip_early_sent(文本, n)`，`n` 是按**原始**文本数的段数。
文本被改写后 `n` 就对不上：**少切 ⇒ 重复发送；多切 ⇒ 丢内容**。

本测试把每种改写都构造出来，断言剥离结果正确。
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


spec = ilu.spec_from_file_location("early_sent", str(HERE / "early_sent.py"))
es = ilu.module_from_spec(spec)
spec.loader.exec_module(es)

# 真实形状：模型写了 4 段，前 2 段已抢发
E1 = "<msg><text>第一段</text></msg>"
E2 = "<msg><text>第二段</text></msg>"
T3 = "<msg><text>第三段</text></msg>"
T4 = "<msg><text>第四段</text></msg>"
SENT = [E1, E2]
ORIG = E1 + E2 + T3 + T4

print("═══ 0) 基线：文本没被改写 ⇒ 与旧行为一致")
got = es.strip_early_sent_smart(ORIG, 2, SENT)
check("剥掉前 2 段，剩第 3、4 段", got == T3 + T4, got)

print()
print("═══ 1) ★ xml_tag_fixer 的「空行分段」：1 条被拆成多条")
# 场景：抢发出去的第 2 段是一条含空行的长消息，被插件拆成 3 条
E2_MULTI = "<msg><text>甲\n\n乙\n\n丙</text></msg>"
SENT2 = [E1, E2_MULTI]
ORIG2 = E1 + E2_MULTI + T3
# 插件改写后（第 2 段变成 3 条）
FIXED2 = E1 + "<msg><text>甲</text></msg><msg><text>乙</text></msg><msg><text>丙</text></msg>" + T3
got2 = es.strip_early_sent_smart(FIXED2, 2, SENT2)
check("★ 1 条被拆成 3 条后，3 条都被剥掉（不重复）",
      "甲" not in got2 and "乙" not in got2 and "丙" not in got2, got2)
check("★ 未发的第 3 段仍保留（不丢内容）", "第三段" in got2, got2)

print()
print("═══ 2) 对照：仍用旧的「按序号」剥离会怎样")
old2 = es.strip_early_sent(FIXED2, 2)      # 只切 2 块
check("★ 旧写法只切 2 块 ⇒ 「丙」被漏下 ⇒ 会被重复发送",
      "丙" in old2, f"剩余={old2[:70]}")

print()
print("═══ 3) ★ xml_tag_fixer 的「跨消息合并」：多条被并成 1 条")
SENT3 = [E1, E2]
ORIG3 = E1 + E2 + T3
# 插件把 前两段合并成一条
FIXED3 = "<msg><text>第一段第二段</text></msg>" + T3
got3 = es.strip_early_sent_smart(FIXED3, 2, SENT3)
check("★ 两条被并成一条后，那一条被剥掉（不重复）",
      "第一段" not in got3 and "第二段" not in got3, got3)
check("★ 未发的第 3 段仍保留", "第三段" in got3, got3)

print()
print("═══ 4) ★ 转义实体：内容里的 & 变成 &amp;")
SENT4 = ['<msg><text>A & B</text></msg>']
ORIG4 = SENT4[0] + T3
FIXED4 = '<msg><text>A &amp; B</text></msg>' + T3
got4 = es.strip_early_sent_smart(FIXED4, 1, SENT4)
check("★ 转义后仍能匹配上（不重复）", "A " not in got4.replace("第三段", ""), got4)
check("★ 未发段保留", "第三段" in got4, got4)

print()
print("═══ 5) ★ 补全缺失的 <msg>：模型没写标签（0 个 → N 个）")
# 抢发出去的是"裸文本"段（模型没写 <msg>），插件补上了 <msg><text>
SENT5 = ["第一段"]
ORIG5 = "<msg><text>第一段</text></msg>" + T3
got5 = es.strip_early_sent_smart(ORIG5, 1, SENT5)
check("★ 补全标签后仍能剥掉（不重复）", "第一段" not in got5, got5)
check("★ 未发段保留", "第三段" in got5, got5)

print()
print("═══ 6) 匹配不上时：退回按序号（不静默失效）")
SENT6 = ["完全不同的内容"]
got6 = es.strip_early_sent_smart(ORIG, 2, SENT6)
check("★ 匹配不上 ⇒ 退回按序号切 2 段（保持旧行为，不会反而丢内容）",
      got6 == T3 + T4, got6)

print()
print("═══ 7) 全部发完 ⇒ 返回 <msg/>（框架不发任何东西）")
got7 = es.strip_early_sent_smart(ORIG, 4, [E1, E2, T3, T4])
check("全发完 ⇒ <msg/>", got7 == "<msg/>", got7)

print()
print("═══ 8) 已发段原文可从响应读回（发送层要用）")


class R:
    pass


r = R()
es.mark_early_sent(r, SENT, ORIG)
check("★ 响应的私有属性里存了已发段原文",
      es.early_sent_segments(r) == SENT, str(es.early_sent_segments(r)))
check("段数仍然可读（向后兼容）", es.early_sent_count(r) == 2)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 文本被改写后剥离依然正确")
