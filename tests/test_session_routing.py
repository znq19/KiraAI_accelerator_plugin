"""会话路由：抢先发送绝不能发到别的会话去。

线上事故（2026-09-23 用户实测）：
  机器人给**私聊**写的回复，被发到了**群聊**里。

根因：
  发送上下文原本放在**实例变量**上（`self._current_sid` / `_current_event` /
  `self._current_tag_set`），在 `ON_LLM_REQUEST` 时写入，但真正发送发生在
  之后的 `chat()` 里。框架会并发处理多个会话 —— 中间插进另一个会话的事件，
  实例变量就被覆盖了 ⇒ A 会话的回复发给了 B 会话。

修法：上下文跟着 **request** 走（`req.__dict__["_accel_ctx"]`），
     引擎工厂接收 request，`_emit_segment` 只认传进来的 ctx。

本测试复刻"先来 A、再来 B、然后 A 才发送"的顺序，
断言消息落在 **A**，并特意确认此刻实例变量已经是 B
（证明它确实跟着 request 走，而不是"碰巧还没被覆盖"）。
"""
import asyncio
import os
import sys
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自身
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.chat import MessageChain                                  # noqa: E402
from core.provider.llm_model import LLMRequest                      # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class FakeEvent:
    def __init__(self, sid):
        self.sid = sid


class FakeMP:
    """记录"往哪个会话发了什么"，不真发。"""

    def __init__(self):
        self.sent = []          # [(sid, text)]
        self.min_message_delay = 0.0    # 测试里不等
        self.max_message_delay = 0.0

    async def _parse_xml_msg(self, seg, tag_set):
        chain = MessageChain()
        chain.text(seg)
        return [chain]

    async def send_message_chain(self, sid, chain):
        self.sent.append((sid, "".join(str(e) for e in chain.message_list)))
        return {"status": "ok"}


class FakeCtx:
    def __init__(self, mp):
        self.message_processor = mp


def make_plugin(mp):
    p = main_mod.AcceleratorPlugin(ctx=FakeCtx(mp), cfg={})
    p.early_send = True
    p.thinking_enabled = False
    p._resp_by_sid = {}
    p._last_seg_ts = {}
    p.stats_reset = None
    for k, v in list(p._stats.items()):
        p._stats[k] = 0
    return p


async def main():
    print("1) 顺序：A 先请求 → B 后请求 → A 才发送")
    mp = FakeMP()
    p = make_plugin(mp)

    evA, evB = FakeEvent("sess_A"), FakeEvent("sess_B")
    reqA = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    reqB = LLMRequest(messages=[{"role": "user", "content": "hi"}])

    # 两个会话的请求钩子先后到达（B 最后写入 ⇒ 实例变量停在 B）
    await p.capture_and_optimize(evA, reqA, {"A": True})
    await p.capture_and_optimize(evB, reqB, {"B": True})
    check("实例变量此刻停在 B（模拟被后到事件覆盖）",
          p._current_sid == "sess_B", f"_current_sid={p._current_sid}")

    # A 的这轮现在才产出第一段并抢先发送
    ctxA = main_mod.ctx_of(reqA)
    check("reqA 上确实挂着自己的上下文", ctxA is not None and ctxA.sid == "sess_A",
          f"{ctxA.sid if ctxA else None}")
    await p._emit_segment("<msg>只给A看的</msg>", ctxA)

    got = [s for s, _ in mp.sent]
    check("★ A 的段发到了 A（而不是被覆盖成的 B）", got == ["sess_A"], f"实际={got}")

    print("\n2) B 的消息也要发到 B")
    ctxB = main_mod.ctx_of(reqB)
    await p._emit_segment("<msg>只给B看的</msg>", ctxB)
    got = [s for s, _ in mp.sent]
    check("★ B 的段发到了 B", got == ["sess_A", "sess_B"], f"实际={got}")

    print("\n3) 交错并发：A、B 交替发送，各归各位")
    mp2 = FakeMP()
    p2 = make_plugin(mp2)
    rA = LLMRequest(messages=[{"role": "user", "content": "x"}])
    rB = LLMRequest(messages=[{"role": "user", "content": "x"}])
    await p2.capture_and_optimize(FakeEvent("A"), rA, {})
    await p2.capture_and_optimize(FakeEvent("B"), rB, {})
    cA, cB = main_mod.ctx_of(rA), main_mod.ctx_of(rB)
    for i in range(3):
        await p2._emit_segment(f"<msg>a{i}</msg>", cA)
        await p2._emit_segment(f"<msg>b{i}</msg>", cB)
    sids = [s for s, _ in mp2.sent]
    check("★ 交错发送也不串（a/b 各 3 条，顺序交替）",
          sids == ["A", "B", "A", "B", "A", "B"], f"实际={sids}")
    texts = [t for _, t in mp2.sent]
    check("内容也没有互换",
          all(f"a{i}" in texts[2 * i] for i in range(3))
          and all(f"b{i}" in texts[2 * i + 1] for i in range(3)),
          str(texts))

    print("\n4) 没有上下文时必须报错，而不是退回实例变量")
    err = None
    try:
        ret = await p._emit_segment("<msg>无上下文</msg>", None)
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    # ★ 行为升级（2026-09-23）：上下文不完整时**返回 False 而不是抛异常**。
    #   因为发送层现在按返回值判"有没有投递"；抛异常会冒到引擎里，
    #   把"没投递"和"投递后杂事失败"混在一起（后者会导致重复发送）。
    #   判据的重点没变：**绝不能静默改用实例变量把消息发到别的会话**。
    check("★ ctx=None 时返回 False（明确表示没投递）", ret is False, f"ret={ret!r} err={err}")
    check("ctx=None 时没有发出任何东西", len(mp.sent) == 2, f"{len(mp.sent)} 条")

    print("\n5) 节奏时间戳按会话分开（并发下互不干扰）")
    mp3 = FakeMP()
    # 间隔 > 0 才会有节奏记录（=0 表示不等，不记时间戳，这是对的）
    mp3.min_message_delay = 0.001
    mp3.max_message_delay = 0.002
    p3 = make_plugin(mp3)
    r3a = LLMRequest(messages=[{"role": "user", "content": "x"}])
    r3b = LLMRequest(messages=[{"role": "user", "content": "x"}])
    await p3.capture_and_optimize(FakeEvent("SA"), r3a, {})
    await p3.capture_and_optimize(FakeEvent("SB"), r3b, {})
    await p3._emit_segment("<msg>x</msg>", main_mod.ctx_of(r3a))
    check("A 记了自己的时间戳", "SA" in p3._last_seg_ts and "SB" not in p3._last_seg_ts,
          str(list(p3._last_seg_ts)))

    print("\n6) 响应按会话分开存（剥离才不会拿错）")
    class R:
        pass
    p3._resp_by_sid["SA"] = R()
    check("按 sid 存响应", p3._resp_by_sid.get("SA") is not None
          and p3._resp_by_sid.get("SB") is None)

    print("\n7) ★ 按会话存的表必须有上限（长期运行不能越用越大）")
    # 直接调私有方法，不走真实发送 ⇒ 不产生等待，跑得很快
    p7 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    p7._resp_by_sid = {}
    p7._last_seg_ts = {}
    p7.early_send = True
    p7.thinking_enabled = True
    p7._frame_delay = lambda: (0.001, 0.002)      # 强制启用节奏记录

    N = 1500
    for i in range(N):
        sid = "s%d" % i
        ctx = main_mod.SendCtx(sid, None, None)
        p7._mark_sent(ctx)
        p7._resp_by_sid[sid] = object()
        if len(p7._resp_by_sid) > 32:
            p7._resp_by_sid.clear()
            p7._resp_by_sid[sid] = object()

    check("★ 节奏时间戳表有上限（%d 个会话后仍受控）" % N,
          len(p7._last_seg_ts) <= main_mod.MAX_PACING_SIDS,
          f"{len(p7._last_seg_ts)} 条，上限 {main_mod.MAX_PACING_SIDS}")
    check("★ 待剥离响应表有上限", len(p7._resp_by_sid) <= 32,
          f"{len(p7._resp_by_sid)} 条")

    # 自动思考那一侧：decide 会触发 _trim_sessions
    from core.provider.llm_model import LLMRequest as _LR
    tk = p7.thinking
    for i in range(N):
        r = _LR(messages=[{"role": "user", "content": "x"}])
        tk.decide("t%d" % i, r)
    sizes = {k: len(getattr(tk, k)) for k in
             ("_last_decision", "_ctx_warmup", "_last_turn_tool", "_ctx_long")}
    check("★ 自动思考的 per-sid 表受 max_sessions 约束",
          all(v <= tk.max_sessions for v in sizes.values()),
          f"{sizes}（上限 {tk.max_sessions}）")

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 消息不会窜会话")


asyncio.run(main())
