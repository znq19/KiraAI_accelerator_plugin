"""自动按需思考 —— 对齐 Alife 的 OccupationNotepad 机制，并做得更好。

## Alife 是怎么做的（源码依据）

`XmlFunctionCaller.cs:114-115, 235-248` + `OpenAILanguageModel.cs:51, 219-222`：

```csharp
// 1) 模块通过思考请求器"租用"思考开关，并给出原因
thinkingOccupationMarker = ChatBot.LanguageModel.GetThinkingRequester().Rent("即将使用隐式功能");

// 2) 模型请求时统一查一次：有没有人租着？
bool thinking = GetThinkingRequester().IsOccupied;
if (thinking && !string.IsNullOrEmpty(Configuration.reasoningEffort))
    payload["reasoning_effort"] = Configuration.reasoningEffort;
string extraBody = thinking ? Configuration.extraBody : Configuration.extraBodyNotThinking;
```

触发点（`XmlFunctionCaller.cs` 内）：
- `:275` 「即将使用隐式功能」—— AI 调用某个隐式 tag 时
- `:235` 「隐式功能激活中」—— 上一轮注入过工具文档
- `:198` 「需要处理函数异常」—— 工具执行报错
- `:188` 「重新激活隐式功能」

## 我们的做法（并做出三点改进）

| # | Alife | 本实现 |
|---|---|---|
| 1 | 只按「是否要调工具」判断 | **多信号打分**：意图复杂度 + 上下文规模 + 触发词 + 上轮工具失败 + 工具数量 |
| 2 | 思考开关是全局布尔 | **按 sid 独立**，不同会话互不干扰 |
| 3 | reasoning_effort 是固定配置 | **按分数分级**（low/medium/high），并可配阈值 |
| 4 | — | **预算控制**：每分钟最多开 N 次，防止被"请思考"类提示词刷爆 token |
| 5 | — | **可观测**：面板显示本轮为什么开/不开思考 + 命中信号 |

## 怎么把开关塞进请求

KiraAI 的 `OpenAICompatibleLLMClient._build_request_kwargs()` 会读取：
```python
extra_body = model_config.get("section_advanced", {}).get("extra_body")   # model_clients.py:61
if extra_body: kwargs["extra_body"] = extra_body                           # :72-73
```
所以 **patch 这个方法**即可在不改任何配置文件的前提下注入思考参数。

⚠️ reasoning 的**字段名因厂商而异**，所以做成可配（默认 `compatible` 会自动挑一套最通用的）：
    - `{"reasoning_effort": "medium"}`   —— OpenAI o 系列 / 多数兼容网关
    - `{"enable_thinking": true}`        —— Qwen / 阿里系
    - `{"thinking": {"type": "enabled"}}` —— Anthropic / 部分网关
    - `{"chat_template_kwargs": {"enable_thinking": true}}` —— vLLM 部署的 Qwen
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

# ── 触发词：命中即认为"这轮需要想一下" ──
COMPLEX_HINTS = (
    "为什么", "怎么", "如何", "解释", "分析", "推理", "证明", "推导", "设计", "架构",
    "比较", "对比", "区别", "优缺点", "评估", "方案", "计划", "步骤", "原因",
    "why", "how", "explain", "analyze", "reason", "derive", "prove", "design",
    "compare", "versus", " vs ", "evaluate", "plan", "steps", "trade-off", "tradeoff",
    "debug", "报错", "错误", "异常", "失败", "bug", "fix", "修复", "优化", "重构",
)

# ── 简单寒暄：命中即认为"不用想"（优先于复杂词判断）──
SIMPLE_HINTS = (
    "你好", "在吗", "哈哈", "谢谢", "再见", "晚安", "早安", "早", "嗯", "好的", "收到",
    "hi", "hello", "thanks", "thank you", "bye", "good night", "ok", "okay", "lol",
)


class ThinkingDecision:
    """一次思考判定的结果。"""

    __slots__ = ("enabled", "score", "signals", "effort", "scanned_chars", "applied")

    def __init__(self, enabled: bool, score: float, signals: list[str], effort: str,
                 scanned_chars: int = 0):
        self.enabled = enabled
        self.score = score
        self.signals = signals
        self.effort = effort
        #: 本轮判定的"用户消息"有多少字 —— 用来发现"扫到了不该扫的文本"
        self.scanned_chars = scanned_chars
        #: 注入层回填的"实际动作"（"已注入开启参数"/"已注入关闭参数"/
        #: "未注入关闭参数"）。日志会带上它 —— 让"判定与实际脱节"一眼可见
        #: （2026-10-05 用户实测：日志说"思考=关"，模型却还在思考）。
        self.applied = ""

    def __repr__(self):
        return (f"ThinkingDecision(enabled={self.enabled}, score={self.score:.1f}, "
                f"effort={self.effort!r}, signals={self.signals})")


# ── 会话起步"爬升期"抑制参数 ──
# 会话刚开始时上下文会从接近 0 涨到稳定值（必然现象，不是"异常变长"）。
# 这期间基线在追赶，会持续被判为"偏离基线"⇒ 十几轮误判。
# 所以要求：基线**连续 N 轮变化都很小**后，才认为它代表"平时水平"，才开始判定。
CONTEXT_SETTLE_TURNS = 3      # 连续多少轮
CONTEXT_SETTLE_TOL = 0.05     # "变化很小" = 基线相对变化 ≤ 5%


class AutoThinkingController:
    """多信号打分的自动思考开关。

    打分规则（全部可配，默认值在 schema.json 里）：
        +2.0  用户消息命中复杂触发词
        +1.5  上一轮出现了工具执行错误
        +1.0  可用工具很多（>= tool_count_threshold，说明可能要走多步）
        +0.5  上下文很长（>= context_char_threshold，容易迷路）
        +0.5  单条用户消息很长（>= long_message_chars，信息量大）
        -3.0  命中简单寒暄（直接压到阈值下）
        score >= threshold ⇒ 开思考
    """

    def __init__(
        self,
        threshold: float = 2.0,
        long_message_chars: int = 220,
        # ── 上下文信号：相对**自己会话的基线**，不再用绝对字符数 ──
        #    绝对值对不同用户意义完全不同（新装用户 6000 就很长，
        #    装了插件的人每轮都 3 万+ ⇒ 信号永远是"真" ⇒ 等于偷偷把阈值挪走）。
        context_ratio_on: float = 1.6,     # 超过基线这个倍数 ⇒ 命中
        context_ratio_off: float = 1.15,   # 回落到这个倍数以下 ⇒ 取消命中（滞回，防抖）
        context_min_samples: int = 5,      # 攒够这么多样本才启用（否则无基线、不判定）
        # 基线 EMA 的更新系数。越大越"健忘"（单轮尖峰一过就把基线带跑，
        # 于是后续即使仍高于历史水平也不再算"长"）；越小越"稳"。
        # 0.15 = 单轮尖峰只把基线推 15% 的差值，代表"平时水平"。
        context_ema_alpha: float = 0.15,
        # ── 工具信号：换成「上一轮是否用过工具」（逐轮变化，真正的信号）──
        #    原来的「工具数 >= N」在一个会话里基本恒定，不是信号而是常数。
        score_last_turn_tool: float = 1.0,
        effort_map: Optional[dict[str, str]] = None,
        effort_low: float = 2.0,
        effort_high: float = 4.5,
        max_per_minute: int = 60,
        max_sessions: int = 500,
    ):
        self.threshold = threshold
        self.long_message_chars = long_message_chars
        self.context_ratio_on = float(context_ratio_on)
        self.context_ratio_off = float(context_ratio_off)
        self.context_min_samples = max(1, int(context_min_samples))
        self.context_ema_alpha = min(1.0, max(0.01, float(context_ema_alpha)))
        self.score_last_turn_tool = float(score_last_turn_tool)
        self.effort_low = effort_low
        self.effort_high = effort_high
        self.max_per_minute = max_per_minute
        self.effort_map = effort_map or {}

        # sid -> 状态。★ 上限保护：会话多了这些 dict 会缓慢增长，超过就清最旧。
        self._last_tool_error: dict[str, bool] = {}
        self._last_decision: dict[str, ThinkingDecision] = {}
        self._opened: dict[str, list[float]] = {}
        # 上下文基线（EMA）与"当前是否判定为长"（滞回状态）
        self._ctx_ema: dict[str, float] = {}
        self._ctx_samples: dict[str, int] = {}
        # 预热期样本缓冲（用来取中位数当种子，抗首轮尖峰）
        self._ctx_warmup: dict[str, list[float]] = {}
        # 基线是否已"稳定到能代表平时水平"（一旦成立就锁定，不再回退）
        self._ctx_ready: dict[str, bool] = {}
        self._ctx_settle: dict[str, int] = {}
        self._ctx_long: dict[str, bool] = {}
        # 上一轮是否用过工具（由 on_tool_result / on_final_result 维护）
        self._last_turn_tool: dict[str, bool] = {}
        self._turn_had_tool: dict[str, bool] = {}
        self.max_sessions = max_sessions

    # ── 外部信号接口（对标 Alife 的 Rent/Return）──
    def note_tool_error(self, sid: str) -> None:
        self._last_tool_error[sid] = True

    def note_tool_ok(self, sid: str) -> None:
        self._last_tool_error[sid] = False

    def last_decision(self, sid: str) -> Optional[ThinkingDecision]:
        return self._last_decision.get(sid)

    # ── 主判定 ──
    def decide(self, sid: str, request: Any) -> ThinkingDecision:
        signals: list[str] = []
        score = 0.0

        text = self._user_text(request)
        scanned_chars = len(text)

        # 1) 简单寒暄优先（避免"你好"也被送去思考）
        if self._is_simple(text):
            signals.append("简单寒暄")
            score -= 3.0
        else:
            if self._has_complex(text):
                score += 2.0
                signals.append("复杂意图词")

        # 2) 上轮工具出错
        if self._last_tool_error.get(sid):
            score += 1.5
            signals.append("上轮工具失败")

        # 3) 上一轮用过工具 ⇒ 说明在做多步任务，这一轮很可能还要走多步
        #    （原来的"工具数 >= N"在一个会话里恒定不变，是常数而不是信号，
        #      它会恒定地+1.0，等于偷偷把阈值挪走）
        if self._last_turn_tool.get(sid):
            score += self.score_last_turn_tool
            signals.append("上轮用过工具")

        # 4) 上下文相对**自己会话的基线**是否明显变长
        ctx_chars = self._context_chars(request)
        ctx_sig = self._update_context_signal(sid, ctx_chars)
        if ctx_sig:
            score += 0.5
            signals.append(ctx_sig)

        # 5) 单条消息长度
        if len(text) >= self.long_message_chars:
            score += 0.5
            signals.append("消息长")

        enabled = score >= self.threshold

        # 预算控制：超了就强制关（防刷）
        if enabled and not self._budget_ok(sid):
            enabled = False
            signals.append("超预算")

        effort = self._pick_effort(score)
        decision = ThinkingDecision(enabled, score, signals, effort,
                                    scanned_chars=scanned_chars)
        self._last_decision[sid] = decision
        self._trim_sessions()
        return decision

    def _trim_sessions(self) -> None:
        """会话数超过上限时，丢弃一半最不活跃的记录（简单、够用）。"""
        if len(self._last_decision) <= self.max_sessions:
            return
        keep = set(sorted(self._last_decision, key=lambda s: self._opened.get(s, [0])[-1] if self._opened.get(s) else 0)[-self.max_sessions // 2:])
        for d in (self._last_decision, self._last_tool_error, self._opened,
                  self._ctx_ema, self._ctx_samples, self._ctx_warmup, self._ctx_long,
                  self._ctx_ready, self._ctx_settle,
                  self._last_turn_tool, self._turn_had_tool):
            for s in list(d):
                if s not in keep:
                    d.pop(s, None)

    # ── 上下文信号：相对基线 + 滞回 ──
    def _update_context_signal(self, sid: str, ctx_chars: int) -> str:
        """返回信号描述（命中时），或空串（未命中/还在攒基线）。

        为什么用**相对基线**而不是绝对字符数：
          绝对值（比如 6000）对不同用户意义完全不同 —— 新装用户觉得"很长"，
          装了插件的人每轮 3 万+，于是这个信号**永远为真** ⇒ 零信息量，
          而且会恒定地把阈值往下挪。

        为什么用**滞回**（开 1.6× / 关 1.15× 两个不同阈值）：
          只用单一阈值时，上下文在临界点附近波动会导致每轮开关抖动。

        基线何时更新：**先判定、后更新**（否则本轮尖峰会立刻抬高基线，
        把自己这次命中抹掉）。
        """

        base = self._ctx_ema.get(sid)

        # ── 预热期：先收集样本，用**中位数**作种子 ──
        #   为什么不用"第一个样本"当种子：单个样本没有抗噪能力。
        #   如果该会话第一轮恰好是个尖峰（比如刚启动就读到一段超长历史），
        #   基线会被带跑到那个值上，之后要十几轮才收敛回真实水平 ——
        #   这段时间里的判定全部失准（基线虚高 ⇒ 该命中的不命中）。
        #   中位数天然抗尖峰：[100000, 35000, 35000, 35000, 35000] 的中位数还是 35000。
        if base is None:
            buf = self._ctx_warmup.setdefault(sid, [])
            buf.append(float(ctx_chars))
            if len(buf) < self.context_min_samples:
                self._ctx_samples[sid] = len(buf)
                return ""                      # 样本不够，只积累不判定
            base = sorted(buf)[len(buf) // 2]   # 中位数作种子
            self._ctx_ema[sid] = base
            self._ctx_warmup.pop(sid, None)
            self._ctx_samples[sid] = self.context_min_samples
            # 种子刚建立，本轮就参与判定（不用再等一轮）

        # ── 判断基线是否已"稳定"，没稳定就不判定 ──
        #   理由：会话起步时上下文从接近 0 涨到稳定值，基线在追赶，
        #   这期间会持续被判为"偏离基线"（十几轮误判）。
        #   ★ 一旦 ready 就锁定为 True —— 否则一次真实尖峰会让基线大幅变化、
        #     把 settle 计数清零，反而在"最该判定"的时候把信号关掉。
        settle = self._ctx_settle.get(sid, 0)
        new_base = base + self.context_ema_alpha * (ctx_chars - base)
        rel = (abs(new_base - base) / base) if base > 0 else 1.0
        settle = settle + 1 if rel <= CONTEXT_SETTLE_TOL else 0
        self._ctx_settle[sid] = settle
        ready = self._ctx_ready.get(sid, False) or settle >= CONTEXT_SETTLE_TURNS
        self._ctx_ready[sid] = ready

        was_long = self._ctx_long.get(sid, False)
        ratio = (ctx_chars / base) if base > 0 else 0.0

        if not ready:
            # 基线还在追赶 ⇒ 本轮不判定（但仍继续更新基线/样本）
            self._ctx_samples[sid] = self._ctx_samples.get(sid, 0) + 1
            self._ctx_ema[sid] = new_base
            return ""

        if was_long:
            if ratio < self.context_ratio_off:
                self._ctx_long[sid] = False            # 回落到关阈值以下 ⇒ 恢复
                hit = False
            else:
                hit = True
        else:
            if ratio > self.context_ratio_on:
                self._ctx_long[sid] = True             # 突破开阈值 ⇒ 命中
                hit = True
            else:
                hit = False

        # 先判定、后更新基线（EMA，平滑掉单轮尖峰）
        self._ctx_samples[sid] = self._ctx_samples.get(sid, 0) + 1
        self._ctx_ema[sid] = new_base

        if hit:
            return f"上下文偏离基线({ratio:.2f}×)"
        return ""

    def context_baseline(self, sid: str) -> float:
        """给面板看：该会话当前的上下文基线。"""
        return float(self._ctx_ema.get(sid, 0.0) or 0.0)

    # ── 工具信号：维护"上一轮是否用过工具" ──
    def note_turn_tool(self, sid: str) -> None:
        """本轮调用了工具（由 on_tool_result 调用）。"""
        if sid:
            self._turn_had_tool[sid] = True

    def commit_turn(self, sid: str) -> None:
        """本轮结束（由 on_final_result 调用）：把本轮的工具有无记为"上一轮"。"""
        if not sid:
            return
        self._last_turn_tool[sid] = bool(self._turn_had_tool.pop(sid, False))

    def _budget_ok(self, sid: str) -> bool:
        now = time.time()
        hist = [t for t in self._opened.get(sid, []) if now - t < 60]
        if len(hist) >= self.max_per_minute:
            self._opened[sid] = hist
            return False
        hist.append(now)
        self._opened[sid] = hist
        return True

    def _pick_effort(self, score: float) -> str:
        if score >= self.effort_high:
            return "high"
        if score >= self.effort_low:
            return "medium"
        return "low"

    @staticmethod
    def _is_simple(text: str) -> bool:
        if not text or len(text) > 40:
            return False
        low = text.lower().strip()
        return any(h in low for h in SIMPLE_HINTS)

    @staticmethod
    def _has_complex(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return any(h in low for h in COMPLEX_HINTS)

    @staticmethod
    def _typed_text_from_event(event: Any) -> str | None:
        """从事件的消息链里取出**用户真正打的字**（结构判据，最可靠）。

        `event.messages[*].chain` 是框架解析好的 MessageChain；
        里面的 `Text` 元素就是用户输入的原始文本，
        而 `Image` / `Sticker` 等媒体的**描述**根本不在其中
        ⇒ 天然把描述排除在外，不用去猜字符串边界。

        这也与 s 版聊天插件的做法一致（它判断媒体看的是元素类型，不是字符串）。
        取不到返回 None，交给调用方走兜底。
        """
        try:
            from core.chat.message_elements import Text
        except Exception:  # noqa: BLE001
            return None
        try:
            msgs = getattr(event, "messages", None) or []
            parts = []
            for m in msgs:
                chain = getattr(m, "chain", None)
                if chain is None:
                    continue
                for ele in chain:
                    if isinstance(ele, Text):
                        parts.append(getattr(ele, "text", "") or "")
            if not parts:
                return None
            return "\n".join(parts)
        except Exception:  # noqa: BLE001
            return None

    #: 框架在 message_format_to_text 里给媒体元素加的**固定前缀**。
    #  按这些前缀剥掉"描述"，只留用户真正打的字。
    #  （core/message_manager.py：`[Image {caption}]`、`[Sticker {caption}]`、
    #    `[Emoji {emoji_desc} (ID: …)]`、`[Image attached]`）
    #  ⚠️ 注意：[Emoji …] 是**用户自己选的表情**（不是模型生成的描述），
    #    不该剥 —— 它短、且是用户的表达。只剥模型生成的媒体描述。
    _MEDIA_PREFIX = ("[Image ", "[Sticker ", "[Record ", "[Video ", "[File ")
    #: 剥掉描述后留下的占位（说明"这里有媒体"，但字数不参与计分）
    _MEDIA_PLACEHOLDER = "[图]"

    @classmethod
    def _strip_media_desc(cls, text: str) -> str:
        """**兜底路径**：从已格式化的字符串里去掉媒体描述。

        ★ 只在拿不到结构化消息链时使用 —— 因为字符串剥离本质上是"猜边界"：
          描述里可能含**不平衡的方括号**（实测会把用户打的字也吃掉）。
        所以这里**保守**处理：只有"方括号能配平、且整段确实是媒体标记"才剥，
        否则原样返回（宁可多算几分，也不要误删用户的话）。

        为什么还要留着：万一框架改了事件结构（拿不到 chain），
        至少不会把"发一张图 = 触发思考"的问题完全暴露回来。
        """
        if not text:
            return text
        out = []
        i = 0
        n = len(text)
        while i < n:
            hit = None
            for pfx in cls._MEDIA_PREFIX:
                if text.startswith(pfx, i):
                    hit = pfx
                    break
            if hit is None:
                out.append(text[i])
                i += 1
                continue
            # 方括号配对；**配不平就整段放弃剥离**（保守）
            depth = 0
            j = i
            ok = False
            while j < n:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        ok = True
                        break
                j += 1
            if not ok:
                # 配不平 ⇒ 不动它，原样保留（宁可不剥，也不误伤）
                out.append(text[i])
                i += 1
                continue
            out.append(cls._MEDIA_PLACEHOLDER)
            i = j
        return "".join(out)

    @classmethod
    def _user_text(cls, request: Any) -> str:
        """只取**用户这一轮真正说的话**。

        ★★ 不能把整个 `user_prompt` 当用户消息（2026-09-23 用户实测：每轮都触发思考）
          `user_prompt` 里混着**别的插件注入的内容**：
            · 长期记忆-Z：`req.user_prompt.insert(Prompt(记忆, name="alife_memory"))`
            · 会话合并：  插入时间提示
            · SubAgent：  `Prompt(task_text, name="task")`
          把注入的记忆/摘要当用户消息扫 ⇒
            · 它们**很长** ⇒ 命中「消息长」(+0.5)
            · 里面常有"分析/计划/步骤"这类词 ⇒ 命中「复杂意图词」(+2.0)
          ⇒ 2.5 ≥ 阈值 2.0 ⇒ **每轮都开思考**，而且和用户实际说了什么无关。

          框架给真正的 IM 消息打的标记是 `name="message"`
          （core/message_manager.py:654 `Prompt(message.message_str, name="message")`）。
        """
        parts = [getattr(p, "content", "") or ""
                 for p in (getattr(request, "user_prompt", None) or [])
                 if getattr(p, "name", None) == "message"]
        if not parts:
            # 兜底：万一框架改了标记名，退回"取全部" —— 至少不会静默失效，
            # 但把长度报出去，日志里能一眼看出异常。
            parts = [getattr(p, "content", "") or ""
                     for p in (getattr(request, "user_prompt", None) or [])]
        text = "\n".join(parts)
        # ★ 图片/表情包描述不算入计分（默认开）—— 描述通常很长，
        #   算进来会"光发一张图就触发思考"（用户实测 281 字描述 +0.5 分）。
        if getattr(cls, "exclude_media_desc", True):
            # ① 首选**结构判据**：直接从事件的消息链取用户打的字（最可靠）
            typed = cls._typed_text_from_event(
                getattr(request, "__dict__", {}).get("_accel_event"))
            if typed is not None and typed.strip():
                return typed
            # ② 兜底：字符串剥离（保守，拿不准就不剥）
            try:
                text = cls._strip_media_desc(text)
            except Exception:  # noqa: BLE001
                pass
        return text

    @staticmethod
    def _context_chars(request: Any) -> int:
        """量"模型本轮真正会看到的上下文"。

        ★ 必须把 `user_prompt` 也算进来：判断发生在 ON_LLM_REQUEST，
          那一刻 `request.messages` 里**只有历史**，本轮刚收到的消息
          还在 `request.user_prompt` 里（框架到 assemble_prompt 才合并）。
          只量 messages 会导致"本轮消息把上下文撑大"要等到下一轮才被察觉 ——
          也就是判断滞后一轮。
        """
        total = 0
        for p in getattr(request, "user_prompt", None) or []:
            try:
                total += len(getattr(p, "content", "") or "")
            except Exception:  # noqa: BLE001
                pass
        for m in getattr(request, "messages", None) or []:
            try:
                c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            except Exception:  # noqa: BLE001
                c = ""
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
        return total


# ── 把思考参数注入 extra_body ──
def build_thinking_extra_body(style: str, effort: str) -> dict:
    """按厂商风格生成"开思考"的请求体片段。

    ★ 关键事实（读框架源码 + 官方文档确认）：
      `reasoning_effort` 的**取值范围因厂商而异**，不是统一的 low/medium/high！
        - OpenAI o 系列：low / medium / high（还有 none / minimal / xhigh）
        - **DeepSeek V4：只有 high / max**（KiraAI 的 schema 就写了
          `"options": ["high","max"]`，见 core/provider/src/deepseek/schema.json）
        - 部分网关只认 none / high / max
      所以本函数会把内部档位(low/medium/high)**按风格映射**到厂商合法值，
      绝不直接把 medium 发给 DeepSeek（那会被静默忽略或报错）。

    style:
      - `compatible`（默认）：最通用的一套，多数兼容网关认
      - `reasoning_effort`：OpenAI o 系列（low/medium/high）
      - `deepseek`：**DeepSeek V4 专用**（high/max）—— 见下方说明
      - `enable_thinking`：Qwen / 阿里系
      - `thinking_object`：Anthropic / 部分网关
      - `vllm_chat_template`：vLLM 部署的 Qwen
    """
    # 内部档位 → 厂商合法值 的映射
    if style == "reasoning_effort":
        return {"reasoning_effort": {"low": "low", "medium": "medium", "high": "high"}.get(effort, "medium")}

    if style == "deepseek":
        # DeepSeek: thinking 是 extra_body 字段；reasoning_effort 是顶层参数（只能 high/max）
        # 框架自己就是这么发的（core/provider/src/deepseek/model_clients.py:55-71）：
        #   extra_body["thinking"] = {"type": "enabled"}; kwargs["reasoning_effort"] = "high"
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": {"low": "high", "medium": "high", "high": "max"}.get(effort, "high"),
        }

    if style == "enable_thinking":
        return {"enable_thinking": True}

    if style == "thinking_object":
        return {"thinking": {"type": "enabled", "budget_tokens": _budget_for(effort)}}

    if style == "vllm_chat_template":
        return {"chat_template_kwargs": {"enable_thinking": True}}

    # compatible：只发最保险的两种拼写，且 reasoning_effort 用**厂商交集**
    # （high 在 OpenAI 与 DeepSeek 都合法；low/medium 在 DeepSeek 非法）
    return {
        "reasoning_effort": {"low": "low", "medium": "high", "high": "high"}.get(effort, "high"),
        "enable_thinking": True,
    }


def build_nothinking_extra_body(style: str) -> dict:
    """关思考（用于"这轮不需要想"时显式关掉，省 token 又更快）。

    ⚠️ 刻意保守：只发**最通用、最不容易 400** 的那一个字段。
      实测部分网关对 `reasoning_effort: "none"` 会报错，故不采用。
    """
    if style == "reasoning_effort":
        return {"reasoning_effort": "low"}          # 最低档而不是 none
    if style == "deepseek":
        # DeepSeek 关思考：只发 extra_body 的 thinking（不发 reasoning_effort）
        return {"thinking": {"type": "disabled"}}
    if style == "enable_thinking":
        return {"enable_thinking": False}
    if style == "thinking_object":
        return {"thinking": {"type": "disabled"}}
    if style == "vllm_chat_template":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"enable_thinking": False}


#: 强度档位排序（只用于"不降级"比较）
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "max": 3}


def provider_effort(model) -> "Optional[str]":
    """读提供商那边给这个模型配的思考强度（没配返回 None）。

    两个来源（读框架源码确认）：
      · DeepSeek 的模型配置里是**顶层** `reasoning_effort`（high/max）
      · 其它兼容网关一般写在 `section_advanced.extra_body.reasoning_effort`
    """
    try:
        cfg = getattr(model, "model_config", None) or {}
        v = cfg.get("reasoning_effort")
        if not isinstance(v, str) or not v:
            adv = cfg.get("section_advanced") or {}
            eb = adv.get("extra_body") if isinstance(adv, dict) else None
            v = eb.get("reasoning_effort") if isinstance(eb, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001
        return None


def follow_provider_effort(params: dict, prov_effort: "Optional[str]") -> dict:
    """【可选行为】判定要思考时，**强度完全用提供商配的那个值**。

    对应面板开关「开思考时跟随提供商的强度」（默认关）。

    ★★★ 2026-09-28 修正（用户："是否能由我们来成功控制"）—— 这里原来有个**真缺陷**：
      旧实现是"把我们的 `reasoning_effort` **去掉**，让提供商那份自己生效"。
      但**框架不一定发**提供商那份！实测（DeepSeek 客户端）：
          `if thinking_enabled: kwargs["reasoning_effort"] = reasoning_effort`
      —— 提供商配了 `thinking_enabled=False` + `reasoning_effort=max` 时，
      框架**永远不写**这个强度；而我们把思考打开之后，用户设的 `max`
      就变成了默认值 ⇒ 「跟随」**名不副实**（这也正是用户感觉"控制不住"的一面）。
      ⇒ 现在改为**把提供商的强度值替进我们的参数**（按本客户端该在的位置发出去），
        不再依赖框架的条件写入 ⇒ "设的 max 就是 max"。
      提供商**没配**强度时保持我们算出来的档位（否则就没强度了）。

    为什么**不**默认这么做：插件按轮次调节强度本来就是它的功能之一
    （简单轮次用低档更省更快）。想要"只开不动强度"的人再打开这个开关。

    注意：这个函数只管"开思考"这一路；"关思考"（inject_nothinking）不受影响。
    """
    if not prov_effort:
        return params
    out = dict(params)
    if "reasoning_effort" in out:
        out["reasoning_effort"] = prov_effort
    return out


def _budget_for(effort: str) -> int:
    return {"low": 1024, "medium": 4096, "high": 16384}.get(effort, 4096)


# ══════════════════════════════════════════════════════════════
# 参数注入路由 —— 同样是"开思考"，三家放的位置不一样
# ══════════════════════════════════════════════════════════════

#: 客户端类型 → 默认风格（用户没显式指定时用）
_AUTO_STYLE = {
    "openai": "compatible",        # 阿里/硅基/火山/魔搭/OpenAI 等兼容网关
    "deepseek": "deepseek",        # DeepSeek：thinking + 顶层 reasoning_effort(high/max)
    "anthropic": "thinking_object",  # Anthropic：顶层 thinking
}


def resolve_style(user_style: str, client_kind: str) -> str:
    """用户没指定具体风格时，按**客户端类型**选合适的那个。

    `auto` 与 `compatible` 都算"没指定"：
      · `auto` 是新的默认值；
      · `compatible` 是历史默认值 —— 而它在 DeepSeek / Anthropic 上
        根本没有正确写法（发出去会被忽略或报错），所以一并按类型改写。
    用户**显式**选了别的风格（reasoning_effort / enable_thinking /
    thinking_object / deepseek / vllm_chat_template）就原样照用。
    """
    if user_style in ("auto", "compatible", "", None):
        return _AUTO_STYLE.get(client_kind, "compatible")
    return user_style


# ── 思考相关键的"全拼写"集合 ──
# ★★★ 2026-09-28（用户："自动思考疑似并没有真的有效，尤其注意确认如果原本提供商
#   那我们选择了开启思考的，是否能由我们来成功控制"）—— 复现出的**真实缺陷**：
#
#   各家"开/关思考"用的字段名不同（thinking / enable_thinking / reasoning_effort /
#   chat_template_kwargs...），而**提供商自己也可能配**其中某一种。
#   以前我们只发自己那两把钥匙（enable_thinking + reasoning_effort），
#   提供商那把（比如 `thinking`）**原样留在请求体里** ⇒ 两把钥匙同时到达网关，
#   它先认哪把是**轮盘赌**：
#     · 提供商配 `thinking:{enabled}` + 我们判"关" ⇒ 仍可能被读成**开**（压不住）
#     · 提供商配 `thinking:{disabled}` + 我们判"开" ⇒ 仍可能被读成**关**（打不开）
#   实测（tests/test_thinking_conflict.py）：`thinking:{enabled}` / `{disabled}` 两条
#   都会残留 ⇒ 用户看到的就是"插件好像控制不住思考"。
#
#   ⇒ 修法：我们**表达哪个维度，就拿走那个维度**（清掉提供商那份的同维键），
#     让该维度在请求体里**只剩一个声音**；我们没表达的维度**原样保留**
#     （例如「跟随提供商强度」开启时，强度维度不属于我们，不能清）。
#
# 维度划分：
#   D1「要不要思考」= thinking / enable_thinking / (chat_template_kwargs.enable_thinking)
#   D2「想多深」    = reasoning_effort / budget_tokens / reasoning_max_tokens
_THINK_D1_EXTRA = (
    "thinking", "enable_thinking", "include_reasoning", "reasoning",
    "chat_template_kwargs", "thinking_config",
)
_THINK_D2_EXTRA = (
    "reasoning_effort", "thinking_budget", "reasoning_max_tokens",
)
_THINK_D1_TOP = (
    "thinking", "enable_thinking", "include_reasoning", "reasoning",
)
_THINK_D2_TOP = (
    "reasoning_effort", "thinking_budget", "reasoning_max_tokens",
)


def strip_conflicting_thinking(kwargs: dict, clear_effort: bool = True) -> int:
    """清掉请求体里**与我们本次判定冲突**的思考键。返回清掉的个数。

    :param clear_effort: 是否连"强度"维度也清。
        · 我们按自己的判定发强度（默认）⇒ True（否则提供商那份强度会打架）
        · 「跟随提供商强度」开启 ⇒ **False**（强度归提供商，不能清）

    ★ 只清"思考"这一类键，**绝不碰** temperature / max_tokens / tools 等无关字段。
    ★ 保留 `chat_template_kwargs` 里的非思考子键（该字典可能装别的东西）。

    ★★★ 2026-09-29（自查发现的**严重缺陷**，必须记牢）：
      框架的 `_build_request_kwargs` 是这么写的：
          extra_body = section_advanced.get("extra_body")   # ← 配置里的**同一个 dict**
          kwargs["extra_body"] = extra_body                  # ← 原样引用传出去
      所以**绝不能在这个 dict 上就地 pop()** —— 那会**永久改写用户的配置对象**
      （内存里那份），第一次请求过后用户的思考设置就被抹掉了
      （实测：调用一次后 `cfg["section_advanced"]["extra_body"]` 从
        `{thinking: ..., reasoning_effort: ...}` 变成 `{}`）。
      ⇒ 必须先**拷贝**出我们自己的一份再清；这样：
        · 本次请求体里只有我们的声音（控制得住）
        · 用户配置一字不动（下次请求、其它模型、面板读到的都还是原样）
    """
    extra_raw = kwargs.get("extra_body")
    if not isinstance(extra_raw, dict):
        return 0

    # ★ 深拷贝一层（值本身多是 dict，如 {"type": "enabled"} —— 用 copy.deepcopy 保险）
    import copy as _copy
    extra = _copy.deepcopy(extra_raw)

    removed = 0
    keys = list(_THINK_D1_EXTRA) + (list(_THINK_D2_EXTRA) if clear_effort else [])
    for k in list(extra.keys()):
        if k not in keys:
            continue
        if k == "chat_template_kwargs" and isinstance(extra[k], dict):
            inner = dict(extra[k])
            for ik in list(inner.keys()):
                if any(w in ik.lower() for w in ("think", "reason")):
                    inner.pop(ik, None)
                    removed += 1
            if inner:
                extra[k] = inner
            else:
                extra.pop(k, None)
            continue
        extra.pop(k, None)
        removed += 1

    if extra:
        kwargs["extra_body"] = extra
    else:
        kwargs.pop("extra_body", None)

    top_keys = list(_THINK_D1_TOP) + (list(_THINK_D2_TOP) if clear_effort else [])
    for k in list(kwargs.keys()):
        if k in top_keys and k != "extra_body":
            kwargs.pop(k, None)
            removed += 1
    return removed


def apply_thinking_params(kwargs: dict, params: dict, client_kind: str,
                          clear_effort: bool = True) -> dict:
    """把思考参数放进**正确的位置**。

    实测三家位置不同（读框架源码确认）：
      · OpenAI 兼容系：全部塞 `extra_body`
      · **DeepSeek**：`thinking` 进 extra_body，但 **`reasoning_effort` 必须在顶层**
        —— 框架自己的 `DeepSeekLLMClient._build_request_kwargs` 就是
        `kwargs["reasoning_effort"] = ...`（不是塞进 extra_body）
      · **Anthropic**：**`thinking` 必须在顶层**（body 参数，不是 extra_body）

    ★★★ 2026-09-28（用户："是否能由我们来成功控制"）：**先清场、再注入**。
      否则提供商自己配的那把"钥匙"（不同拼写）会与我们的**同时**到达网关，
      谁生效取决于网关先读哪个 ⇒ 表现为"插件控制不住思考"（已实测复现）。
      我们表达的维度清掉提供商那份、再放上我们的；没表达的维度不动。
    """
    strip_conflicting_thinking(kwargs, clear_effort=clear_effort)

    extra = dict(kwargs.get("extra_body") or {})
    for k, v in params.items():
        if client_kind == "deepseek" and k == "reasoning_effort":
            kwargs[k] = v               # ★ DeepSeek 要顶层
        elif client_kind == "anthropic" and k == "thinking":
            kwargs[k] = v               # ★ Anthropic 要顶层
        else:
            extra[k] = v

    if client_kind == "anthropic":
        th = kwargs.get("thinking")
        if isinstance(th, dict):
            if th.get("type") == "disabled":
                # Anthropic 关思考 = **不发**这个字段
                # （{"type":"disabled"} 在旧版 API 会 400）
                kwargs.pop("thinking", None)
            else:
                # ★ 硬约束：Anthropic 要求 max_tokens > budget_tokens，否则 400。
                #   我们的档位最高 16384，而框架默认 max_tokens 才 4096 ⇒ 必须夹紧。
                mt = kwargs.get("max_tokens")
                bt = th.get("budget_tokens")
                if isinstance(mt, int) and isinstance(bt, int) and bt >= mt:
                    th = dict(th)
                    th["budget_tokens"] = max(1024, mt - 1024)
                    kwargs["thinking"] = th

    if extra:
        kwargs["extra_body"] = extra
    return kwargs

