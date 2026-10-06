"""★★ 交回护栏：RootTagAction 与 is_stopped 不得被抢发路径吞掉（2026-10-06，v1.0.80）。

## 修复的两个问题

### ① RootTagAction 静默跳过（潜伏）

`_emit_segment` 的发送循环只处理 `MessageChain`：

    for action in actions:
        if not isinstance(action, MessageChain) or action.is_empty():
            continue          # ← RootTagAction 在这里被静默跳过

若一段是「链 + root 动作」混合：链被发出、**整段记了台账** ⇒ 框架发送层把这段
从文本里剥离 ⇒ root 动作（`tag.handle(...)`，如 root 级功能标签）**永远不被执行**。
框架的 send_xml_messages 本来会执行它（message_manager.py:931）。

修复：解析后、广播前发现 RootTagAction ⇒ 整段 `return False` 交回框架
（钩子也只该在框架那遍见到这段，不该被执行两次）。

### ② AFTER_XML_PARSE 阶段 is_stopped 语义分歧

框架：任一 handler 后 `event.is_stopped` ⇒ `return None`，**整批不发**。
旧版抢发：`_broadcast_after_xml_parse` 只 break 出循环，`_emit_segment`
**仍照常发送** ⇒ 内容过滤/拦截类插件的「停止」被抢发路径无视。

修复：广播返回后检查 `is_stopped` ⇒ `return False` 交回框架，
由框架走完「停止 ⇒ 不发」的统一逻辑。

## 本套件

  1. 含 RootTagAction 的段 ⇒ 交回（return False / 未发送 / 未广播 / 未记台账）
  2. 广播中被 stop ⇒ 交回（return False / 链仍在也没发 / 未记台账）
  3. 回归：正常文本段不受影响（照发、照记台账）
  4. 反向验证：模拟旧行为（无两个护栏）⇒ ① 链被发、root 动作被吞且台账误记
     ② 被 stop 仍然发出 —— 两个 bug 复现
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
# ★ 必须排在 core.plugin 之后：core.tag.base 反向 import core.plugin，
#   先 import core.tag 会撞上循环导入（实测）。
from core.tag import RootTagAction                             # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []

def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))

class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = None

class DummyTag:
    """最小 root 标签：只承载身份，不真正执行。"""
    name = "dummy_root"
    parent = None

ROOT_SEG = "<msg><text>前文</text></msg><dummy_root>x</dummy_root>"
TEXT_SEG = "<msg><text>普通一句话</text></msg>"

class FakeMP:
    """最小 message_processor：普通段解析成链；ROOT_SEG 额外附一个 RootTagAction。"""

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
        if "<dummy_root>" in xml:
            out.append(RootTagAction(tag=DummyTag(), value="x", attrs={}))
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append("".join(e.text for e in chain.message_list
                                 if isinstance(e, Text)))
        return Result("M%d" % len(self.sent))

class Ev:
    sid = "test:gm:1"
    event_id = "ev_handback"
    def __init__(self):
        self.is_stopped = False

def fresh_plugin(mp):
    p = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    p.patches.uninstall_all()
    p.early_send = True
    p._resp_by_sid = {}
    p._early_results = {}
    p._last_seg_ts = {}
    p._sent_ledger = {}
    p._ledger_ts = {}
    p._stats = {"early_sent": 0, "first_seg_s": None}
    p._frame_delay = lambda: (0.0, 0.0)
    p.ctx = type("C", (), {"message_processor": mp})()
    return p

def ledger_of(p):
    return p._sent_ledger.get(p._ckey("test:gm:1", "ev_handback"), [])

# 计数钩子：记录 AFTER_XML_PARSE 被广播了几次
BCAST = {"n": 0}
async def counting_hook(event, message_chains: list):
    BCAST["n"] += 1

# 停止钩子：模拟「内容过滤」插件 —— 在 AFTER_XML_PARSE 里叫停事件
async def stopping_hook(event, message_chains: list):
    event.is_stopped = True

print("═══ 1) 含 RootTagAction 的段 ⇒ 必须整段交回框架")
_H = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=counting_hook,
                  priority=Priority.HIGH, desc="count")
event_handler_reg.register(_H)
try:
    BCAST["n"] = 0
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())

    ok = asyncio.run(plugin._emit_segment(ROOT_SEG, ctx))

    check("★ emit 返回 False（交回框架，修前会把链发出去还记台账）",
          ok is False, f"返回 {ok!r}")
    check("★ 链没有被抢发（root 动作若随之被吞就是丢功能）",
          mp.sent == [], str(mp.sent))
    check("★ 没有广播 AFTER_XML_PARSE（钩子只该在框架那遍见到这段）",
          BCAST["n"] == 0, f"广播 {BCAST['n']} 次")
    check("★ 未记台账（框架那遍会完整重发这段，root 动作得以执行）",
          ledger_of(plugin) == [], str(ledger_of(plugin)))
finally:
    event_handler_reg.del_handler(_H)

print()
print("═══ 2) 广播中被 stop ⇒ 必须整段交回框架（框架语义：整批不发）")
_HS = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=stopping_hook,
                   priority=Priority.HIGH, desc="stopper")
event_handler_reg.register(_HS)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ev = Ev()
    ctx = main_mod.SendCtx("test:gm:1", ev, object())

    ok = asyncio.run(plugin._emit_segment(TEXT_SEG, ctx))

    check("★★ emit 返回 False（修前：break 后仍照常发送 = 拦截被无视）",
          ok is False, f"返回 {ok!r}")
    check("★★ 没有发出任何内容（被拦截的段一个字都不能出去）",
          mp.sent == [], str(mp.sent))
    check("★ 未记台账（交回框架 ⇒ 框架那遍也会因 stop 而不发，不重复）",
          ledger_of(plugin) == [], str(ledger_of(plugin)))
finally:
    event_handler_reg.del_handler(_HS)

print()
print("═══ 3) 回归：正常文本段不受影响（照发、照记台账）")
mp = FakeMP()
plugin = fresh_plugin(mp)
ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
ok = asyncio.run(plugin._emit_segment(TEXT_SEG, ctx))
check("正常段仍抢发成功", ok is True and mp.sent == ["普通一句话"],
      f"ok={ok!r} sent={mp.sent}")
check("台账照记（剥离不受影响）", ledger_of(plugin) == [TEXT_SEG],
      str(ledger_of(plugin)))

print()
print("═══ 4) ★★ 反向验证：模拟旧行为 ⇒ 两个 bug 必须复现")
# 旧行为 = 没有两个护栏：含 root 动作的段会把链发出去、root 动作被吞、台账误记；
# 被 stop 的段仍照常发送。这里内联旧逻辑证明「问题真实存在、新护栏确实挡住」。
async def old_style_emit(plugin, seg, ctx):
    mp = plugin.ctx.message_processor
    actions = await mp._parse_xml_msg(seg, ctx.tag_set)
    actions = await plugin._broadcast_after_xml_parse(ctx, actions)
    delivered = False
    for a in actions:
        if not isinstance(a, MessageChain) or a.is_empty():
            continue                      # ← RootTagAction 在这里被静默跳过
        await mp.send_message_chain(ctx.sid, a)
        delivered = True
    if delivered:
        plugin._sent_ledger.setdefault(
            plugin._ckey(ctx.sid, "ev_handback"), []).append(seg)
    return delivered

_HS2 = EventHandler(event_type=EventType.AFTER_XML_PARSE, handler=stopping_hook,
                    priority=Priority.HIGH, desc="stopper2")
event_handler_reg.register(_HS2)
try:
    mp = FakeMP()
    plugin = fresh_plugin(mp)
    ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
    ok = asyncio.run(old_style_emit(plugin, ROOT_SEG, ctx))
    check("旧行为①：链被抢发而 root 动作被吞（且台账误记 ⇒ 永不执行）",
          ok is True and mp.sent == ["前文"] and ledger_of(plugin) == [ROOT_SEG],
      f"sent={mp.sent} ledger={ledger_of(plugin)}")

    mp2 = FakeMP()
    plugin2 = fresh_plugin(mp2)
    ctx2 = main_mod.SendCtx("test:gm:1", Ev(), object())
    ok2 = asyncio.run(old_style_emit(plugin2, TEXT_SEG, ctx2))
    check("旧行为②：事件已被 stop 仍照常发出（拦截被无视）",
          ok2 is True and mp2.sent == ["普通一句话"], f"sent={mp2.sent}")
finally:
    event_handler_reg.del_handler(_HS2)

print()
print("═══ 结果 ═══")
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
print("🎉 全部通过")
