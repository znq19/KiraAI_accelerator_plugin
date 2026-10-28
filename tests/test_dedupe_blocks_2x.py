"""2.x send_xml_messages：同正文、仅差 reply/@ 的去重与消息 ID 对位。

1.0.92 只在 3.0 parse_xml 路径去重，2.x 仍会出现「正文 @昵称 正文」。
本套件走真实 AcceleratorPlugin._emit_segment + _install_early_sent_strip，
覆盖截图形态、后续新内容 ID、多个重复块、不同内容、开关和保守回退。

跑法：KIRA_FW=/path/to/KiraAI-2.x python3 tests/test_dedupe_blocks_2x.py
"""
import asyncio
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 2.x 框架源码（设 KIRA_FW=/path/to/KiraAI-2.x）")
WS = tempfile.mkdtemp(prefix="acc_dedupe_2x_")
os.makedirs(os.path.join(WS, "data"), exist_ok=True)
os.chdir(WS)
sys.path.insert(0, FW)

from core.message_manager import MessageProcessor  # noqa: E402

if not hasattr(MessageProcessor, "send_xml_messages"):
    _env.skip("本套件专测 2.x send_xml_messages 路径")

main_mod = _env.load("main")
es = _env.load("early_sent")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev-dedupe-2x"


class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = ""


class FakeMP:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        from core.chat import MessageChain
        out = []
        for s in re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S):
            chain = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                chain.text(s)
            out.append(chain)
        return out

    async def send_message_chain(self, sid, chain):
        text = "".join(getattr(e, "text", str(e)) for e in chain.message_list)
        self.sent.append(text)
        return Result("E%d" % len(self.sent))


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


async def stub_send_xml_messages(self, event, xml_data, tag_set):
    actions = await self._parse_xml_msg(xml_data, tag_set)
    results = []
    for action in actions:
        if not action.is_empty():
            results.append(await self.send_message_chain(event.sid, action))
    return results


MessageProcessor.send_xml_messages = stub_send_xml_messages

B1 = "<msg><text>找到啦w</text></msg>"
B2 = "<msg><reply>r</reply><at>GeneralNew</at><text>找到啦w</text></msg>"
C = "<msg><text>后续新内容</text></msg>"
DIFF_TEXT = "<msg><reply>r</reply><at>GeneralNew</at><text>不同正文</text></msg>"
DIFF_MEDIA = "<msg><reply>r</reply><at>GeneralNew</at><text>找到啦w</text><sticker>8</sticker></msg>"


def run_case(blocks, cfg=None):
    mp = FakeMP()
    plugin = main_mod.AcceleratorPlugin(Ctx(mp), cfg or {})
    plugin.early_send = True
    plugin._install_early_sent_strip()
    ev = Ev()
    ctx = main_mod.SendCtx(ev.sid, ev, object())
    original = "\n".join(blocks)
    try:
        asyncio.run(plugin._emit_segment(B1, ctx))
        results = asyncio.run(MessageProcessor.send_xml_messages(mp, ev, original, object()))
        raw = MessageProcessor._add_message_ids(original, results)
        root = ET.fromstring("<root>" + raw + "</root>")
        ids = [m.get("message_id") for m in root.findall("msg")]
        return mp.sent, ids, results
    finally:
        plugin.patches.uninstall_all()


print("## 1) 截图形态：同正文 + reply/@ 去重，后续 ID 不得错位")
sent, ids, results = run_case([B1, B2, C])
check("第二块不再发送，后续新内容仍发送", sent == [B1, C], sent)
check("去重块空占位，后续 ID 对位", ids == ["E1", "", "E2"], ids)
check("结果数保持原消息块数", len(results) == 3, len(results))

print("\n## 2) 重复块在末尾：不留坏 ID，也不多发送")
sent, ids, results = run_case([B1, B2])
check("只发送首块", sent == [B1], sent)
check("末尾去重块为空 ID 占位", ids == ["E1", ""], ids)
check("结果数保持两块", len(results) == 2, len(results))

print("\n## 3) 正文或媒体不同：绝不误删")
sent, ids, _ = run_case([B1, DIFF_TEXT])
check("不同正文仍发送", sent == [B1, DIFF_TEXT], sent)
check("不同正文 ID 正常", ids == ["E1", "E2"], ids)
sent, ids, _ = run_case([B1, DIFF_MEDIA])
check("不同媒体仍发送", sent == [B1, DIFF_MEDIA], sent)
check("不同媒体 ID 正常", ids == ["E1", "E2"], ids)

print("\n## 4) 用户关闭内容去重：保留旧行为")
sent, ids, _ = run_case([B1, B2, C], {"section_stream": {"dedupe_blocks": False}})
check("开关关闭时第二块仍发送", sent == [B1, B2, C], sent)
check("开关关闭时 ID 顺序不变", ids == ["E1", "E2", "E3"], ids)

print("\n## 5) 多个重复块：每个位置都有占位")
sent, ids, results = run_case([B1, B2, B2, C])
check("多个同文重复块全部去重", sent == [B1, C], sent)
check("多个占位后新内容 ID 仍正确", ids == ["E1", "", "", "E2"], ids)
check("结果数保持四块", len(results) == 4, len(results))

print("\n## 6) 位置映射失败：保守不去重，绝不错位")
orig_positions = es.dropped_positions
try:
    es.dropped_positions = lambda *a, **k: []
    sent, ids, _ = run_case([B1, B2, C])
finally:
    es.dropped_positions = orig_positions
check("映射失败时保留内容", sent == [B1, B2, C], sent)
check("映射失败时 ID 仍按原顺序", ids == ["E1", "E2", "E3"], ids)

print("\n## 7) 当前响应键漂移 + 旧同文缓存：仍要精确剥离")
import types
mp7 = FakeMP()
plugin7 = main_mod.AcceleratorPlugin(Ctx(mp7), {})
plugin7.early_send = True
plugin7._install_early_sent_strip()
try:
    ev7 = Ev()
    current = types.SimpleNamespace(text_response=B1)
    plugin7._resp_by_sid["stale-same-text"] = types.SimpleNamespace(text_response=B1)
    k7 = plugin7._ckey(ev7.sid, "old-turn")
    plugin7._resp_by_sid[k7] = current
    plugin7._sent_ledger[k7] = [B1]
    plugin7._early_results[k7] = [Result("E1")]
    asyncio.run(plugin7._remember_active_response(None, current))
    ev7.event_id = "new-event-id"
    results7 = asyncio.run(MessageProcessor.send_xml_messages(mp7, ev7, B1, object()))
    check("键漂移且存在旧同文缓存时不重发", mp7.sent == [], mp7.sent)
    check("当前响应仍返回已抢发结果", len(results7) == 1 and results7[0].message_id == "E1", results7)
finally:
    plugin7.patches.uninstall_all()

print("\n" + "=" * 64)
print(f"通过 {len(PASS)}，失败 {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ACC 2.x DEDUPE + ID ALIGNMENT TEST PASSED")
