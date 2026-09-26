"""KiraAI 提速器 —— 在不影响质量与兼容的前提下提速。

分层设计：
  L1 观测层    始终可开，零风险：记录每步耗时/token/抢先发送情况
  L3 接管层    护栏式打补丁：HTTP 客户端复用 / 记忆落盘异步化
  L4 流式引擎  ★ 强制流式（任意 provider）+ 抢先发送（首字即刻可见）
  L5 自动思考  ★ 对标并超越 Alife：多信号打分决定这轮要不要开思考

接管铁律（patches.py 实现）：幂等 / 精确还原 / 熔断旁路。

兼容性保证（已核对插件市场 7 个重点插件的真实源码）：
  - 插件依赖 `client.model.*`（sustained-chat queue_merge.py:136、
    midflight main.py:261、model-fallback main.py:35）⇒ 代理类透传 `.model`
  - 插件依赖 `ON_LLM_REQUEST` 改 tool_set ⇒ 我们用 Priority.LOW 后执行，不抢
  - 插件依赖 `ON_MESSAGE_SENT` ⇒ 抢先发送时我们自己补广播
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from typing import Any, Optional

from core.plugin import BasePlugin, logger, on, Priority, register, PluginPage, PageMenu
from core.provider import LLMRequest, LLMResponse
from core.chat.message_utils import KiraMessageBatchEvent

from .patches import (PatchHandle, PatchRegistry, install, breaker_for,
                      mark_side_effects)
from .stream_engine import StreamEngine, LLMClientProxy
from .auto_thinking import (
    AutoThinkingController,
    apply_thinking_params,
    build_thinking_extra_body,
    build_nothinking_extra_body,
    follow_provider_effort,
    provider_effort,
    resolve_style,
)

PLUGIN_ID = "kira_accelerator"

#: "上次发送时刻"这张表最多记多少个会话（超出整体清空，防长期运行内存累积）
MAX_PACING_SIDS = 512


class SendCtx:
    """一轮请求的发送上下文（会话 / 事件 / 标签集）。

    ★ 为什么必须**跟着 request 走**，而不是放在 self 上（线上事故）：
      框架会并发处理多个会话（群里来消息、私聊同时在回复）。
      `self._current_sid` 是实例级的，会被**后到的**事件覆盖，
      而抢先发送发生在之后的 `chat()` 里 —— 于是
      **A 会话的回复被发到了 B 会话**（用户实测：私聊的回复进了群）。

      上下文挂在 request 上，天然跟着这一次调用走，并发也不会串。
    """

    __slots__ = ("sid", "event", "tag_set")

    def __init__(self, sid, event, tag_set):
        self.sid = sid
        self.event = event
        self.tag_set = tag_set


def ctx_of(request) -> "Optional[SendCtx]":
    """取出挂在 request 上的发送上下文（没有就是 None）。"""
    return getattr(request, "__dict__", {}).get("_accel_ctx")

class AcceleratorPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.patches = PatchRegistry()

        # ── 配置 ──
        c_obs = cfg.get("section_observe", {}) or {}
        c_req = cfg.get("section_request", {}) or {}
        c_tak = cfg.get("section_takeover", {}) or {}
        c_str = cfg.get("section_stream", {}) or {}
        c_thk = cfg.get("section_thinking", {}) or {}
        c_req = cfg.get("section_compat", {}) or {}
        c_app = cfg.get("section_appearance", {}) or {}

        self.observe_enabled = bool(c_obs.get("enabled", True))
        self.observe_window = int(c_obs.get("window", 50))
        # ★ 计数保留天数：默认 30 天自动清除；0 = 永久不清
        try:
            self.stats_keep_days = int(c_obs.get("stats_keep_days", 30))
        except (TypeError, ValueError):
            self.stats_keep_days = 30
        if self.stats_keep_days < 0:
            self.stats_keep_days = 0


        self.takeover_build_client = bool(c_tak.get("reuse_http_client", True))
        # 记忆落盘去掉缩进（纯格式，解析结果不变）
        # ⚠️ 待验证功能：**代码里锁死**，配置写成 true 也不生效。
        #   为什么锁：实测收益只在多群大规模时才明显（单会话约 0.7ms），
        #   而且它发生在回复发出【之后】、与事件循环的交互尚未充分验证。
        #   等验证清楚了再放开（面板上这个开关同样是置灰的）。
        #   想临时验证：把下面这行改成读配置即可。
        self.takeover_memory_dump = False
        if bool(c_tak.get("compact_memory_dump", False)):
            logger.warning("[accel] compact_memory_dump 已配置为开，"
                           "但该功能仍在待验证状态，本次不启用")

        # L4
        self.force_stream = bool(c_str.get("force_stream", True))
        self.early_send = bool(c_str.get("early_send", True))
        self.stream_providers = list(c_str.get("providers", []) or [])

        # 请求体兼容加固（默认开，零风险）
        self.normalize_empty_content = bool(c_req.get("normalize_empty_content", True))

        # 同轮工具并行（默认关）
        c_par = cfg.get("section_parallel", {}) or {}
        from .parallel_tools import ToolGate, DEFAULT_BLACKLIST
        self._tool_gate = ToolGate(
            enabled=bool(c_par.get("enabled", False)),
            whitelist=list(c_par.get("whitelist", []) or []),
            blacklist=list(c_par.get("blacklist", []) or []) or list(DEFAULT_BLACKLIST),
            match_exact=bool(c_par.get("match_exact", False)),
            max_parallel=int(c_par.get("max_parallel", 8)),
        )

        # 外观
        self.wallpaper_enabled = bool(c_app.get("wallpaper_enabled", True))
        self.wallpaper_interval = int(c_app.get("wallpaper_interval", 30))
        self.wallpaper_files = list(c_app.get("wallpaper_files", []) or [])

        # L5
        self.thinking_enabled = bool(c_thk.get("enabled", False))
        self.thinking_style = str(c_thk.get("style", "compatible"))
        self.thinking_inject_nothink = bool(c_thk.get("inject_nothinking", False))
        # 开思考时是否"只开、强度照用提供商配的"（默认关：插件按判定调强度）
        self.thinking_follow_provider = bool(
            c_thk.get("follow_provider_effort", False))
        self.thinking = AutoThinkingController(
            threshold=float(c_thk.get("threshold", 2.0)),
            long_message_chars=int(c_thk.get("long_message_chars", 220)),
            context_ratio_on=float(c_thk.get("context_ratio_on", 1.6)),
            context_ratio_off=float(c_thk.get("context_ratio_off", 1.15)),
            context_min_samples=int(c_thk.get("context_min_samples", 5)),
            score_last_turn_tool=float(c_thk.get("score_last_turn_tool", 1.0)),
            effort_low=float(c_thk.get("effort_low", 2.0)),
            effort_high=float(c_thk.get("effort_high", 4.5)),
            max_per_minute=int(c_thk.get("max_per_minute", 60)),
        )
        # ★ 图片/表情包描述不计入评分（默认开）—— 描述长，算进去会"光发图就开思考"
        self.thinking.exclude_media_desc = bool(c_thk.get("exclude_media_desc", True))

        # ── 运行时状态 ──
        self._samples: deque[dict] = deque(maxlen=self.observe_window)
        self._client_cache: dict[tuple, Any] = {}
        self._current_sid: Optional[str] = None
        self._current_event: Any = None
        self._current_tag_set: Any = None
        self._current_resp: Any = None
        self._last_seg_ts: dict[str, float] = {}   # 每个会话各自的"上次发送时刻"
        self._resp_by_sid: dict[str, Any] = {}      # 每个会话各自的"本轮响应"
        # 本轮抢先发送的结果（按顺序），供发送层拼回 message_results 以对齐 message_id
        self._early_results: dict[str, list] = {}
        # ★ 旁证台账：sid → 本轮已抢发的**段原文**列表
        #   （响应标记丢失时的兜底；存原文而不是只存个数，
        #     这样兜底时也能按内容剥离，对文本被改写同样稳健）
        self._sent_ledger: dict[str, list] = {}
        #: 台账被消费的**时间戳**（sid -> float）。
        #: 用它区分"第一步"与"后续步"；带时间戳是为了**不依赖清理钩子**
        #: （钩子可能被前面 stop() 的处理器跳过，纯布尔会永久卡在 True）。
        self._ledger_consumed: dict[str, float] = {}
        #: 台账最后一次活动时间（用于清理陈旧状态，双保险）
        self._ledger_ts: dict[str, float] = {}
        self._proxy_cache: dict[tuple, LLMClientProxy] = {}
        self._stats = {
            "turns": 0, "steps": 0,
            "tool_signals": 0, "strip_calls": 0, "parallel_batches": 0, "content_normalized": 0,
            "streamed_calls": 0, "early_sent": 0, "first_seg_s": None,
            "thinking_on": 0, "thinking_off": 0, "thinking_skipped_budget": 0,
        }

    # ══════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════
    # KPI 统计的持久化
    # ══════════════════════════════════════════════════════════
    def _stats_file(self):
        """统计文件位置（插件数据目录，框架约定）。"""
        try:
            d = self.ctx.get_plugin_data_dir()          # 框架提供的插件数据目录
            d.mkdir(parents=True, exist_ok=True)
            return d / "kpi_stats.json"
        except Exception:  # noqa: BLE001
            return None

    def _load_stats(self) -> None:
        """热重载后把累计统计读回来（用户要求：点保存/热重载不该清零）。"""
        try:
            p = self._stats_file()
            if p is None or not p.is_file():
                return
            import json
            saved = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
            # ★ 保留期结算（在灌入数值**之前**判断，否则会先看到旧值）
            #   ★★ 它返回 True 表示「刚清零」⇒ 必须**跳过下面的灌入**，
            #   否则刚清成 0 又被文件里的旧值覆盖 —— 实测抓到的真 bug。
            if self._apply_keep_window(saved):
                return
            for k, v in saved.items():
                if k in self._stats:
                    # ★ 只接受同类型，避免污染（比如把 dict 塞进数字里）
                    cur = self._stats[k]
                    if cur is None or isinstance(v, (int, float)):
                        self._stats[k] = v
            self._stats_since = saved.get("_since")
            logger.info("[accel] 已恢复累计统计：%s", {
                k: self._stats.get(k) for k in ("turns", "early_sent", "streamed_calls")
            })
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 恢复统计失败（忽略，按 0 起步）")

    def _apply_keep_window(self, saved: dict) -> None:
        """按「计数保留天数」结算：超期就清零并重新起算（滚动窗口）。

        返回 True 表示**本次已清零**（调用方必须跳过后续的数值灌入）。

        · keep=0  ⇒ 永久保留，不做任何处理
        · 首次启动（文件里没有 _since）⇒ 记为现在，本轮照常累计
        · 已超期 ⇒ 清零 stats 并把 _since 重置为现在
        ★ 放在加载时结算，而不是起一个常驻计时器：
          插件停用期间不会执行任何代码，下次加载时一次性算清即可，
          既不占资源，也不会因为休眠而漏判。
        """
        import json
        import time
        keep = int(getattr(self, "stats_keep_days", 30) or 0)
        p = self._stats_file()
        if keep <= 0:
            return False                            # 永久保留
        now = time.time()
        since = saved.get("_since")
        try:
            since = float(since) if since is not None else None
        except (TypeError, ValueError):
            since = None
        # ★★ 合理性校验：_since 必须是"过去且为正"的时间。
        #   为什么要校验：文件可能被手工改过、被同步工具写坏、或系统时间被调过。
        #   - `_since = -1` 会让 (now - since) 大得离谱 ⇒ **误判超期、把计数清掉**；
        #   - `_since` 在未来（时间被调过）⇒ 窗口永远不触发。
        #   两种情况都**只重记起算点、本轮绝不清零** —— 宁可不清，也不误伤数据。
        #   （边界审计实测抓到 `_since=-1` 会清掉计数，故加此校验。）
        #   还有"准 0"的时间戳（1e-9 ≈ 1970-01-01）：按字面确实"超期 55 年"，
        #   但那不可能是真实起算点 —— 插件那时并不存在。⇒ 用一个**绝对下限**
        #   兜住这类值（2020-09 之前的时间戳对本插件都不可能是真的起算点）。
        #   （相对下限定不住：回溯 100 年后会是负数，反而把 1e-9 放行了 —— 实测踩过。）
        _FLOOR = 1_600_000_000.0                    # 2020-09-13，早于此视为坏数据
        if since is None or since <= 0 or since > now or since < _FLOOR:
            try:
                if p is not None:
                    data = dict(saved)
                    data["_since"] = now
                    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            return False
        if now - since < keep * 86400.0:
            return False                            # 还在窗口内
        # 超期 ⇒ 清零并重新起算
        days = int((now - since) // 86400)
        for k in self._stats:
            self._stats[k] = None if k == "first_seg_s" else 0
        try:
            if p is not None:
                data = {k: v for k, v in self._stats.items()
                        if isinstance(v, (int, float)) or v is None}
                data["_since"] = now
                p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.info("[accel] 计数保留期（%s 天）已到（距上次起算 %s 天）⇒ 计数已清零并重新起算",
                    keep, days)
        return True                                 # ★ 告诉调用方：已清零，别再灌旧值

    def _save_stats(self) -> None:
        """把统计落盘（节流：最多每 5 秒一次，避免每轮都写盘）。"""
        import time
        now = time.monotonic()
        if now - getattr(self, "_stats_saved_at", 0.0) < 5.0:
            return
        self._stats_saved_at = now
        try:
            p = self._stats_file()
            if p is None:
                return
            import json
            # 只存数字与 None（first_seg_s 可能是 None）
            data = {k: v for k, v in self._stats.items()
                    if isinstance(v, (int, float)) or v is None}
            # ★ 保留期起算点也要落盘，否则每次加载都会当成"首次"从而永不清零
            if getattr(self, "_stats_since", None) is not None:
                data["_since"] = self._stats_since
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass          # 存不下不影响功能

    async def initialize(self) -> None:
        logger.info(
            "[accel] initialize（观测=%s 强制流式=%s 抢先发送=%s 自动思考=%s）",
            self.observe_enabled, self.force_stream,
            self.early_send, self.thinking_enabled,
        )
        if self.takeover_build_client:
            self._install_client_cache()
        if self.force_stream or self.early_send:
            self._install_stream_engine()
        if self.thinking_enabled or self.normalize_empty_content:
            self._install_request_hook()
        if self.early_send:
            self._install_early_sent_strip()
        if self._tool_gate.enabled:
            self._install_parallel_tools()
        if self.takeover_memory_dump:
            self._install_memory_dump()
        # ★ 把上次累计的统计读回来 —— 热重载（保存配置）不会清零
        self._load_stats()

    async def terminate(self) -> None:
        # ★ 卸载前把统计落盘（热重载会走这里，下次 initialize 再读回来）
        try:
            self._stats_saved_at = 0.0      # 绕过节流，保证这次一定写
            self._save_stats()
        except Exception:  # noqa: BLE001
            pass
        self.patches.uninstall_all()
        self._proxy_cache.clear()
        logger.info("[accel] terminate，所有接管点已还原")

    # ══════════════════════════════════════════════════════════
    # L1 观测
    # ══════════════════════════════════════════════════════════
    @on.step_result(priority=Priority.LOW)
    async def observe_step(self, event: KiraMessageBatchEvent, step_result, *_):
        if not self.observe_enabled:
            return
        try:
            self._stats["steps"] += 1
            self._samples.append({
                "ts": time.time(),
                "sid": getattr(event, "sid", "?"),
                "segments": len(getattr(step_result, "message_results", []) or []),
                "chars": len(getattr(step_result, "raw_output", "") or ""),
            })
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 观测失败")

    def _clear_round_state(self, sid):
        """一轮真正结束时清理"本轮状态"（台账 / 响应缓存）。

        ★ 为什么必须等到**轮结束**而不是"每次 send_xml_messages 返回"：
          框架的多步 agent loop 每一步都会调用 send_xml_messages，
          若在每次调用后就清台账，**第二步就剥离不了**（n=0）
          ⇒ 整批重复发送（用户线上现象）。
        """
        try:
            if sid:
                self._sent_ledger.pop(sid, None)
                self._sent_ledger.pop(sid + "\x00ev", None)
                self._ledger_consumed.pop(sid, None)
                self._resp_by_sid.pop(sid, None)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 清理本轮发送状态失败（不影响发送）")

    @on.final_result(priority=Priority.LOW)
    async def observe_final(self, event: KiraMessageBatchEvent, final_result, *_):
        # ★ 无论观测是否开启，**都要**清理本轮发送状态（否则台账会残留到下一轮，
        #   下一轮若文本恰好相同就会被误剥离 = 丢内容）。
        self._clear_round_state(getattr(event, "sid", None))
        if not self.observe_enabled:
            return
        try:
            self._stats["turns"] += 1
            self._save_stats()          # ★ 节流落盘（≤5s 一次），热重载不清零
            sid = getattr(event, "sid", "?")
            # 把"本轮有没有用工具"记为"上一轮"，供下一轮判定使用
            self.thinking.commit_turn(sid)
            d = self.thinking.last_decision(sid)
            # ★★ 自动思考**没开**时，日志里**完全不出现**"思考"这个字段。
            #
            #   为什么不是"换个委婉的说法"：用户**可能自己在提供商那边开了思考**
            #   （那就与我们无关）。日志里出现"思考=未启用"会让人以为
            #   **插件把思考关掉了** —— 既吓人又误导。
            #   所以：功能没开 ⇒ 这个字段**根本不出现**，不留任何能误读的字样。
            #
            #   启用之后才显示，而且不写光秃秃的"关"，带上得分与信号，
            #   明确这是**本轮判定结果**、不是全局开关。
            if not self.thinking_enabled:
                logger.info(
                    "[accel] 一轮结束 sid=%s steps=%d 抢先发=%d 首段=%.2fs",
                    sid, len(getattr(final_result, "step_results", []) or []),
                    self._stats["early_sent"],
                    self._stats["first_seg_s"] or 0.0,
                )
                return

            if d is None:
                think_txt = "—"
            elif d.enabled:
                think_txt = "开(%s,%s)" % (d.effort, "/".join(d.signals))
            else:
                think_txt = "关(得分%.1f)" % d.score
            # 把"这轮扫了多少字"一并打出来 —— 扫错文本时一眼可见
            # （曾经因为把插件注入的记忆当用户消息扫，导致每轮都触发思考）
            scanned = getattr(d, "scanned_chars", None)
            if scanned is not None:
                think_txt += "[%d字]" % scanned
            logger.info(
                "[accel] 一轮结束 sid=%s steps=%d 抢先发=%d 首段=%.2fs 思考=%s",
                sid, len(getattr(final_result, "step_results", []) or []),
                self._stats["early_sent"],
                self._stats["first_seg_s"] or 0.0,
                think_txt,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 观测失败")

    # ══════════════════════════════════════════════════════════
    # L2 请求优化
    # ══════════════════════════════════════════════════════════
    def _clear_stale_round_state(self):
        """清掉**明显陈旧**的本轮状态（双保险，不依赖 final_result 一定执行）。

        ★ 为什么需要：`_clear_round_state` 挂在 `@on.final_result` 上，而事件处理器
          的循环是「按优先级从高到低、遇 `event.stop()` 就 break」
          ⇒ **排在后面的处理器可能不执行**。
          一旦没执行，台账与"已消费"标记就会**残留到下一轮**
          ⇒ 下一轮要么不剥离（**重复**）、要么误剥离（**丢内容**）。
        ⇒ 这里在**每轮请求前**兜一次底：凡是超过 10 分钟没动过的状态一律清掉。
          正常一轮远短于 10 分钟，所以不会误清正在用的状态。
        """
        try:
            now = time.time()
            # ⚠️ 必须遍历**四个字典的键并集**，不能只看 _ledger_ts ——
            #    否则某些键（比如只写过 `_ledger_consumed` 的）永远清不掉 ⇒
            #    多群聊场景下这几个字典会**随会话数缓慢增长**（内存泄漏）。
            keys = set()
            for d in (self._ledger_ts, self._sent_ledger, self._ledger_consumed,
                      self._resp_by_sid):
                if isinstance(d, dict):
                    keys |= set(d.keys())
            for k in keys:
                ts = float(self._ledger_ts.get(k, 0) or 0) if isinstance(self._ledger_ts, dict) else 0
                # 没有时间戳的（陈旧遗留）按「更早」处理 ⇒ 一并清掉
                if ts and (now - ts) <= 600:
                    continue
                self._ledger_ts.pop(k, None)
                self._sent_ledger.pop(k, None)
                self._sent_ledger.pop(k + "\x00ev", None)
                self._ledger_consumed.pop(k, None)
                self._resp_by_sid.pop(k, None)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 陈旧状态清理失败（不影响请求）")

    @on.llm_request(priority=Priority.LOW)
    async def capture_and_optimize(self, event: KiraMessageBatchEvent, req: LLMRequest,
                                   tag_set=None, *_):
        """排在 Priority.LOW ⇒ 在大多数插件之后收口，顺序确定。

        只做两件事（**刻意不做工具裁剪**，见 README「为什么不做工具裁剪」）：
          1. 记录本轮上下文（sid/event/tag_set），供抢先发送使用
          2. 自动思考判定（结果在 request 上打标记，由 _build_request_kwargs 注入）
        """
        # ★ 双保险：先清掉**明显陈旧**的本轮状态（若 final_result 因 processor
        #   被跳过而没执行，这里能兜住 ⇒ 不会残留到下一轮造成重复/误剥）。
        self._clear_stale_round_state()
        sid_now = getattr(event, "sid", None)
        # 仅供面板展示"最近一次判定属于哪个会话"，功能路径一律用 req 上的 SendCtx
        self._current_sid = sid_now
        req.__dict__["_accel_ctx"] = SendCtx(sid_now, event, tag_set)
        # ★★ 把 event 也挂上 —— 思考判定要用它做**结构判据**
        #   （从 event.messages[*].chain 的 Text 元素取"用户真正打的字"，
        #   从而把图片/表情包描述天然排除）。
        #   ⚠️ 漏了这一行 ⇒ 结构判据永远拿不到 event ⇒ 落到字符串兜底 ⇒
        #      会把别的插件注入的内容（如记忆·Z 的记忆）当成用户消息扫。
        #      实测：日志里 `[1021字]` 而用户只打了 50 字，就是这个原因。
        req.__dict__["_accel_event"] = event
        # ★ 新一轮开始 ⇒ 清掉该会话的抢发台账（否则上一轮的残留会让本轮**多切**=丢内容）
        self._reset_turn_ledger(sid_now)

        if self.thinking_enabled and sid_now:
            decision = self.thinking.decide(sid_now, req)
            req.__dict__["_accel_thinking"] = decision
            if decision.enabled:
                self._stats["thinking_on"] += 1
            elif "超预算" in decision.signals:
                self._stats["thinking_skipped_budget"] += 1
            else:
                self._stats["thinking_off"] += 1

    @on.tool_result(priority=Priority.LOW)
    async def on_tool_result(self, event: KiraMessageBatchEvent, tool_result, *_):
        """**只读**：把"上轮工具失败"这个信号喂给自动思考判定。

        刻意**不修改** tool_result.text（见 README「为什么不做结果瘦身」）：
          - 框架把结果写回 resp 是在所有 ON_TOOL_RESULT handler 之后
            （session_merger 的注释也点明了这个时序陷阱）
          - 实测 midflight 与 session_merger 都会读 `tool_result.text`，
            改动会影响它们的判断
          - 内容一旦被截断，模型会以为自己拿到了全文
        """
        sid = getattr(event, "sid", "")
        if not sid:
            return
        # 「上一轮用过工具」信号的数据来源
        self.thinking.note_turn_tool(sid)
        try:
            text = getattr(tool_result, "text", "") or ""
            head = text[:200].lower()
            if "error" in head or "失败" in head or "denied" in head or "not allowed" in head:
                self.thinking.note_tool_error(sid)
            else:
                self.thinking.note_tool_ok(sid)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 工具结果信号读取失败（已忽略）")

    # ══════════════════════════════════════════════════════════
    # L3 接管
    # ══════════════════════════════════════════════════════════
    def _install_client_cache(self) -> None:
        try:
            from core.utils import model_clients as mc
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 model_clients 失败")
            return

        cache = self._client_cache

        def factory(original):
            def make(self):
                # ★ 键必须覆盖 _build_client 真正用到的一切：
                #   api_key / base_url / default_headers。
                #
                #   以前键里有 `id(self.model.provider_config)`：既不可靠
                #   （id 会被解释器复用），又**漏了 headers** ——
                #   用户改了提供商的自定义 header 后，base_url 与 api_key 没变就会
                #   命中旧客户端，新 header 一直不生效（要等插件重载才恢复）。
                pc = self.model.provider_config or {}
                adv = pc.get("section_advanced") or {}
                hdr = adv.get("headers") if isinstance(adv, dict) else None
                key = (pc.get("base_url", ""), pc.get("api_key", ""),
                       repr(sorted(hdr.items())) if isinstance(hdr, dict) else "")
                c = cache.get(key)
                if c is None:
                    c = original(self)
                    if len(cache) > 32:          # 防着配置反复变更把缓存撑大
                        cache.clear()
                    cache[key] = c
                return c
            return make

        h = PatchHandle("reuse_http_client")
        if install(h, mc.OpenAICompatibleLLMClient, "_build_client", factory) is not None:
            self.patches.add(h)

    # ══════════════════════════════════════════════════════════
    # L4 流式引擎（强制流式 + 抢先发送）
    # ══════════════════════════════════════════════════════════
    def _install_stream_engine(self) -> None:
        """patch `ProviderManager.get_model_client`，把返回的 client 包一层代理。

        为什么选这里而不是 patch 某个 client 类的 chat()：
          ① **一次性覆盖所有 provider**（OpenAI / DeepSeek / Anthropic / 任何插件注册的）
          ② 插件自己注册的 provider 也自动生效
          ③ **不改任何客户端的 `chat_stream`** ⇒ 与 openai-stream-compat 之类的
             provider 插件天然兼容（它重写的 chat 会先跑，我们再包一层）
        """
        try:
            from core.provider.provider_manager import ProviderManager
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 provider_manager 失败")
            return

        plugin = self
        proxy_cache = self._proxy_cache
        allowed = set(self.stream_providers)

        # ★ 缓存上限：provider 反复重建时旧 proxy 会累积（且键是 id(client)，
        #   id 复用会造成脏命中）。超过上限就整体清空重建 —— 代价只是下次多包一层。
        MAX_PROXY_CACHE = 64

        def factory(original):
            def get_model_client(self, provider_id, model_id, model_type=None):
                client = original(self, provider_id, model_id, model_type)
                if client is None:
                    return None
                # 只包装 LLM 客户端（其它类型如 TTS/STT 无流式语义）
                if type(client).__name__.endswith("EmbeddingClient") or \
                   type(client).__name__.endswith("ImageClient"):
                    return client
                if not hasattr(client, "chat") or not hasattr(client, "chat_stream"):
                    return client

                # provider 白名单（空=全部）
                # ★★ 多选下拉给的值是 `providerId:modelId`（框架 source:'model' 的格式），
                #   所以三种写法都要认，否则用户从下拉里选了也匹配不上：
                #     · "providerId:modelId"（模型级，下拉默认给这个）
                #     · "providerId"（提供商级，兼容旧配置 / 手填）
                #     · "providerName"（名称，最老的写法，继续兼容）
                if allowed:
                    m0 = getattr(client, "model", None)
                    pname = getattr(m0, "provider_name", "") or ""
                    pid = getattr(m0, "provider_id", "") or ""
                    mid = getattr(m0, "model_id", "") or ""
                    if not ({pname, pid, f"{pid}:{mid}"} & allowed):
                        return client

                # 用 (provider_id, model_id) 而非 id(client) 做键：
                # id 会被解释器复用，拿它当键在对象被 GC 后可能命中"别人的"代理。
                m = getattr(client, "model", None)
                key = (getattr(m, "provider_id", ""), getattr(m, "model_id", ""))
                if len(proxy_cache) > MAX_PROXY_CACHE:
                    proxy_cache.clear()
                    logger.info("[accel] 流式代理缓存超过 %d，已重置", MAX_PROXY_CACHE)
                cached = proxy_cache.get(key)
                if cached is not None:
                    return cached
                proxy = LLMClientProxy(client, plugin._make_engine)
                proxy_cache[key] = proxy
                logger.info("[accel] 已为 %s/%s 启用流式代理",
                            getattr(m, "provider_name", "?"), getattr(m, "model_id", "?"))
                return proxy
            return get_model_client

        h = PatchHandle("stream_engine")
        if install(h, ProviderManager, "get_model_client", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 流式引擎已安装（强制流式=%s 抢先发送=%s providers=%s）",
                        self.force_stream, self.early_send, sorted(allowed) or "ALL")

    def _make_engine(self, request=None) -> StreamEngine:
        """建一个引擎实例。request 必须传进来 —— 发送上下文挂在它身上。"""
        ctx = ctx_of(request) if request is not None else None
        emit = None
        if self.early_send:
            async def _emit(seg: str) -> None:
                await self._emit_segment(seg, ctx)
            emit = _emit

        def _remember(resp):
            # 记录本轮响应，供发送层（send_xml_messages）读取标记做剥离。
            # ★ 按 sid 分开存：并发会话下用一个实例变量会拿错响应
            #   ⇒ 要么重复发送、要么把内容丢掉。
            if ctx is not None and ctx.sid:
                self._resp_by_sid[ctx.sid] = resp
                if len(self._resp_by_sid) > 32:          # 防泄漏
                    self._resp_by_sid.clear()
                    self._resp_by_sid[ctx.sid] = resp
            self._current_resp = resp                    # 仅供观测

        return StreamEngine(force_stream=self.force_stream, emit=emit, on_complete=_remember)

    @staticmethod
    def _has_sendable(xml_data: str) -> bool:
        """剥离后的剩余文本里，有没有**真会发出去**的段。

        只是空占位符（<msg/>）就不需要为它补间隔 —— 什么都没发，等它没有意义。
        """
        try:
            from .early_sent import _MSG_CLOSED
        except Exception:  # noqa: BLE001
            _MSG_CLOSED = None
        if not xml_data:
            return False
        chunks = _MSG_CLOSED.findall(xml_data) if _MSG_CLOSED else []
        if not chunks:
            return False
        for c in chunks:
            c = c.strip()
            if c not in ("<msg/>", "<msg />") and len(c) > len("<msg></msg>"):
                return True
        return False

    async def _pace(self, ctx: "SendCtx") -> None:
        """发送**前**等够 min/max_message_delay（第一段不等，保证首字快）。

        间隔值直接取框架自己的配置（`_frame_delay`），插件不自造第二个数字。
        """
        lo, hi = self._frame_delay()
        if hi <= 0:
            self._last_seg_ts.pop(ctx.sid, None)
            return
        last = self._last_seg_ts.get(ctx.sid)
        if last is not None:
            wait = random.uniform(lo, hi) - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)

    def _mark_sent(self, ctx: "SendCtx") -> None:
        """记下"刚刚发出"的时刻（下一段据此计算还要等多久）。"""
        if self._frame_delay()[1] > 0:
            # ★ 上限：长期运行的机器人会积累很多会话，这条映射不能无限长。
            #   超了就整体清掉重建 —— 代价只是"下一次发送不等待"，
            #   不会出错（比留着一堆死会话的条目更划算）。
            if len(self._last_seg_ts) > MAX_PACING_SIDS:
                self._last_seg_ts.clear()
            self._last_seg_ts[ctx.sid] = time.monotonic()

    async def _broadcast_after_xml_parse(self, ctx: "SendCtx", actions: list) -> list:
        """广播 AFTER_XML_PARSE，并返回**插件改写后**的待发列表。

        为什么必须有：抢先发送绕过了框架的 send_xml_messages，而那个函数里
        除了 ON_MESSAGE_SENT，还派发 AFTER_XML_PARSE。实测 qq-enhance 的
        「表情独立成行」就挂在这个钩子上 —— 不广播 ⇒ 功能失效。

        容错原则：钩子出问题**不能拖垮发送**。任何一个 handler 抛异常时，
        框架自己的 exec_handler 已经会捕获并转成异常事件，这里再兜一层：
        最坏情况是"这次没被插件改写"，而不是"消息发不出去"。
        """
        try:
            from core.plugin.plugin_handlers import event_handler_reg, EventType
            if ctx.event is None:
                return actions
            for handler in event_handler_reg.get_handlers(EventType.AFTER_XML_PARSE):
                try:
                    await handler.exec_handler(ctx.event, actions)
                except Exception:  # noqa: BLE001
                    logger.exception("[accel] AFTER_XML_PARSE 的某个处理器出错（继续发送）")
                if getattr(ctx.event, "is_stopped", False):
                    logger.info("[accel] AFTER_XML_PARSE 阶段事件被停止")
                    break
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 广播 AFTER_XML_PARSE 失败（按未改写的内容发送）")
        return actions

    def _reset_turn_ledger(self, sid: str) -> None:
        """**每轮开始时**清掉该会话的抢发台账。

        ★ 为什么必须清（这是个真实隐患，不是洁癖）：
          台账原来按 sid 累加且**从不清零**。如果某一轮"抢发了但没走到
          send_xml_messages"（事件被 stop、或中途换轮），残留值就会让**下一轮
          多切**若干段 ⇒ **丢内容**（比重复发送更糟）。
        """
        if sid:
            try:
                self._sent_ledger.pop(sid, None)
                self._sent_ledger.pop(sid + "\x00ev", None)
                # ★★ 必须**一起清掉响应与结果**，否则会用到**上一轮的响应**：
                #   `_resp_by_sid[sid]` 存的是上一轮的响应，若这一轮先走到发送层，
                #   拿到的 n / segs 全是旧的 ⇒
                #     · 按内容匹配必然失败（文本完全不同）⇒ 日志里那条告警
                #     · n 与实际不符 ⇒ 可能**多切 = 丢内容**（比重复更糟）
                #   （用户实测日志：`抢先发=46` 是累计值，而本轮 n=1 ⇒ 明显用了旧响应）
                self._resp_by_sid.pop(sid, None)
                self._early_results.pop(sid, None)
            except Exception:  # noqa: BLE001
                pass

    async def _emit_segment(self, seg: str, ctx: "SendCtx" = None) -> bool:
        """把一段已闭合的 <msg> 真正发出去，并补广播 ON_MESSAGE_SENT。

        ★★ 投递契约（修「重复发送」的关键）：
           **返回值 True = 这一段确实投递出去了；False = 没投递。**
           而且**绝不向上抛异常**。

        为什么要有这个契约：
          发送是**不可撤销**的副作用，但原来它被当成普通调用 ——
          `_emit_segment` 在投递成功之后还要记账、补广播、写统计，
          这些只要抛异常，异常就冒到 StreamEngine 的 except ⇒ `mark_failed()`
          ⇒ **那一段不计入 emitted** ⇒ 已发出去的内容被当成没发
          ⇒ 框架剥离少切一段 ⇒ **用户看到最后一段重复**（线上截图就是这样）。

          记账/补广播属于"投递之后的杂事"，失败只该告警，绝不该改变
          "这一段到底发出去没有"这个事实。

        ctx 必须来自**这一次**请求（request 上挂的 SendCtx）——
        用实例变量会在并发会话下把消息发到别的会话去。
        """
        import asyncio

        mp = getattr(self.ctx, "message_processor", None)
        if ctx is None or mp is None or not ctx.sid or ctx.tag_set is None:
            logger.error("[accel] 抢先发送上下文不完整，本段交回框架发送")
            return False

        # 解析失败 ⇒ 没投递过任何东西，交回框架（由它统一报错/处理）
        try:
            actions = await mp._parse_xml_msg(seg, ctx.tag_set)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 抢先发送：解析失败，本段交回框架")
            return False

        # ★★ 必须广播 AFTER_XML_PARSE —— 否则「表情独立成行」这类功能会失效。
        #
        #   实测（qq-enhance 插件）：
        #       @on.after_xml_parse(priority=Priority.HIGH)
        #       async def process_stickers(self, event, message_chains: list):
        #           ... 把含表情的链拆成「文字一条 + 每个表情单独一条」
        #           message_chains.clear()
        #           message_chains.extend(new_chains)     # ← 就地改写
        #
        #   框架正是靠这个列表的**可变性**生效：
        #       actions = await self._parse_xml_msg(...)
        #       for handler in AFTER_XML_PARSE handlers: await handler.exec_handler(event, actions)
        #       for action in actions: await self.send_message_chain(...)
        #
        #   我们抢先发送时绕过了 send_xml_messages，如果只补 ON_MESSAGE_SENT、
        #   不补 AFTER_XML_PARSE，插件就**完全看不到**这次发送
        #   ⇒ 表情被塞在文字里一起发出去（用户实测的功能失效）。
        #
        #   语义与框架一致：**让钩子改写列表，然后按改写后的内容发送**。
        actions = await self._broadcast_after_xml_parse(ctx, actions)

        delivered = False
        from core.chat import MessageChain

        for action in actions:
            if not isinstance(action, MessageChain) or action.is_empty():
                continue
            # ★ 段间隔：**发送前**补足与上一段的间隔（第一段不等 ⇒ 首字要快）
            #
            #   为什么必须在"发送前"而不是"发送后"：
            #     框架是在每次发送**之后** sleep，所以它的第 1 段照发、第 2 段前
            #     已经等过一次。我们若把等待放在发送后、又让第 1 段跳过等待，
            #     **第 2 段就会紧贴着第 1 段出去**（实测间隔 0s）——
            #     这正是用户报的"最小/最大间隔没生效"。
            await self._pace(ctx)

            # ── 投递本身：这一步成功即等于"已经发出去了"，后面任何失败都不改这个事实 ──
            try:
                result = await mp.send_message_chain(ctx.sid, action)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 抢先发送失败，本段交回框架发送（不丢内容）")
                return delivered
            delivered = True

            # ── 以下都是"投递之后的杂事"，失败只告警，绝不改变 delivered ──
            try:
                # ★ 记下这一段的结果：发送层的剥离会把"已抢先发出的段"从文本里去掉，
                #   而框架的 _add_message_ids 是**按位置**把 message_results 贴到 <msg> 上的。
                #   只还回"剩余段"的结果 ⇒ 位置全错 ⇒ 模型在自己历史里读到**错位的
                #   message_id**（提示词明确说这个 ID 由系统添加，模型会用它引用消息）。
                self._early_results.setdefault(ctx.sid, []).append(result)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 记录抢发结果失败（不影响已投递的事实）")

            try:
                self._mark_sent(ctx)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 记录发送时刻失败（不影响已投递的事实）")

            # ★ 补广播 ON_MESSAGE_SENT（绕过框架发送层就必须补）
            try:
                from core.plugin.plugin_handlers import event_handler_reg, EventType
                if ctx.event is not None:
                    for handler in event_handler_reg.get_handlers(EventType.ON_MESSAGE_SENT):
                        await handler.exec_handler(ctx.event, action, result)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 补广播 ON_MESSAGE_SENT 失败")

            try:
                self._stats["early_sent"] += 1
                if self._stats["first_seg_s"] is None:
                    self._stats["first_seg_s"] = 0.0
            except Exception:  # noqa: BLE001
                pass

        # ★ 旁证台账：本轮这个会话一共抢发了几段。
        #   发送层的剥离优先用响应上的标记；标记万一取不到（字典被清、响应换过、
        #   sid 不一致），就用这份台账兜底 —— 否则 `n=0` ⇒ 剥离不发生
        #   ⇒ **整份回复被重新发一遍**（比漏切一段严重得多）。
        if delivered and ctx is not None and ctx.sid:
            try:
                # ★★★ 台账**带轮次 id**：上一轮的残留绝不能参与本轮的剥离
                #   （否则"本轮文本恰好与上一轮相同"时会被误剪 = 丢内容）。
                #   轮次变了就**重新开账** —— 这也是**不依赖任何钩子执行**的清理方式，
                #   比"等 final_result 来清"稳得多（那个钩子可能被跳过）。
                cur_ev = getattr(getattr(ctx, "event", None), "event_id", None)
                if self._sent_ledger.get(ctx.sid + "\x00ev") != cur_ev:
                    self._sent_ledger[ctx.sid] = []
                    self._sent_ledger[ctx.sid + "\x00ev"] = cur_ev
                self._sent_ledger.setdefault(ctx.sid, []).append(seg)
                # 记"最后一次活动时间"，供 `_clear_stale_round_state` 判断陈旧
                if not isinstance(getattr(self, "_ledger_ts", None), dict):
                    self._ledger_ts = {}
                self._ledger_ts[ctx.sid] = time.time()
            except Exception:  # noqa: BLE001
                pass

        return delivered

    # ══════════════════════════════════════════════════════════
    # L5 自动思考注入
    # ══════════════════════════════════════════════════════════
    def _install_request_hook(self) -> None:
        """patch 各家的请求构造方法，注入思考参数。

        ★ 为什么必须挂**三个**点（2026-09-23 实测，用户提问引出）：
            OpenAICompatibleLLMClient._build_request_kwargs
                ← 阿里 / 硅基 / 火山 / 魔搭 / OpenAI（子类都继承它）
            DeepSeekLLMClient._build_request_kwargs
                ← **直接继承 LLMModelClient**，不是 OpenAI 兼容的子类
            AnthropicCompatibleLLMClient._build_request_body
                ← 方法名都不一样（不是 _build_request_kwargs）
        只挂第一个 ⇒ **DeepSeek / Anthropic 用户的自动思考完全无效**
        （实测补丁前后请求体一模一样，开关等于摆设）。

        且三家"参数放哪"也不同 —— 交给 auto_thinking.apply_thinking_params 路由：
            OpenAI 兼容系 → extra_body
            DeepSeek      → thinking 进 extra_body，reasoning_effort 必须顶层
            Anthropic     → thinking 必须顶层（且 budget_tokens 要 < max_tokens）

        我们不改任何配置文件，热重载即生效。
        """
        targets = []
        try:
            from core.utils import model_clients as mc
            targets.append((mc.OpenAICompatibleLLMClient, "_build_request_kwargs",
                            "openai"))
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 OpenAI 兼容客户端失败，跳过该注入点")
        try:
            from core.provider.src.deepseek.model_clients import DeepSeekLLMClient
            targets.append((DeepSeekLLMClient, "_build_request_kwargs", "deepseek"))
        except Exception:  # noqa: BLE001
            logger.warning("[accel] 未找到 DeepSeek 客户端，跳过该注入点")
        try:
            from core.provider.src.anthropic.model_clients import (
                AnthropicCompatibleLLMClient,
            )
            targets.append((AnthropicCompatibleLLMClient, "_build_request_body",
                            "anthropic"))
        except Exception:  # noqa: BLE001
            logger.warning("[accel] 未找到 Anthropic 客户端，跳过该注入点")

        plugin = self
        installed = []

        for cls, attr, kind in targets:
            def make_factory(kind=kind):
                def factory(original):
                    def build(self, request, **overrides):
                        kwargs = original(self, request, **overrides)

                        # ★ 请求体兼容加固：把空白 content 规范成 None
                        #   （Gemini 等网关不接受 content:""，会让该模型持续不可用）
                        if (kind == "openai" and plugin.normalize_empty_content
                                and isinstance(kwargs.get("messages"), list)):
                            from .request_compat import normalize_messages
                            n = normalize_messages(kwargs["messages"])
                            if n:
                                plugin._stats["content_normalized"] = (
                                    plugin._stats.get("content_normalized", 0) + n)

                        return plugin._apply_thinking(kwargs, request, kind, self)
                    return build
                return factory

            h = PatchHandle("auto_thinking:%s" % kind)
            if install(h, cls, attr, make_factory()) is not None:
                self.patches.add(h)
                installed.append(kind)

        logger.info("[accel] 请求钩子已安装（自动思考=%s 规范空白=%s 注入点=%s）",
                    self.thinking_enabled, self.normalize_empty_content, installed)

    def _apply_thinking(self, kwargs: dict, request: Any, client_kind: str,
                        client: Any = None) -> dict:
        """按本轮判定，把思考参数合并进请求体（位置由 apply_thinking_params 决定）。"""
        if not self.thinking_enabled:
            return kwargs
        decision = request.__dict__.get("_accel_thinking")
        if decision is None:
            return kwargs

        style = resolve_style(self.thinking_style, client_kind)
        if decision.enabled:
            params = build_thinking_extra_body(style, decision.effort)
            # ★ 首次真正注入时打一条日志：让"到底发了什么参数、走哪个风格"
            #   成为可见事实 —— 不然用户只能猜自己选的风格对不对
            #   （provider 类型 与 风格 是两个不同的东西：前者决定参数放哪，
            #     后者决定用哪套字段名；选错也只会"静默不生效"）。
            key = (client_kind, style)
            if key not in getattr(self, "_thinking_logged", set()):
                if not hasattr(self, "_thinking_logged"):
                    self._thinking_logged = set()
                self._thinking_logged.add(key)
                logger.info("[accel] 思考参数已注入（provider 类型=%s ⇒ %s 位置，"
                            "风格=%s）：%s", client_kind,
                            "extra_body" if client_kind != "deepseek" else "混合",
                            style, params)
            if self.thinking_follow_provider:
                # 开关打开：只负责"开"，强度完全提供商配的那个值
                params = follow_provider_effort(
                    params, provider_effort(getattr(client, "model", None)))
        elif self.thinking_inject_nothink:
            params = build_nothinking_extra_body(style)
        else:
            return kwargs
        return apply_thinking_params(kwargs, params, client_kind)


    def _install_parallel_tools(self) -> None:
        """同轮工具并行执行（默认关）。

        ★ 只并行"执行工具"这一段；其余全部保持串行且按序：
          - `max_tool_calls_per_turn` 超限的告警条目：原地保留
          - `ON_TOOL_RESULT` 钩子：**串行、按序**（插件会在这里 event.stop()，
            框架的实现会因此"提前返回 + 只写部分 tool_results"，
            session_merger 的注释明确点出过这个时序）
          - `assemble_result()`：串行、按序
          - `resp.tool_results` 的写入顺序：**与 tool_calls 对齐**（乱了会 400）

        ⇒ 所以并行只发生在"调用工具函数"这一层，可观测行为与串行版**完全一致**，
          区别只是"等待时间"。
        """
        try:
            from core.agent.func_tool_manager import FuncToolManager
            from core.provider.llm_model import LLMResponse  # noqa: F401
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 func_tool_manager 失败，跳过工具并行")
            return

        import json as _json
        from .parallel_tools import ToolGate

        try:
            from core.utils.tool_utils import BaseTool  # noqa: F401
        except Exception:  # noqa: BLE001
            pass

        gate = self._tool_gate
        plugin = self

        def factory(original):
            async def execute_tool(self, event, resp, tool_set=None):
                calls = getattr(resp, "tool_calls", None) or []

                # 先读框架自己的两个配置（与原实现一致）
                max_calls = self.kira_config.get_config("bot_config.agent.max_tool_calls_per_turn")
                try:
                    max_calls = int(max_calls)
                except (TypeError, ValueError):
                    max_calls = 5

                to_run = calls[:max_calls] if max_calls >= 0 else []
                ok, reason = gate.can_parallelize(to_run)
                if not ok:
                    return await original(self, event, resp, tool_set=tool_set)

                plugin._stats["parallel_batches"] = plugin._stats.get("parallel_batches", 0) + 1
                logger.info("[accel] 同轮工具并行：%s", reason)

                # ── 阶段一：并发执行（只这一层并行）──
                timeout = self.kira_config.get_config("bot_config.agent.tool_call_timeout")
                try:
                    timeout = float(timeout)
                    if timeout <= 0:
                        timeout = None
                except (TypeError, ValueError):
                    timeout = 60

                def parse_args(tc):
                    raw = (tc.get("function") or {}).get("arguments") or ""
                    try:
                        return {} if not raw.strip() else _json.loads(raw)
                    except Exception:  # noqa: BLE001
                        return {}

                async def run_one(tc):
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    args = parse_args(tc)
                    if not (tool_set and name in tool_set):
                        return {"__error": f"Tool {name} not implemented"}
                    try:
                        inst = tool_set.get(name)
                        coro = inst.execute(event, **args)
                        import asyncio as _a
                        return await (_a.wait_for(coro, timeout) if timeout else coro)
                    except Exception as e:  # noqa: BLE001 —— 与框架一致：任何异常都变成结果
                        return {"__error": f"Failed to call tool '{name}': {e}"}

                import asyncio as _a
                raw_results = await _a.gather(
                    *[run_one(tc) for tc in to_run], return_exceptions=False)

                # ── 阶段二：按序做"不能并行"的部分（与框架逐行等价）──
                from asyncio import wait_for, TimeoutError as AsyncTimeoutError
                from core.agent.tool import ToolResult
                from core.plugin.plugin_handlers import event_handler_reg, EventType
                import core.agent.func_tool_manager as _ftm

                for idx, tool_call in enumerate(calls):
                    tool_call_id = tool_call.get("id")
                    name = (tool_call.get("function") or {}).get("name")

                    if max_calls >= 0 and idx >= max_calls:
                        warn_msg = (f"Tool call limit exceeded: maximum {max_calls} "
                                    f"tool calls per turn, skipping tool '{name}'.")
                        _ftm.tool_logger.warning(warn_msg)
                        resp.tool_results.append({
                            "role": "tool", "tool_call_id": tool_call_id,
                            "name": name, "content": warn_msg,
                        })
                        continue

                    result = raw_results[idx]
                    if isinstance(result, dict) and "__error" in result:
                        result = {"error": result["__error"]}
                        _ftm.tool_logger.error(result["error"])

                    tool_result_obj = result if isinstance(result, ToolResult) else ToolResult(str(result))

                    # ON_TOOL_RESULT：串行、按序（保持 event.stop 语义）
                    for handler in event_handler_reg.get_handlers(event_type=EventType.ON_TOOL_RESULT):
                        await handler.exec_handler(event, tool_result_obj)
                        if event.is_stopped:
                            logger.info("Event stopped while ON_TOOL_RESULT stage")
                            return

                    content = await tool_result_obj.assemble_result()
                    _ftm.tool_logger.info(f"tool_result: {content}")
                    resp.tool_results.append({
                        "role": "tool", "tool_call_id": tool_call_id,
                        "name": name, "content": content,
                    })
            return execute_tool

        from .patches import PatchHandle, install
        h = PatchHandle("parallel_tools")
        if install(h, FuncToolManager, "execute_tool", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 同轮工具并行已安装（白名单=%s 黑名单=%d条 全字匹配=%s）",
                        gate.whitelist or "全部", len(gate.blacklist), gate.match_exact)

    def _frame_delay(self) -> tuple[float, float]:
        """读框架的消息发送间隔（用户自己的配置）。

        框架在 MessageProcessor.__init__ 里一次性读取：
            self.min_message_delay = bot_config.min_message_delay (默认 2)
            self.max_message_delay = bot_config.max_message_delay (默认 5)
        我们直接取这两个属性 —— 这样用户调 KiraAI 的配置就能直接生效，
        插件不再引入第二个"要调的地方"。
        """
        mp = getattr(self.ctx, "message_processor", None)
        if mp is None:
            return 0.0, 0.0          # 拿不到就不等（安全默认）
        try:
            lo = float(getattr(mp, "min_message_delay", 0.0) or 0.0)
            hi = float(getattr(mp, "max_message_delay", lo) or lo)
            return (min(lo, hi), max(lo, hi))
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    def _install_memory_dump(self) -> None:
        """让记忆落盘去掉 `indent=4`（只改格式，不改内容）。

        实测（2026-09-23 审计）——**先说结论：默认关**。
          它发生在 agent loop 结束【之后】（回复已经发出），所以**不拖慢回复**，
          只占事件循环、影响其它并发会话与下一轮。而且真实规模下收益很小：
              1 会话 × 50 条   0.9ms → 0.2ms   （约省 0.7ms，无感）
              3 会话 × 100 条  2.9ms → 0.7ms   （约省 2ms）
              10 会话 × 300 条 22.3ms → 4.2ms  （约省 18ms）
              50 会话 × 300 条 126ms → 23ms    （约省 100ms，这种规模才值得开）
          所以默认**关**（保住 chat_memory.json 的可读性），
          只有多群大规模部署才建议打开。

        它的成本（框架侧的现状）：
          `SessionManager.update_memory` 每轮都会把**全部会话**的记忆重新
          `json.dumps` 再整文件重写，而且是**同步**执行（会阻塞事件循环）。
          缩进在其中占大头：50 会话 × 400 条时 165.8ms vs 26.6ms（6.2 倍）。

          去掉缩进后 `json.load` 解析出来的对象**完全一样** ⇒ 对任何读这个文件的
          代码都零影响。已实测确认：
            · 框架自己的 `_load_memory` 读紧凑文件正常
            · WebUI 的会话历史走 `get_existing_memory_snapshot`（**内存快照**，
              根本不读文件）—— 取回的 50 条历史与原始数据逐字段相同
            · 新旧两种格式读回来的对象深度相等

        ★ 为什么**不**顺便做这两件（收益更大但会破坏兼容）：
          1) 改成异步/丢线程池 —— `update_memory` 是被**不带 await** 调用的
             （message_manager.py:832），改成协程会让"直接调用它的插件"
             拿到一个永远不会执行的协程 ⇒ 记忆静默丢失。
          2) 只重序列化"变了的会话"（能到 <1ms）—— 需要在会话数据被就地修改时
             可靠地失效缓存，一旦漏标就会把**过期的记忆写回磁盘**（丢数据）。
             拿记忆的正确性换几十毫秒，不划算。
        """
        try:
            from core.chat.session_manager import SessionManager
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 SessionManager 失败，跳过落盘优化")
            return

        def factory(original):
            def _save_memory(self, memory=None, path=None):
                if not memory:
                    memory = self.chat_memory
                if not path:
                    path = self.chat_memory_path
                try:
                    # 与框架唯一的不同：不缩进 + 紧凑分隔符。
                    # 解析结果完全相同，只是文件不再是给人看的排版。
                    text = json.dumps(memory, ensure_ascii=False,
                                      separators=(",", ":"))
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[accel] 记忆落盘失败 {path}: {e}")
                    return False
            return _save_memory

        h = PatchHandle("memory_dump")
        if install(h, SessionManager, "_save_memory", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 记忆落盘已优化（去掉缩进：序列化快约 6 倍）")

    def _install_early_sent_strip(self) -> None:
        """发送时剥离"已抢先发出的段"，避免重复发送。

        ★ 为什么不在构造响应时就剥离（那是我的第一版做法，有兼容问题）：
          实测框架内置 kira-ai 插件（builtin_plugins/kira-ai/main.py:111）和
          sustained-chat 插件（main.py:1681）都会在 ON_LLM_RESPONSE 里
          把 `resp.text_response` 当作**模型的完整输出**来用。
          提前剥离会让它们只看到尾巴 ⇒ XML 校验误判、或误判"AI 没说话"。

          所以现在：`resp.text_response` 保持完整；"哪些段已发出"记在
          响应对象的私有标记里；真正发送时（本函数）才把它们去掉。

        插在 `MessageProcessor.send_xml_messages` 上（它是类方法，可干净 patch）：
          - 正常轮次：剥离后交给原实现发送剩余部分
          - 全部发完：传 "<msg/>"，框架解析为空消息，不会重复发
        """
        try:
            from core.message_manager import MessageProcessor
            from .early_sent import early_sent_count
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 MessageProcessor 失败，跳过发送层剥离")
            return

        plugin = self

        def factory(original):
            async def send_xml_messages(self, event, xml_data, tag_set):
                # ★ 按 sid 取"本轮响应"。
                #   ⚠️ 刻意**不**回退到 self._current_resp：那是实例级的，
                #   并发会话下可能是**别的会话**的响应，拿它去剥离就会
                #   把本会话还没发过的内容剪掉（丢消息）。
                #   查不到就按"没抢发过"处理（n=0，全部交给框架发）——
                #   最坏是重复一条，而不是丢内容。
                sid_now = getattr(event, "sid", None)
                resp = plugin._resp_by_sid.get(sid_now) if sid_now else None
                n = early_sent_count(resp) if resp is not None else 0

                # ★★★ 台账是**权威来源**，优先用它（不再叫"兜底"，也不再告警）。
                #
                #   为什么台账优先：
                #     · `_sent_ledger` 是在**发送的那一刻**逐段记下的 ——
                #       它是"确实发出去了"的第一手记录；
                #     · `_accel_early_sent_count` 只是同一份信息挂在响应对象上的**拷贝**，
                #       而框架在 ON_LLM_RESPONSE 时已消费/改写它，
                #       其它插件（如 xml_tag_fixer）还会重写整个响应
                #       ⇒ **标记取不到是常态，不是异常**。
                #
                #   ⚠️ 原来这里打 `响应标记缺失，改用本轮台账剥离` 的 warning，
                #     暗示"台账只是兜底"，与实际相反 ⇒ 用户实测"一直报 warning"，
                #     被这条日志误导（以为有故障）。现在拿不到就**安静地用**。
                from .early_sent import early_sent_segments, strip_early_sent_smart
                segs = early_sent_segments(resp) if resp is not None else []
                ledger = plugin._sent_ledger.get(sid_now, []) if sid_now else []
                # ★★★ 「已经剥过一轮 = 台账已消费」的标记。
                #
                #   为什么要它：框架的多步 agent loop（message_manager.py:793）
                #   **每一步**都调一次 send_xml_messages，而抢发只发生在**第一步**
                #   （后续步的文本是新生成的，从没发过）。
                #   若第二步还拿台账去剥，就是**拿不相关的文本按序号切**
                #   ⇒ 可能把该发的内容切掉 = **丢内容**（比重复更严重）。
                #
                #   所以：台账只在**第一次**调用时消费，之后置为已消费。
                #   而"整批重复"的根因是台账**在第一次调用后就被 pop**
                #   ⇒ 第一步之外的路径拿不到它。两者一起修才对：
                #     · 台账活到轮结束（避免第二步 n=0 ⇒ 整批重复）
                #     · 但只消费一次（避免第二步按序号误切 ⇒ 丢内容）
                # ★★★ 消费标记带**时间戳**，而不是纯布尔。
                #
                #   为什么：这个标记的清除原来只挂在 `final_result` 上，
                #   而事件处理器**可能不执行**（排在后面的处理器会被
                #   前面调用 `event.stop()` 的处理器跳过）。
                #   一旦没清掉，标记就**永久为真** ⇒ 之后每一轮都走
                #   "已消费 ⇒ 不剥离" ⇒ **每轮都重复发送**（用户报"更明显了"）。
                #   ⇒ 用时间戳做**自愈**：只在一轮可能的时间窗内认它，
                #     超时自动失效，不依赖任何钩子一定被执行。
                #      （正常一轮远短于 5 分钟；真的跑那么久，
                #        宁可不剥离（最坏重复一次）也不能永久失效。）
                # ★★★ 用**轮次标识**（event_id）判断"是否已消费"，而不是时间戳。
                #
                #   踩过的坑（用户："还是经常触发重复发送"）：
                #   上一版用 `(time.time() - ts) < 300` 做时效 —— 也就是
                #   **5 分钟内都算"已消费"**。但连续对话里每轮间隔通常远小于 5 分钟
                #   ⇒ 标记一直有效 ⇒ **从第二轮起每轮都走"已消费 ⇒ 不剥离"**
                #   ⇒ 每一轮都重复发送。时间戳根本区分不了"同一轮的第二步"
                #   和"下一轮的第一步"。
                #
                #   ⇒ 正确做法：**按轮次 id 判断**。框架的 `event.event_id`
                #     就是"唯一标识一个事件"的字段（见 message_utils.py 注释）。
                #     同一轮的不同步：event_id 相同 ⇒ 已消费 ⇒ 不剥离；
                #     下一轮：event_id 变了 ⇒ 视为**新轮** ⇒ 正常剥离。
                #     ⇒ 既不依赖钩子一定执行，也不会跨轮误判。
                _ev = getattr(event, "event_id", None) or sid_now
                _cons_ev = plugin._ledger_consumed.get(sid_now) if sid_now else None
                if _cons_ev is not None and _cons_ev == _ev:
                    ledger = []
                    segs = []
                    n = 0
                if ledger:
                    # 台账优先（更权威）
                    if n > 0 and n != len(ledger):
                        # ★ 两边都有但**不一致** —— 这才是真异常，值得留线索
                        logger.warning(
                            "[accel] 已发段数不一致：响应标记 %d 段、台账 %d 段，以台账为准",
                            n, len(ledger),
                        )
                    n = len(ledger)
                    segs = list(ledger)
                    if sid_now:
                        # 记**轮次 id**（不是 True、也不是时间戳）——
                        # 下一轮 event_id 变了就会被视为"新轮"，见上
                        plugin._ledger_consumed[sid_now] = (
                            getattr(event, "event_id", None) or sid_now)
                elif n > 0 and not segs:
                    # 台账没有、标记在但段原文不在（理论上不该发生）
                    # ⇒ 只能按序号剥离，留一条线索
                    logger.warning(
                        "[accel] 有已发标记但拿不到段原文（台账为空），按序号剥离 %d 段", n)

                # ⚠️ 这里曾经有个"同一 sid 被交给发送层两次"的检测 —— **已删除，它误报**。
                #   为什么误报：sid 是**会话** ID，同一会话的**下一轮**不会变，
                #   而记录集从不清空 ⇒ 从第二条消息起每条都命中"重复"。
                #   而框架的多步 agent loop 本来就会在一轮里多次调用本函数（每步一次），
                #   所以它必然狂报。用户实测："每句都报重复发送，但实际没有重复"。
                #   ⇒ 误报的是检测器，发送逻辑是对的。
                #   现在改成**按内容核对**（剥离之后那段）：只有"剥离完仍然整段
                #   包含已抢发内容"才是真的会重复。

                early = plugin._early_results.pop(sid_now, []) if sid_now else []
                if n > 0:
                    # ★ 用"按内容剥离"：count 是按**原始**文本数的，
                    #   而 xml_tag_fixer 这类插件可能已改写 text_response
                    #   （补 <msg> / 拆分消息块 / 合并块 / 转义实体），
                    #   按序号切会少切（重复）或多切（丢内容）。
                    xml_data = strip_early_sent_smart(xml_data, n, segs)

                    # ★★ 事后核对（**唯一有意义的重复判据**）：
                    #   剥离之后，剩余文本里若**仍然整段包含**某个已抢发段的内容，
                    #   那才是真的会把同一段再发一次 —— 这时才报。
                    #   与"同一 sid 出现两次"那种结构判据不同，这个只在真有重叠时触发。
                    try:
                        from .early_sent import _norm_seg
                        rest_norm = _norm_seg(xml_data)
                        for _s in segs:
                            _piece = _norm_seg(_s)
                            if _piece and _piece in rest_norm:
                                logger.warning(
                                    "[accel] 剥离后剩余文本里仍含已抢发的段"
                                    "（这一段可能被重复发送）：%s", _piece[:48])
                                break
                    except Exception:  # noqa: BLE001
                        logger.exception("[accel] 重复发送核对失败（不影响发送）")
                    # 记录一下：本条只发剩余部分（纯观测用）
                    plugin._stats["strip_calls"] = plugin._stats.get("strip_calls", 0) + 1

                    # ★★ 交接处也要守住用户设置的"消息间隔"。
                    #   现在有**两套时钟**：抢发段由我们的 _pace 控制，剩余段由框架
                    #   自己的循环控制 —— 而框架的循环是"每段之后才 sleep"，
                    #   所以**它发的第一段是立即发的**，并不知道我们刚刚才发过一段。
                    #   实测（min=max=0.30）：抢发段之间的间隔是 0.301，
                    #   但交接到框架第一段时变成 **0.001s** —— 两条消息挤在一起，
                    #   用户有意设置的节奏被破坏了。
                    #   所以交出去之前，先把"距上次发送"补足。
                    if early and plugin._has_sendable(xml_data):
                        ctx_now = SendCtx(sid_now, event, tag_set)
                        await plugin._pace(ctx_now)
                        # 框架立刻就会发第一段 ⇒ 现在这个时刻就等于它的发送时刻
                        plugin._mark_sent(ctx_now)
                try:
                    rest = await original(self, event, xml_data, tag_set)
                    # ★ 把抢先发出的结果按【顺序】拼回去：
                    #   框架随后用 _add_message_ids 按位置给 <msg> 贴 ID，
                    #   只还回剩余段会让 ID 全部错位（模型会引用错消息）。
                    #   注意：这里只是"还账"，框架**并没有重发**那些段，
                    #   所以锁里也不会为它们多睡一次（提速收益不受影响）。
                    if early:
                        return (early + list(rest or []))
                    return rest
                finally:
                    # ★ 只清"跟这一次调用绑定"的东西（响应标记 / 响应缓存）。
                    if resp is not None:
                        resp.__dict__.pop("_accel_early_sent_count", None)
                    if sid_now:
                        plugin._resp_by_sid.pop(sid_now, None)
                    # ★★★ **不要在这里清台账** —— 这里踩过一个真 bug：
                    #
                    #   框架的多步 agent loop（message_manager.py:793）是
                    #       async for step in agent_executor.run(...):
                    #           if not await send_llm_text(llm_resp): break
                    #   也就是**每一步**都调一次 send_xml_messages。
                    #   原来台账在这里 pop ⇒ 第二步时台账已空 ⇒ n=0 ⇒ 不剥离
                    #   ⇒ 该步文本被**整段再发一遍** ⇒ 用户截图里"整批消息重复"。
                    #
                    #   台账的清理由 `final_result`（真正的一轮结束）负责 ——
                    #   见 `_clear_round_state`。
                        # ★★ 台账也要**消费完立即清**。
                        #   原来它只等"下一轮开始"才清，于是同一轮内只要
                        #   `send_xml_messages` 被调用第二次（多步 loop / 框架的其它
                        #   调用点），就会出现：
                        #     · 响应已 pop ⇒ n = 0
                        #     · 台账仍在   ⇒ 走兜底 ⇒ **每次都告警**（用户实测"经常性 warning"）
                        #   而且它会拿**本次的（可能完全不相关的）文本**去按内容剥离
                        #   ⇒ **可能把不该切的内容切掉 = 丢内容**（比重复更严重）。
                        #   清掉之后：第二次调用 n=0、不告警、原样交给框架（正确）；
                        #   而"真抢发过但标记丢了"的场景台账仍在，兜底依旧生效。
                        plugin._sent_ledger.pop(sid_now, None)
                plugin._sent_ledger.pop(sid_now + "\x00ev", None)
            # ★★ 声明「有不可撤销的副作用」：这个函数的调用会**真的把消息发出去**。
            #   一旦它抛异常，`patches.guard` 默认会"回落原实现"——那等于**再发一遍**
            #   （用户线上看到的就是同一段回复重复出现）。
            #   声明之后 guard 不再回退，宁可把异常交给上层。
            return mark_side_effects(send_xml_messages)

        h = PatchHandle("early_sent_strip")
        if install(h, MessageProcessor, "send_xml_messages", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 发送层已接管（剥离已抢发段，避免重复发送）")

    # ══════════════════════════════════════════════════════════
    # 侧边栏面板
    # ══════════════════════════════════════════════════════════
    @register.page(
        "/index",
        # ★ 图标改用插件自带的 SVG 文件（框架支持：PageMenu.icon 给相对路径时，
        #   会通过 /api/plugins/<id>/menu-icon/<route> 提供）。这样侧边栏菜单里
        #   显示的是与插件图标同一套视觉，而不是通用图标字体里的某个图标。
        menu=PageMenu(
            label={"zh": "提速器", "en": "Accelerator"},
            icon="icon.svg",
            order=95,
        ),
    )
    def page(self):
        return PluginPage.from_folder("./web")

    # ══════════════════════════════════════════════════════════
    # 壁纸服务（面板挂载在 /page/... 下，无法直接访问插件目录 ⇒ 必须走 API）
    # ══════════════════════════════════════════════════════════
    @register.api(method="GET", path="/wallpapers", auth=True)
    async def api_wallpapers(self):
        """列出可用壁纸（面板据此轮换）。"""
        from . import wallpapers_api as wp
        return {"files": wp.discover()}

    @register.api(method="GET", path="/wallpapers/{name}", auth=True)
    async def api_wallpaper_file(self, name: str):
        """返回一张壁纸文件。★ 安全：只认 discover() 过的文件名，不做路径拼接。"""
        from fastapi.responses import Response
        from . import wallpapers_api as wp

        path = wp.resolve(name)
        if path is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="wallpaper not found")
        data = path.read_bytes()
        return Response(
            content=data,
            media_type=wp.mime_for(name),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # ── 用户上传壁纸（面板里的「+」方框）──
    @register.api(method="POST", path="/wallpapers/upload", auth=True)
    async def api_wallpaper_upload(self, payload: dict | None = None):
        """接收 base64 图片并写入壁纸目录。

        payload: {"name": "原始文件名（仅用于取扩展名）", "data": "data:image/...;base64,xxx"}
        返回:    {"ok": true, "name": "u_xxx.webp"} / {"ok": false, "error": "..."}
        """
        import base64
        from . import wallpapers_api as wp

        p = payload or {}
        raw = p.get("data") or ""
        if not isinstance(raw, str) or len(raw) < 32:
            return {"ok": False, "error": "没有收到图片数据"}
        # dataURL → bytes
        try:
            b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
            data = base64.b64decode(b64, validate=False)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "图片数据无法解析"}
        name = wp.save_upload(str(p.get("name") or ""), data)
        if not name:
            return {"ok": False,
                    "error": f"不是可识别的图片，或超过 {wp.MAX_UPLOAD_BYTES // 1024 // 1024}MB"}
        logger.info("[accel] 已保存用户上传的壁纸：%s（%d KB）", name, len(data) // 1024)
        return {"ok": True, "name": name, "files": wp.discover()}

    @register.api(method="POST", path="/wallpapers/delete", auth=True)
    async def api_wallpaper_delete(self, payload: dict | None = None):
        """删除一张**用户上传**的壁纸（内置图不允许删）。"""
        from . import wallpapers_api as wp
        name = str((payload or {}).get("name") or "")
        if not wp.remove(name):
            return {"ok": False, "error": "只能删除自己上传的背景"}
        return {"ok": True, "files": wp.discover()}

    @register.api(method="POST", path="/breaker/reset", auth=True)
    async def api_breaker_reset(self, payload: dict | None = None):
        """手动重置熔断的接管点（面板按钮调用）。

        payload: {"name": "stream_engine"} 或 {"all": true}
        """
        from .patches import breaker_for
        names = [h["name"] for h in self.patches.health()]
        if payload and payload.get("all"):
            for n in names:
                breaker_for(n).reset()
            return {"reset": names}
        target = (payload or {}).get("name", "")
        if target not in names:
            return {"error": f"unknown patch: {target}"}
        breaker_for(target).reset()
        return {"reset": [target]}

    # ══════════════════════════════════════════════════════════
    # API / 工具
    # ══════════════════════════════════════════════════════════
    @register.api(method="GET", path="/providers", auth=True)
    async def api_providers(self):
        """列出**已配置的提供商与模型**，供面板的多选下拉使用。

        ★ 为什么要这个 API：框架的 `source:'model'` 只对**框架自己的配置页**生效；
          插件自带的侧边栏面板是独立前端，拿不到那份列表，只能自己问后端。

        ★★ 必须用 `self.ctx.provider_mgr` —— **不要自己 new ProviderManager**。
          框架里它是 `ProviderManager(db, kira_config)` 需要两个位置参数，
          自己 new 会直接 TypeError（v1.0.2 就是这么炸的，线上日志刷了一屏）。
          上下文里框架已经把**同一个单例**注入好了，直接用即可。

        返回扁平列表，每项同时给出"模型级 / 提供商级 / 名称"三种取值 ——
        用户点哪个都能匹配（后端三种都认）。
        """
        out = []
        try:
            mgr = getattr(self.ctx, "provider_mgr", None)
            if mgr is None:
                # 兜底：有些框架版本把管理器挂在别处。**仍然不要自己 new** ——
                # 宁可返回空列表让面板退化为可手填，也不要抛异常刷日志。
                mgr = getattr(self.ctx, "provider_manager", None)
            if mgr is None:
                logger.warning("[accel] 上下文里拿不到 provider_mgr ⇒ 面板退化为可手填")
                return {"providers": []}

            # 用框架已有的枚举方法（同步，返回 {provider_id: BaseProvider}）
            allp = {}
            for attr in ("get_all_providers",):
                fn = getattr(mgr, attr, None)
                if callable(fn):
                    try:
                        allp = fn() or {}
                    except Exception:  # noqa: BLE001
                        logger.exception("[accel] %s() 调用失败", attr)
                    break

            for pid, p in (allp or {}).items():
                pid = str(pid or "")
                # 名字优先查配置（get_provider_info 会给出友好的 provider_name）
                pname = pid
                try:
                    info = mgr.get_provider_info(pid)
                    if info is not None:
                        pname = getattr(info, "provider_name", None) or pid
                except Exception:  # noqa: BLE001
                    pass

                # 模型列表：优先 get_models()（框架的权威来源），退回对象属性
                models = None
                try:
                    gm = getattr(mgr, "get_models", None)
                    if callable(gm):
                        models = gm(pid)
                except Exception:  # noqa: BLE001
                    models = None
                if not isinstance(models, dict):
                    models = getattr(p, "models", None) or {}
                llm = models.get("llm") if isinstance(models, dict) else None

                if isinstance(llm, dict) and llm:
                    for mid in llm.keys():
                        out.append({"value": f"{pid}:{mid}",
                                    "label": f"{mid} ({pname})",
                                    "provider_id": pid, "provider_name": pname,
                                    "model_id": mid})
                else:
                    # 没有登记模型也要给出提供商本身（后端认纯 providerId）
                    out.append({"value": pid, "label": pname,
                                "provider_id": pid, "provider_name": pname,
                                "model_id": ""})
        except Exception:  # noqa: BLE001
            # 这里失败**不能**影响插件其余功能：面板退化为可手填即可。
            logger.exception("[accel] 列出提供商失败（面板退化为可手填）")
        return {"providers": out}

    @register.api(method="GET", path="/health", auth=True)
    async def api_health(self):
        sid = self._current_sid
        d = self.thinking.last_decision(sid) if sid else None
        return {
            "stats": self._stats,
            "patches": self.patches.health(),
            "thinking_now": ({
                "enabled": d.enabled, "score": d.score,
                "signals": d.signals, "effort": d.effort,
                # 供面板显示"相对基线在怎么判断"
                "context_baseline": int(self.thinking.context_baseline(sid)) if sid else 0,
                "ratio_on": self.thinking.context_ratio_on,
                "ratio_off": self.thinking.context_ratio_off,
            } if d else None),
        }

    @register.tool(
        "accel_report",
        "报告 KiraAI 提速器状态：已生效的优化项、抢先发送与自动思考统计（用于排查 为什么这轮很慢）",
        # ★ 刻意**不写 `required`**（而不是写 `required: []`）：
        #   空数组是部分网关（Gemini 的函数声明校验尤其严）拒收的模式，
        #   而"没有必填参数"用**省略**表达与"空数组"完全等价。
        #   框架自带工具与市场插件里都没有"无参数工具"的先例，
        #   所以这里取最保守的写法。
        {"type": "object", "properties": {}},
    )
    async def accel_report(self, event, **_) -> str:
        s = self._stats
        lines = [
            f"轮数={s['turns']} 步数={s['steps']}",
            f"抢先发送={s['early_sent']} 段；工具失败信号={s['tool_signals']} 次",
            f"自动思考：开={s['thinking_on']} 关={s['thinking_off']} 超预算拦截={s['thinking_skipped_budget']}",
        ]
        for h in self.patches.health():
            br = breaker_for(h["name"])
            flag = "已熔断⚠" if h["tripped"] else ("生效" if h["active"] else "未启用")
            lines.append(f"接管点 {h['name']}: {flag}（失败 {h['failures']} 次）")
        return "\n".join(lines)
