"""★★★ 用**真实的 `_emit_segment`** 跑完整链路，证明不再重复发送。

这是对线上现象（多段回复最后一段整条重复）的收口测试。

为什么要"真实"：
  前一个测试里，我让 emit 回调**抛异常**来模拟故障 —— 但生产里的 emit
  是插件的 `_emit_segment`，**它现在永不抛异常**（投递之后的杂事都各自 try 住）。
  所以真正的判据是：**无论投递之后发生什么，已发段数都必须等于实际投递数。**

本测试构造最刁钻的情况：**投递成功、但之后的每一步都抛异常**
（记账抛、`_mark_sent` 抛、补广播抛、统计抛），然后看：
  ① `_emit_segment` 是否仍然准确返回"已投递"
  ② 发送层剥离后，框架有没有重发任何已发段
  ③ 没投递的段有没有被完整交回框架（不丢内容）
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

from core.chat import MessageChain                              # noqa: E402
from core.message_manager import MessageProcessor               # noqa: E402

main_mod = _env.load("main")
es = _env.load("early_sent")
se = _env.load("stream_engine")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Result:
    def __init__(self, mid, ok=True, err=None):
        self.message_id = mid
        self.ok = ok
        self.err = err


class FakeMP:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self, plugin):
        self.plugin = plugin
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        segs = re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S)
        out = []
        for s in segs:
            c = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                c.text(s)
            out.append(c)
        return out

    async def send_message_chain(self, sid, chain):
        txt = "".join(str(e) for e in chain.message_list)
        self.sent.append(txt)
        return Result("E%d" % len(self.sent))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev1"


print("═══ 0) 真实 _emit_segment：投递成功、但之后的每一步都抛异常")
plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.patches.uninstall_all()
plugin.early_send = True
plugin._resp_by_sid = {}
plugin._early_results = {}
plugin._last_seg_ts = {}
plugin._sent_ledger = {}
plugin._sent_once = set()

mp = FakeMP(plugin)


class Ctx:
    message_processor = mp


plugin.ctx = Ctx()

ctx = main_mod.SendCtx("test:gm:1", Ev(), object())   # tag_set 只需非空（FakeMP 不真用）
SEG = "<msg><text>第1段</text></msg>"

# 让"投递之后的杂事"全部抛异常：记账 / 记时刻 / 统计
plugin._mark_sent = lambda c: (_ for _ in ()).throw(RuntimeError("mark_sent 炸了"))


def _boom_dict(*a, **k):
    raise RuntimeError("stats 炸了")


class BoomDict(dict):
    def __getitem__(self, k):
        raise RuntimeError("stats 取值炸了")

    def __setitem__(self, k, v):
        raise RuntimeError("stats 写入炸了")


plugin._stats = BoomDict(early_sent=0, first_seg_s=None)

ok = asyncio.run(plugin._emit_segment(SEG, ctx))
check("★ 投递成功 ⇒ 返回 True（即使之后的记账/统计全炸）", ok is True, f"返回 {ok!r}")
check("★ 消息确实发出去了（一次，不多不少）", len(mp.sent) == 1, str(mp.sent))
check("★ 没有向上抛异常（否则引擎会误判成没发 ⇒ 重复）", True)

print()
print("═══ 1) 真实 _emit_segment：解析失败（真没投递）⇒ 返回 False 且不抛")
ctx2 = main_mod.SendCtx("test:gm:1", Ev(), object())
plugin2 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin2.early_send = True
plugin2._stats = {"early_sent": 0, "first_seg_s": None}
mp2 = FakeMP(plugin2)


class Ctx2:
    message_processor = mp2


plugin2.ctx = Ctx2()


async def _parse_boom(xml, tag_set):
    raise ValueError("XML 坏了")


mp2._parse_xml_msg = _parse_boom
ok2 = asyncio.run(plugin2._emit_segment(SEG, ctx2))
check("★ 解析失败 ⇒ 返回 False（这一段没投递，要交回框架）", ok2 is False, f"返回 {ok2!r}")
check("★ 且确实什么都没发出去", len(mp2.sent) == 0, str(mp2.sent))

print()
print("═══ 2) 真实 _emit_segment：上下文不完整 ⇒ False，不抛")
ok3 = asyncio.run(plugin2._emit_segment(SEG, None))
check("上下文为 None ⇒ False（交回框架，不崩）", ok3 is False, f"返回 {ok3!r}")

print()
print("═══ 3) ★ 全链路：引擎按返回值记账 + 发送层剥离 ⇒ 零重复")
FULL = "".join("<msg><text>第%d段</text></msg>" % i for i in range(1, 7))
plugin3 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin3.patches.uninstall_all()
plugin3.early_send = True
plugin3._resp_by_sid = {}
plugin3._early_results = {}
plugin3._last_seg_ts = {}
plugin3._sent_ledger = {}
plugin3._sent_once = set()
mp3 = FakeMP(plugin3)


class Ctx3:
    message_processor = mp3


plugin3.ctx = Ctx3()
ctx3 = main_mod.SendCtx("test:gm:1", Ev(), object())

# 前 3 段正常抢发；第 4 段"投递成功但杂事炸"；之后不再抢发
real_emit = plugin3._emit_segment
state = {"n": 0}


async def emit_picky(seg):
    state["n"] += 1
    if state["n"] == 4:
        # 投递成功，然后把之后的杂事弄炸（模拟最刁钻的情形）
        ctx_tmp = main_mod.SendCtx("test:gm:1", Ev(), object())
        orig_mark = plugin3._mark_sent
        plugin3._mark_sent = lambda c: (_ for _ in ()).throw(RuntimeError("炸"))
        try:
            return await real_emit(seg, ctx_tmp)
        finally:
            plugin3._mark_sent = orig_mark
    if state["n"] >= 5:
        return False
    return await real_emit(seg, ctx3)


async def fake_stream():
    for i in range(0, len(FULL), 11):
        yield type("C", (), {"delta_text": FULL[i:i + 11], "delta_reasoning": "",
                             "tool_calls_delta": None, "usage": None})()


class FakeClient:
    def chat_stream(self, request, **kw):
        return fake_stream()


async def run_engine():
    eng = se.StreamEngine(force_stream=True, emit=emit_picky)
    return await eng.run(FakeClient(), object())


resp = asyncio.run(run_engine())
delivered = list(mp3.sent)                     # 抢先发出去的段
early = es.early_sent_count(resp)
print(f"     抢先发出 {len(delivered)} 段；引擎记为已发 {early} 段")
check("★★ 已发段数 == 实际投递数（这是不重复的根本保证）",
      early == len(delivered), f"delivered={len(delivered)} counted={early}")

remaining = es.strip_early_sent(FULL, early)
sent_bodies = set(re.findall(r"第\d段", " ".join(delivered)))
remain_bodies = set(re.findall(r"第\d段", remaining))
overlap = sent_bodies & remain_bodies
check("★★ 框架要发的段 与 已抢发的段 **零重叠**（不会重复）",
      not overlap, f"重叠={sorted(overlap)}")
check("★ 没抢发的段全部交回框架（不丢内容）",
      remain_bodies == {f"第{i}段" for i in range(1, 7)} - sent_bodies,
      f"交回={sorted(remain_bodies)} 已发={sorted(sent_bodies)}")

print()
print("═══ 4) 反向验证：把『投递成功但抛异常就漏记』的旧行为放回去，必须能复现重复")
# 旧行为：emit 抛异常 ⇒ 引擎 mark_failed ⇒ 那一段不计入 emitted
old_ok = None


async def emit_old_style(seg):
    """旧形状：投递成功，然后抛异常（引擎会漏记这一段）。"""
    mp3.sent.append("第X段")           # 已经发出去了
    raise RuntimeError("投递之后抛异常")


async def run_old():
    eng = se.StreamEngine(force_stream=True, emit=emit_old_style)
    try:
        return await eng.run(FakeClient(), object())
    except Exception as e:             # noqa: BLE001
        return e


r_old = asyncio.run(run_old())
old_early = es.early_sent_count(r_old) if hasattr(r_old, "__dict__") else 0
check("★ 反向验证：抛出型 emit 会被漏记（说明这个检查不是恒真）",
      old_early == 0, f"记为已发 {old_early} 段，而实际已发出内容 ⇒ 会重复")
print("     ⇒ 这正是修前的形状；修后 `_emit_segment` 永不抛异常，所以不会发生。")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 已发段数与实际投递严格一致，不会重复发送")
