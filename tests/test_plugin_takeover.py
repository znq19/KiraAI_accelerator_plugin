"""★★★ 插件接管识别：「自发 + 删链」类插件不得再造成重复发送（2026-10-05）。

## 线上事故（用户截图 + 日志实证）

fold_fan_formatter（折扇）在 AFTER_XML_PARSE 里**自己调 send_action** 发出
穗文本 + 合并转发，然后**把链从待发列表删掉**：

    acc 抢先广播 ⇒ 折扇发第 1 份、删链 ⇒ acc 遍历空列表 ⇒ delivered=False
    ⇒ 台账不记 ⇒ 段交回框架 ⇒ 框架再派发一次同一钩子 ⇒ 折扇发第 2 份
    = 穗 ×2 + 合并转发 ×2（与截图逐条一致）

## 修复

`_emit_segment` 识别「广播前有可发链、广播后列表被清空」= **插件接管了投递**：
  · 视为已投递（记台账 ⇒ 发送层剥离把它从文本里拿掉 ⇒ 框架那遍插件见不到它）
  · 补占位 KiraIMSentResult（保持 message_id 按位置对齐）
  · 打一行 INFO（不是 error —— 这是正常的生态协作，不是故障）

安全论证（两种插件语义都不丢不重）：
  · 「自发」语义（折扇）⇒ 剥离防住第二遍 ⇒ 不重复
  · 「过滤器」语义（故意丢弃）⇒ 不剥离时框架那遍也会被同一过滤器再丢一次，
    结果一致 ⇒ 不丢内容

## 本套件

  1. ★★★ 端到端复现事故链路并验证修复：acc 抢先（折扇式钩子发 1 份）→
     发送层剥离 → 框架那遍文本里已无该段 ⇒ 钩子不再触发 ⇒ **总共只发 1 份**
  2. 反向验证：关掉接管识别（模拟修复前）⇒ 钩子被触发 2 次 = 重复发送复现
  3. 过滤器语义：只删不发的钩子 ⇒ 同样视为已投递（不丢不重）
  4. 正常钩子（不删链）⇒ 行为不变（acc 自己发，台账照记）
"""
import asyncio
import os
import re
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

from core.chat import MessageChain                             # noqa: E402
from core.chat.message_elements import Text                    # noqa: E402
from core.plugin.plugin_handlers import (                      # noqa: E402
    event_handler_reg, EventType, EventHandler, Priority,
)

main_mod = _env.load("main")
es = _env.load("early_sent")                               # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = None


class FakeMP:
    """最小 message_processor：解析 <msg> 成链 + 记录真正发出的内容。"""

    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        out = []
        for s in re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S):
            chain = MessageChain()
            for m in re.finditer(r"<text>(.*?)</text>", s, re.S):
                chain.message_list.append(Text(m.group(1)))
            out.append(chain)
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append("".join(e.text for e in chain.message_list
                                 if isinstance(e, Text)))
        return Result("M%d" % len(self.sent))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev_takeover"


def fresh_plugin(mp):
    p = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    p.patches.uninstall_all()
    p.early_send = True
    p._resp_by_sid = {}
    p._early_results = {}
    p._last_seg_ts = {}
    p._sent_ledger = {}
    p._stats = {"early_sent": 0, "first_seg_s": None}
    p._frame_delay = lambda: (0.0, 0.0)
    p.ctx = type("C", (), {"message_processor": mp})()
    return p


# ── 折扇式钩子：自己"发送"（记录到 SELF_SENT），然后把链从列表里删掉 ──
SELF_SENT = []


async def foldfan_hook(event, message_chains: list):
    """复刻 fold_fan_formatter 的契约：匹配到就**自己发**并**就地删链**。"""
    new_chains = []
    for chain in message_chains:
        text = "".join(e.text for e in chain.message_list if isinstance(e, Text))
        if "[3p]" in text:
            SELF_SENT.append(text)          # ← 插件自己发了（对 acc 隐形）
            continue                        # ← 不 append = 从列表删掉
        new_chains.append(chain)
    message_chains[:] = new_chains


SEG = "<msg><text>官人放心[3p]三叠内容[/3p]</text></msg>"
FULL = SEG  # 本轮模型完整输出

print("═══ 1) ★★★ 事故链路端到端：修复后全程只发 1 份")
SELF_SENT.clear()
_H1 = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=foldfan_hook,
                  priority=Priority.HIGH, desc="foldfan")
event_handler_reg.register(_H1)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())

    ok = asyncio.run(plugin._emit_segment(SEG, ctx))
    key = plugin._ckey("test:gm:1", "ev_takeover")

    check("插件自己发了第 1 份（穗+转发内容）", len(SELF_SENT) == 1, str(SELF_SENT))
    check("acc 自己没有再发（链已被接管删空）", mp.sent == [], str(mp.sent))
    check("★★ emit 返回 True（接管 = 已投递，修前为 False）", ok is True, f"返回 {ok!r}")
    check("★★ 台账记下了这一段（修前不记 ⇒ 剥离失效的根因）",
          plugin._sent_ledger.get(key) == [SEG], str(plugin._sent_ledger))
    check("★ 占位结果已补（保持 message_id 位置对齐）",
          len(plugin._early_results.get(key, [])) == 1
          and plugin._early_results[key][0].message_id is None
          and plugin._early_results[key][0].ok is True)

    # ── 发送层剥离（_install_early_sent_strip 的核心动作）──
    ledger = plugin._sent_ledger.get(key, [])
    remaining = es.strip_early_sent_smart(FULL, len(ledger), list(ledger))
    check("★★★ 剥离后框架那遍的文本里已不含该段", "[3p]" not in remaining, remaining)

    # ── 框架那遍：对剥离后的文本再派发同一钩子 ⇒ 折扇见不到 [3p]，不会再发 ──
    actions2 = asyncio.run(mp._parse_xml_msg(remaining, None))
    for h in event_handler_reg.get_handlers(EventType.AFTER_XML_PARSE):
        asyncio.run(h.exec_handler(Ev(), actions2))
    for a in actions2:
        if isinstance(a, MessageChain) and not a.is_empty():
            asyncio.run(mp.send_message_chain("test:gm:1", a))
    check("★★★ 全程插件只发了 1 份（修前为 2 份 = 用户截图的重复）",
          len(SELF_SENT) == 1, f"实际 {len(SELF_SENT)} 份")
finally:
    event_handler_reg.del_handler(_H1)

print()
print("═══ 2) ★★ 反向验证：关掉接管识别 ⇒ 必须复现「发 2 份」")
SELF_SENT.clear()
_H2 = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=foldfan_hook,
                  priority=Priority.HIGH, desc="foldfan2")
event_handler_reg.register(_H2)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())

    # 精确模拟修复前：广播后不做接管识别（had_sendable 恒 False ⇒ 走旧路）
    orig_emit = plugin._emit_segment

    async def old_style_emit(seg, ctx=None):
        import types
        # 旧行为 = 看不到"广播前有可发内容" ⇒ 接管识别永不触发
        p2 = plugin
        # 直接内联旧逻辑：广播 → 遍历（空）→ delivered=False
        actions = await p2.ctx.message_processor._parse_xml_msg(seg, ctx.tag_set)
        actions = await p2._broadcast_after_xml_parse(ctx, actions)
        delivered = False
        from core.chat import MessageChain as MC
        for a in actions:
            if not isinstance(a, MC) or a.is_empty():
                continue
            await p2.ctx.message_processor.send_message_chain(ctx.sid, a)
            delivered = True
        if delivered:
            p2._sent_ledger.setdefault(p2._ckey(ctx.sid, "ev_takeover"), []).append(seg)
        return delivered

    ok = asyncio.run(old_style_emit(SEG, ctx))
    key = plugin._ckey("test:gm:1", "ev_takeover")
    check("反向：旧路 emit 返回 False（没投递的错觉）", ok is False, f"返回 {ok!r}")
    check("反向：旧路台账为空 ⇒ 剥离不会发生", plugin._sent_ledger.get(key) is None)

    # 旧路下框架那遍：原文完整回来 ⇒ 钩子再触发一次
    actions2 = asyncio.run(mp._parse_xml_msg(FULL, None))
    for h in event_handler_reg.get_handlers(EventType.AFTER_XML_PARSE):
        asyncio.run(h.exec_handler(Ev(), actions2))
    check("★★ 反向成立：旧路下插件共发了 2 份（事故复现）",
          len(SELF_SENT) == 2, f"实际 {len(SELF_SENT)} 份")
finally:
    event_handler_reg.del_handler(_H2)

print()
print("═══ 3) 过滤器语义（只删不发）⇒ 同样视为已投递，不丢不重")
SELF_SENT.clear()


async def filter_hook(event, message_chains: list):
    message_chains[:] = [c for c in message_chains
                         if "违禁" not in "".join(
                             e.text for e in c.message_list if isinstance(e, Text))]


_H3 = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=filter_hook,
                  priority=Priority.HIGH, desc="filter")
event_handler_reg.register(_H3)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
    ok = asyncio.run(plugin._emit_segment("<msg><text>违禁词</text></msg>", ctx))
    key = plugin._ckey("test:gm:1", "ev_takeover")
    check("过滤器吃掉段 ⇒ emit 返回 True（已被插件处置）", ok is True, f"返回 {ok!r}")
    check("台账记账 ⇒ 框架那遍不会再见到它（与过滤器再丢一次结果一致）",
          plugin._sent_ledger.get(key) == ["<msg><text>违禁词</text></msg>"])
finally:
    event_handler_reg.del_handler(_H3)

print()
print("═══ 4) 正常钩子（不删链）⇒ 行为完全不变")
SELF_SENT.clear()
_H4 = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=foldfan_hook,
                  priority=Priority.HIGH, desc="foldfan4")
event_handler_reg.register(_H4)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
    ok = asyncio.run(plugin._emit_segment("<msg><text>普通消息</text></msg>", ctx))
    key = plugin._ckey("test:gm:1", "ev_takeover")
    check("普通段 acc 自己发出", mp.sent == ["普通消息"], str(mp.sent))
    check("钩子没自发（不含 [3p]）", SELF_SENT == [])
    check("emit 返回 True 且台账照记", ok is True and
          plugin._sent_ledger.get(key) == ["<msg><text>普通消息</text></msg>"])
    # 占位结果只该在「接管」时补；正常发送走的是真实结果
    check("正常段的结果是真实 message_id（不是占位）",
          plugin._early_results.get(key, [])[-1].message_id == "M1")
finally:
    event_handler_reg.del_handler(_H4)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 插件接管识别生效，「自发+删链」类插件不再造成重复发送")
