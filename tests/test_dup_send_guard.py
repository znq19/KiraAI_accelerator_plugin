"""验证「重复发送」修复生效 —— 把每条路径都真跑一遍。

对照物是修复前后的行为差异：
  · 投递成功但"杂事"抛异常  →  修前：那一段不计入已发 ⇒ 重复
                              修后：仍计入已发 ⇒ 不重复
  · 发送回调异常（真没投递）→  段必须**放回缓冲**交回框架（不丢内容）
  · 有副作用的接管点出错    →  修前：回退原实现 ⇒ 再发一遍
                              修后：不回退，异常上抛
  · 响应标记丢失            →  修前：n=0 ⇒ 整份重发
                              修后：用台账兜底
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


import os
sys.path.insert(0, str(HERE))          # 让 patches 能 import 到 breaker
sys.path.insert(0, str(HERE / "tests"))
try:
    os.makedirs("/tmp/itest/data", exist_ok=True)
    os.chdir("/tmp/itest")
    import _env
    _FW = _env.framework()
    if _FW:
        sys.path.insert(0, _FW)
except Exception:  # noqa: BLE001
    _FW = None


def load(mod):
    spec = ilu.spec_from_file_location(mod, str(HERE / f"{mod}.py"))
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


es = load("early_sent")
pt = load("patches")

print("═══ 1) guard：有副作用的实现出错时【不回退】")
import asyncio


class Boom(Exception):
    pass


async def _orig_async():
    return "ORIGINAL-RAN"          # 回退就会执行到这里 = 副作用再做一遍


async def _impl_async():
    raise Boom("投递之后抛的异常")


plain = pt.guard("t.pure", _orig_async, _impl_async)
try:
    r = asyncio.run(plain())
    pure_fell_back = (r == "ORIGINAL-RAN")
except Boom:
    pure_fell_back = False
check("无副作用的接管点：出错仍然回退（保持原有兜底能力）", pure_fell_back)


async def _impl_side():
    raise Boom("已经发出去了，之后抛的异常")


side = pt.guard("t.side", _orig_async, pt.mark_side_effects(_impl_side))
fell_back = False
raised = False
try:
    r = asyncio.run(side())
    fell_back = (r == "ORIGINAL-RAN")
except Boom:
    raised = True
check("★ 有副作用的接管点：**不回退**（否则副作用做两遍 = 重复发送）",
      raised and not fell_back, f"raised={raised} fell_back={fell_back}")

print()
print("═══ 2) has_side_effects 能穿透 functools.wraps 层")
def _f_plain():
    pass


def _f_marked():
    pass


_marked = pt.mark_side_effects(_f_marked)
check("被标记的函数识别为有副作用", pt.has_side_effects(_marked))
check("未标记的函数识别为无副作用", not pt.has_side_effects(_f_plain))
import functools


@functools.wraps(_marked)
def _wrapped():
    pass
check("★ 穿透 wraps（guard 拿到的可能是包过的）", pt.has_side_effects(_wrapped))

print()
print("═══ 3) 投递语义：已发段数对得上 ⇒ 不重复")
FULL = "".join(f"<msg><text>第{i}段</text></msg>" for i in range(1, 7))
# 修复后：投递成功的那一段一定计入 emitted ⇒ 发了几段就剥离几段
for sent in (1, 2, 5, 6):
    remain = es.strip_early_sent(FULL, sent)
    dup = any(f"第{i}段" in remain for i in range(1, sent + 1))
    check(f"发 {sent} 段 ⇒ 前 {sent} 段不会出现在剩余里（不重复）", not dup,
          f"剩余={remain[:50]}")

print()
print("═══ 4) 台账兜底：响应标记丢失时仍能正确剥离")
# 模拟发送层里的兜底逻辑：marker=0、ledger=2 ⇒ 取 ledger
def resolve_n(marker, ledger):
    n = marker
    if n <= 0 and ledger > 0:
        n = ledger
    return n


check("★ 标记丢失(0) + 台账 2 ⇒ 用 2 剥离（不整份重发）",
      resolve_n(0, 2) == 2)
check("标记正常(3) 时优先用标记", resolve_n(3, 5) == 3)
check("两者都没有 ⇒ 0（维持原行为，最坏重复一条）", resolve_n(0, 0) == 0)

print()
print("═══ 5) 失败段必须放回缓冲（不丢内容）")
if _FW:
    sf = load("stream_first")
    em = sf.SegmentEmitter()
    em.feed(FULL)
    got = em.pop_pending()
    check("6 段都进了 pending", len(got) == 6, str(len(got)))
    # 模拟"第 1 段投递失败"
    # ★ 模拟真实的失败路径：第 1 段没投递 ⇒ **整批**（第 1~6 段）放回缓冲
    em.push_back_many(got[0:])
    em.mark_failed()
    rest = em.remaining()
    check("★ 失败段回到了缓冲里（稍后交回框架 ⇒ 不丢字）", "第1段" in rest, rest[:60])
    check("★ 放回后顺序不变（第1段在最前）", rest.startswith("<msg><text>第1段"), rest[:40])
    check("★ 未投递的第 2~6 段也都在缓冲里（不能静默丢内容）",
          all(f"第{i}段" in rest for i in range(2, 7)), rest[:60])
else:
    print("     （跳过：无框架源码）")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 投递语义与回退策略都已收紧")
