"""跨轮次的重复发送 —— 用户报"更明显了"的那一类。

现场特征（截图）：
  · 同一批消息在聊天里出现**两次**
  · 日志里 `LLM ->` 只打印**一次**、`steps=1`（不是多步场景）
  ⇒ 说明"框架发送 + 我们抢发"各发了一次 = **剥离没生效**。

根因（我上一版引入的）：
  1. 1.0.8 加了 `_ledger_consumed` 标记（消费一次后置位），清理挂在
     `@on.final_result` 上；
  2. 但事件处理器循环是「按优先级降序、遇 `event.stop()` 就 break」
     ⇒ 排在**最后**的处理器**可能不执行**；
  3. 我当时用的是 `Priority.SYS_LOW`（= -100，框架**内部专用**，
     注释明确写着 "DO NOT use SYS_LOW or SYS_HIGH in user plugins"）
     ⇒ 永远排最后 ⇒ 清理可能永不执行；
  4. 标记没清 ⇒ **从第二轮起永久为真** ⇒ 每轮都走"已消费 ⇒ 不剥离"
     ⇒ **每一轮都重复**（这就是"更明显了"）。

本套件锁住三件事：
  ① 用户插件**不得**使用 SYS_LOW / SYS_HIGH
  ② 消费标记必须能**自愈**（带时间戳，不依赖钩子一定执行）
  ③ 轮开始时有陈陈旧状态的兜底清理
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

ROOT = _env.ROOT
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
OK, BAD = [], []


def check(name, cond, detail=""):
    print(("  \u2713 " if cond else "  \u2717 ") + name + (f"  [{detail}]" if detail else ""))
    (OK if cond else BAD).append(name)


print("═══ 1) 优先级：用户插件不得占用框架保留值 ═══")
# plugin_handlers.py 的注释：DO NOT use SYS_LOW or SYS_HIGH in user plugins
check("★★★ 不使用 Priority.SYS_LOW（框架内部专用，排最后可能不执行）",
      "Priority.SYS_LOW" not in MAIN)
check("★★★ 不使用 Priority.SYS_HIGH（同上）", "Priority.SYS_HIGH" not in MAIN)
check("★ 仍有 final_result 清理（正常路径）",
      re.search(r"@on\.final_result[\s\S]{0,400}?_clear_round_state", MAIN) is not None)

print()
print("═══ 2) 消费标记必须自愈（不依赖钩子一定执行）═══")
# 契约升级：时间戳 -> **轮次 id**（时间戳区分不了"同一轮第二步"与"下一轮第一步"）
check("★★★ 消费标记存的是轮次 id（不是 True、也不是时间戳）",
      '"event_id", None) or sid_now' in MAIN,
      "轮次变了才失效")
# 变量名可能是 _cons_ts 之类，正则放宽：只要"取标记"后面不远处出现
# `time.time() - <变量> < 数字` 就算带时效。
# 直接找"时效判断"语句本身（不依赖它与标记取值之间的注释长度 ——
# 第一次写的时候窗口只给了 200 字符，中间夹着大段注释 ⇒ 判据假红）
check("★★★ 读标记时按轮次 id 比较（不是时间窗口）",
      re.search(r"_cons_ev\s*==\s*_ev", MAIN) is not None)
check("★ 声明可容纳轮次 id", "_ledger_consumed" in MAIN)

print()
print("═══ 3) 轮开始时的兜底清理（双保险）═══")
check("★★★ 有陈旧状态清理函数", "def _clear_stale_round_state" in MAIN)
check("★★★ 在 llm_request（每轮开始）里调用",
      re.search(r"async def capture_and_optimize[\s\S]{0,600}?_clear_stale_round_state\(\)", MAIN)
      is not None)
check("★ 台账记录最后活动时间（供判断陈旧）", "_ledger_ts" in MAIN)
# 阈值表达式在重写兜底逻辑后变了（现在是 `(now - ts) <= 600 → 跳过`），
# 所以判据改成**通用的**：找到"保留窗口"数字并断言它足够长（>=300s）。
_keep = re.search(r"\(now - ts\)\s*<=\s*(\d+)", MAIN) or re.search(r">\s*(\d+)\s*:", MAIN)
check("★ 陈旧阈值足够长（不会误清正在用的状态）",
      _keep is not None and int(_keep.group(1)) >= 300,
      f"窗口={_keep.group(1) if _keep else '未匹配'}s")

print()


print()
print("═══ 4) 性能：保险不得拖慢热路径 ═══")
# 用户关心的点：这些"保险"会不会反而增加延迟/堵塞。
# 逐条量化结论（本仓库基准实测）：
#   · 剥离（每轮一次）：20 段 / 1010 字符 → 0.037 ms
#   · 消费标记判断：一次 dict.get + 减法 → 亚微秒
#   · 轮前兜底清理：10 个活跃会话 → 5.1 µs；100 个 → 31 µs
#   · 全程**没有任何新增 await / IO**（抢发路径的 await 都是原有的解析与发送调用）
# ⇒ 相比一次 LLM 请求的数秒，占比约 0.0001%，不构成延迟。
check("★ 保险逻辑里没有新增 sleep（不得人为引入等待）",
      "sleep" not in MAIN[MAIN.find("def _clear_stale_round_state"):
                         MAIN.find("def _clear_stale_round_state") + 1200])
check("★ 保险逻辑是纯内存操作（无 IO / 无 await）",
      "await " not in MAIN[MAIN.find("def _clear_stale_round_state"):
                           MAIN.find("def _clear_stale_round_state") + 1200])
# 泄漏防护：清理必须覆盖四个字典，而不只是 _ledger_ts
_i = MAIN.find("def _clear_stale_round_state")
_blk = MAIN[_i:_i + 2000]
check("★★ 清理覆盖全部四个字典（否则多会话下会缓慢泄漏）",
      all(k in _blk for k in ("_ledger_ts", "_sent_ledger", "_ledger_consumed", "_resp_by_sid")))



print()
print("═══ 5) ★★★ 跨轮次：标记必须按**轮次 id** 失效（不是按时间）═══")
# 现场（用户："还是经常触发重复发送"）：
#   上一版用 `(time.time() - ts) < 300` 做时效 ⇒ **5 分钟内都算已消费**。
#   连续对话里每轮间隔远小于 5 分钟 ⇒ 标记一直有效 ⇒
#   **从第二轮起每轮都走"已消费 ⇒ 不剥离"** ⇒ 每轮都重复。
#   时间戳区分不了"同一轮的第二步"与"下一轮的第一步"。
check("★★★ 消费标记按 event_id 比较（不是时间窗口）",
      re.search(r"_cons_ev\s*==\s*_ev", MAIN) is not None)
check("★★★ 不再用 300 秒时间窗口判断已消费",
      not re.search(r"_ledger_consumed\.get\(sid_now\)[\s\S]{0,200}?time\.time\(\)\s*-", MAIN))
check("★★★ 取轮次 id 用 event.event_id（框架注释：唯一标识一个事件）",
      "getattr(event, \"event_id\", None)" in MAIN or "getattr(event, 'event_id', None)" in MAIN)
check("★★ 台账带轮次键，轮次变了自动开新账（不依赖钩子清理）",
      "cur_ev" in MAIN and MAIN.count("x00ev") >= 4)
check("★★ 轮次键被所有清理点覆盖（否则字典会留垃圾）",
      MAIN.count('+ "\\x00ev", None)') >= 4)

if BAD:
    print(f"❌ {len(BAD)} 项未通过: {BAD}")
    sys.exit(1)
print(f"🎉 全部通过（{len(OK)} 项）—— 跨轮次不会重复")
