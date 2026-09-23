"""★★ 回归：抢先发送必须仍然让 AFTER_XML_PARSE 钩子生效（表情独立成行）。

## 用户报告

装了 qq-enhance 后，「表情控制 → 表情独立成行」失效：表情跟着文字一起发出去。

## 根因（实测该插件的真实代码）

    @on.after_xml_parse(priority=Priority.HIGH)
    async def process_stickers(self, event, message_chains: list):
        ...
        message_chains.clear()
        message_chains.extend(new_chains)     # 就地改写

框架靠这个列表的**可变性**生效：
    actions = await self._parse_xml_msg(...)
    for h in AFTER_XML_PARSE handlers: await h.exec_handler(event, actions)
    for action in actions:  await self.send_message_chain(...)

而抢先发送绕过了 `send_xml_messages`，原来只补广播 `ON_MESSAGE_SENT`
⇒ 钩子没跑 ⇒ 表情没被拆行。

## 本测试

用一个**行为与 qq-enhance 完全一致**的钩子（把含表情的链拆成"文字一条 +
每个表情单独一条"），走**真实的 `_emit_segment`**，断言：

  1. 钩子被调用了（修前：0 次）
  2. 发出去的条数 = 拆分后的条数（表情独立成行）
  3. 表情确实是**单独一条**，没有和文字混在一起

并做**反向验证**：把广播去掉（模拟修前），必须复现"表情没被拆行"。
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
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


# ── 一个"表情"元素（qq-enhance 用的是 Sticker；这里用等价的占位实现） ──
class FakeSticker:
    """等价的"表情"元素（qq-enhance 判断的是 isinstance(e, Sticker)；
    这里只需要一个**独立的类型**，语义与 Sticker 相同：它占一条消息、不跟文字混）。"""

    type = "sticker"

    def __init__(self, sid="1"):
        self.sid = sid

    def __str__(self):
        return f"<sticker id={self.sid}/>"

    def __repr__(self):
        return self.__str__()


class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = None


class FakeMP:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.sent = []          # 每一条真正发出去的消息（字符串化）

    async def _parse_xml_msg(self, xml, tag_set):
        """把 <msg>...</msg> 解析成 MessageChain；<sticker/> 解析成 FakeSticker。

        ★ 与框架一致：**每次调用都产生新的 chain 对象**，
          插件通过改写传入的 list 来影响发送内容。
        """
        segs = re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S)
        out = []
        for s in segs:
            chain = MessageChain()
            # 文字部分
            for m in re.finditer(r"<text>(.*?)</text>", s, re.S):
                chain.message_list.append(Text(m.group(1)))
            # 表情部分
            for m in re.finditer(r"<sticker id=\"(\d+)\"/>", s):
                chain.message_list.append(FakeSticker(m.group(1)))
            out.append(chain)
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append("|".join(str(e) for e in chain.message_list))
        return Result("M%d" % len(self.sent))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev_sticker"


# ── 装一个与 qq-enhance 行为一致的钩子 ──
CALLS = {"n": 0}


async def sticker_split_hook(event, message_chains: list):
    """复刻 qq-enhance 的 process_stickers：
    把含表情的链拆成「文字一条 + 每个表情单独一条」。

    ★ 关键：它靠**就地改写** message_chains 生效 —— 这正是框架的约定。
    """
    CALLS["n"] += 1
    new_chains = []
    for chain in message_chains:
        elements = chain.message_list
        idxs = [i for i, e in enumerate(elements) if isinstance(e, FakeSticker)]
        if not idxs:
            new_chains.append(chain)
            continue
        rest = [e for i, e in enumerate(elements) if i not in idxs]
        if rest:
            new_chains.append(MessageChain(rest))
        for i in idxs:
            new_chains.append(MessageChain([elements[i]]))   # ← 表情独立成行
    message_chains.clear()
    message_chains.extend(new_chains)


print("═══ 0) 先证明：框架自己的路径下，这个钩子确实会生效")
_HOOK = EventHandler(
    event_type=EventType.AFTER_XML_PARSE,
    handler=sticker_split_hook,
    priority=Priority.HIGH,
    desc="sticker split (test)",
)
event_handler_reg.register(_HOOK)

mp0 = FakeMP()
actions0 = asyncio.run(mp0._parse_xml_msg('<msg><text>嗨</text><sticker id="7"/></msg>', None))
ev0 = Ev()
for h in event_handler_reg.get_handlers(EventType.AFTER_XML_PARSE):
    asyncio.run(h.exec_handler(ev0, actions0))
check("钩子被调用", CALLS["n"] == 1, f"调用 {CALLS['n']} 次")
check("★ 框架语义：钩子把 1 条拆成 2 条（文字 + 表情各一条）",
      len(actions0) == 2, f"实际 {len(actions0)} 条")

print()
print("═══ 1) ★ 抢先发送：必须让同一个钩子也生效")
CALLS["n"] = 0
plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.patches.uninstall_all()
plugin.early_send = True
plugin._resp_by_sid = {}
plugin._early_results = {}
plugin._last_seg_ts = {}
plugin._sent_ledger = {}
plugin._sent_once = set()
plugin._stats = {"early_sent": 0, "first_seg_s": None}
plugin._frame_delay = lambda: (0.0, 0.0)
mp = FakeMP()


class Ctx:
    message_processor = mp


plugin.ctx = Ctx()

ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
SEG = '<msg><text>嗨</text><sticker id="7"/></msg>'
ok = asyncio.run(plugin._emit_segment(SEG, ctx))

print(f"     钩子被调用 {CALLS['n']} 次；真正发出 {len(mp.sent)} 条：{mp.sent}")
check("★ 抢先发送也广播了 AFTER_XML_PARSE（修前为 0 次）", CALLS["n"] >= 1,
      f"调用 {CALLS['n']} 次")
check("★ 表情被拆成独立一条（文字与表情分开）", len(mp.sent) == 2,
      f"实际 {len(mp.sent)} 条: {mp.sent}")
check("★ 其中一条**只含表情**（这就是「独立成行」）",
      any(s.strip().startswith("<sticker") and "嗨" not in s for s in mp.sent),
      str(mp.sent))
check("★ 另一条**只含文字**（表情没有混在文字里）",
      any("嗨" in s and "<sticker" not in s for s in mp.sent), str(mp.sent))
check("投递成功返回 True", ok is True, f"返回 {ok!r}")

print()
print("═══ 2) 反向验证：不广播 AFTER_XML_PARSE 时必须复现「表情没拆行」")
CALLS["n"] = 0
mp2 = FakeMP()
plugin2 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin2.early_send = True
plugin2._stats = {"early_sent": 0, "first_seg_s": None}
plugin2._frame_delay = lambda: (0.0, 0.0)
plugin2._sent_ledger = {}


class Ctx2:
    message_processor = mp2


plugin2.ctx = Ctx2()
# 把广播换成"什么都不做" —— 精确模拟修复前的行为
async def _no_broadcast(ctx, actions):
    """精确模拟修复前：解析完直接发，不广播 AFTER_XML_PARSE。"""
    return actions


plugin2._broadcast_after_xml_parse = _no_broadcast

ctx2 = main_mod.SendCtx("test:gm:1", Ev(), object())
asyncio.run(plugin2._emit_segment(SEG, ctx2))
print(f"     钩子被调用 {CALLS['n']} 次；真正发出 {len(mp2.sent)} 条：{mp2.sent}")
check("★ 反向验证：不广播 ⇒ 钩子 0 次调用（复现修前）", CALLS["n"] == 0,
      f"调用 {CALLS['n']} 次")
check("★ 反向验证：表情与文字挤在一条里（正是用户看到的现象）",
      any("嗨" in s and "<sticker" in s for s in mp2.sent), str(mp2.sent))

print()
print("═══ 3) 钩子抛异常不能拖垮发送（容错）")


async def boom_hook(event, message_chains: list):
    raise RuntimeError("插件的钩子炸了")


# 再注册一个会炸的钩子
event_handler_reg.register(EventHandler(
    event_type=EventType.AFTER_XML_PARSE,
    handler=boom_hook,
    priority=Priority.LOW,
    desc="boom (test)",
))
mp3 = FakeMP()
plugin3 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin3.early_send = True
plugin3._stats = {"early_sent": 0, "first_seg_s": None}
plugin3._frame_delay = lambda: (0.0, 0.0)
plugin3._sent_ledger = {}


class Ctx3:
    message_processor = mp3


plugin3.ctx = Ctx3()
ctx3 = main_mod.SendCtx("test:gm:1", Ev(), object())
ok3 = asyncio.run(plugin3._emit_segment(SEG, ctx3))
check("★ 有钩子抛异常时，消息仍然发出去了", len(mp3.sent) >= 1, str(mp3.sent))
check("★ 且返回 True（投递事实不被钩子的失败改变）", ok3 is True, f"返回 {ok3!r}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 表情独立成行在抢先发送下依然生效")
