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
import contextvars
import json
import random
import re
import time
from collections import deque
from typing import Any, Optional

from core.plugin import BasePlugin, logger, on, Priority, register, PluginPage, PageMenu
from core.provider import LLMRequest, LLMResponse
from core.chat.message_utils import KiraMessageBatchEvent

from .core_compat import event_system, openai_compatible_client
from .early_sent import chain_signature as _chain_signature
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
from .config_migrations import (
    MIGRATION_DELAY_S,
    marker_applied as migration_marker_applied,
    run_nothink_migration,
)

PLUGIN_ID = "kira_accelerator"

#: 当前任务正在处理的 LLMResponse。ON_LLM_RESPONSE 与随后的 parse_xml 在同一
#: 任务链里，ContextVar 既能精确定位本轮响应，又不会像实例变量那样串会话。
_ACTIVE_RESP: contextvars.ContextVar = contextvars.ContextVar(
    "kira_accelerator_active_response", default=None)

#: "上次发送时刻"这张表最多记多少个会话（超出整体清空，防长期运行内存累积）
MAX_PACING_SIDS = 512


class _SentIdPlaceholder:
    """最后兜底：只带 `message_id` 的最小替身（拿不到框架原生结果类时才用）。"""

    __slots__ = ('message_id',)

    def __init__(self, message_id=''):
        self.message_id = message_id


def _make_id_placeholder(message_results):
    """给「被去重删掉、从未发送」的 <msg> 造一个**与框架同类型**的空结果。

    优先级：
      1. 直接复用本批真实结果的类（`type(message_results[0])`）—— 类型、字段
         与框架原生完全一致，未来框架若读取 `.ok` / `.is_notice` 也不会出事；
      2. 退而导入 `core.chat.message_utils.KiraIMSentResult`（两代都有）；
      3. 都失败才用最小替身（只保证 `.message_id` 可读，当前框架只读这个字段）。
    """
    if message_results:
        try:
            return type(message_results[0])(message_id="")
        except Exception:  # noqa: BLE001
            pass
    try:
        from core.chat.message_utils import KiraIMSentResult  # noqa: WPS433

        return KiraIMSentResult(message_id="")
    except Exception:  # noqa: BLE001
        return _SentIdPlaceholder("")


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
        #: 内容级去重：与已发段同内容、仅多 reply/at 的残留块整块丢弃（可关）
        self.dedupe_blocks = bool(c_str.get("dedupe_blocks", True))
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
        # 判定为"不需要思考"时是否**主动发关闭参数**（默认开：v1.0.78 起）。
        # 旧版默认关，后果是"判了关、模型却还在思考"（2026-10-05 用户实测）——
        # 升级时会自动迁移一次旧配置（见 config_migrations.py），之后以用户设置为准。
        self.thinking_inject_nothink = bool(c_thk.get("inject_nothinking", True))
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
        #: 抢发自己的投递在飞标记（3.0 按链对账时跳过拦截）。
        #: **必须按会话分键**：实例级计数会让 A 群抢发时 B 群的链级对账也被跳过，
        #: 叠加同文响应缓存时可稳定复现重复发送。
        self._early_inflight_sids: dict[str, int] = {}
        # ★ 旁证台账：sid → 本轮已抢发的**段原文**列表
        #   （响应标记丢失时的兜底；存原文而不是只存个数，
        #     这样兜底时也能按内容剥离，对文本被改写同样稳健）
        self._sent_ledger: dict[str, list] = {}
        # ★★★ v1.0.85：按**链**记账的抢发签名（元素侧生成，与对账**同一函数**）。
        #   为什么不再只靠"段原文"：段是**解析前**的文本，链是**解析后**的元素，
        #   解析期任何改写（`<file>` 的路径解析、`Image.md5` 惰性算出、生成类结果）
        #   都会让两侧对不上 ⇒ 台账出不了账 ⇒ 框架再发一次（线上：表情包/文件/
        #   出图/语音被发两次）。这里记抢发时那条链自己的
        #   `(可见文字, 身份, 元素类名)` ⇒ 两侧同源，差异从根上消失。
        #   与 `_early_results` 一一对应（同一次投递写一条）。
        self._chain_records: dict[str, list] = {}
        # ★★★ v1.0.86：3.0 的「解析前剥离」——把接缝补回 2.x 的位置。
        #
        #   2.x：acc 包住 `MessageProcessor.send_xml_messages`，在**入口**把已抢发段
        #        从 `xml_data` 里刨掉；而那个函数**自己解析**
        #        （`actions = await self._parse_xml_msg(xml_data, tag_set)`）
        #        ⇒ 框架压根看不到那些段 ⇒ 不会重复解析、更不会重复**生成**。
        #   3.0：`send_xml_messages` 已删。解析搬到了 workflow
        #        （`core/workflow/src/im/stages/execute.py:63` 调 `parse_xml`），
        #        发送在第 77 行走 `send_message_chain`（acc 原来只包了这里）
        #        ⇒ **解析（含出图/TTS/视频的真执行）发生在 acc 拿到控制权之前**
        #        ⇒ 生成类段会被生成两次（实测：`<img>`/`<record>`/`<video>` 各 2 次）。
        #
        #   这里把接缝补到解析前：包 `parse_xml` 做同样的剥离；
        #   再包 `add_message_ids` 把抢发结果按顺序拼回（= 2.x 的 `early + rest`），
        #   否则"少解析一段 ⇒ 少一个结果"会让模型历史里的 message_id 错位
        #   （`add_message_ids` 是**按 `<msg>` 位置**贴 ID 的）。
        #   两层都留了兜底：链级对账（v1.0.85）仍在，剥离失败只告警、不改变发送。
        #: 本轮响应文本 -> (抢发结果列表, 被去重删掉的 <msg> 位置)
        self._strip_ctx: dict[str, tuple] = {}
        self._accel_parse_inflight: int = 0       # 我们自己调 parse_xml 时的标记
        # （`_ledger_consumed` 已删除：它编码的是"已消费 ⇒ 跳过剥离"这个
        #   错误设计，而它正是"还是经常重复发送"的根因 ——
        #   每一次 send_xml_messages 对应多步 loop 的一步，台账按步消费即可，
        #   不需要、也不能有"整轮只消费一次"的概念。）
        #: 台账最后一次活动时间（用于清理陈旧状态，双保险）
        self._ledger_ts: dict[str, float] = {}
        self._proxy_cache: dict[tuple, LLMClientProxy] = {}
        # 一次性配置迁移（v1.0.78）：定时器/任务引用（防被 GC，terminate 可取消）
        self._migration_timer = None
        self._migration_task = None
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

    # ══════════════════════════════════════════════════════════
    # 一次性配置迁移（v1.0.78）—— 见 config_migrations.py 的完整说明
    # ══════════════════════════════════════════════════════════
    def _migration_data_dir(self):
        """迁移标记文件所在目录（插件数据目录）。取不到返回 None。"""
        try:
            return self.ctx.get_plugin_data_dir()
        except Exception:  # noqa: BLE001
            return None

    def _schedule_nothink_migration(self) -> None:
        """把「inject_nothinking 一次性迁移为开」排到插件初始化完成之后执行。

        为什么必须延迟：迁移通过框架的 update_plugin_config 写盘，而它**会热重载
        本插件** —— 不能在 initialize() 的调用栈里立刻执行（那会让框架在"正在
        初始化我们"的过程中把我们卸载重建）。延迟到初始化完成后，只执行一次。
        """
        try:
            if getattr(self.ctx, "plugin_mgr", None) is None:
                return                      # 测试 / 无管理器：不迁移
            data_dir = self._migration_data_dir()
            if data_dir is None or migration_marker_applied(data_dir):
                return                      # 已经迁移过（或无处存标记）
            loop = asyncio.get_running_loop()
            self._migration_timer = loop.call_later(
                MIGRATION_DELAY_S, self._spawn_nothink_migration)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 配置迁移调度失败（忽略，下次加载重试）")

    def _spawn_nothink_migration(self) -> None:
        """定时器回调：创建迁移任务（引用存实例上，防被 GC）。"""
        try:
            self._migration_task = asyncio.get_running_loop().create_task(
                self._run_nothink_migration())
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 配置迁移任务启动失败（忽略）")

    async def _run_nothink_migration(self) -> None:
        try:
            await run_nothink_migration(self, PLUGIN_ID,
                                        self._migration_data_dir(), log=logger)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 配置迁移失败（忽略，下次加载重试）")

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
        # ★ v1.0.78 一次性配置迁移：旧版的 inject_nothinking=false 改成 true。
        #   与 thinking_enabled 无关（这是"配置默认值"的迁移）；仅在需要且安全时
        #   才动（见 config_migrations.py），失败安静跳过、下次重试。
        self._schedule_nothink_migration()

    async def terminate(self) -> None:
        # ★ 取消尚未触发的一次性迁移定时器（已触发的任务由自身守卫兜底：
        #   被替换/停用时不会再动配置）
        try:
            _t = getattr(self, "_migration_timer", None)
            if _t is not None:
                _t.cancel()
        except Exception:  # noqa: BLE001
            pass
        # ★ 卸载前把统计落盘（热重载会走这里，下次 initialize 再读回来）
        try:
            self._stats_saved_at = 0.0      # 绕过节流，保证这次一定写
            self._save_stats()
        except Exception:  # noqa: BLE001
            pass
        self.patches.uninstall_all()
        self._proxy_cache.clear()
        self._early_inflight_sids.clear()
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

    def _clear_round_state(self, sid, event_id=None):
        """一轮真正结束时清理"本轮状态"（台账 / 响应缓存 / 抢发结果）。

        ⚠️ 这里**不是**剥离能否生效的前提 —— 台账在每次 `send_xml_messages`
        返回时就已经按步消费掉了（见 `_install_early_sent_strip` 的 finally）。
        本函数是**兜底**：一轮里若"抢发了却没走到发送"（事件被 stop），
        残留会留到下一轮，靠这里 + 轮次键一起清掉。

        ★★★ 2026-09-28（审计 P1 —— **确认会造成真实重复发送**）：
          这里原来是"按 sid 前缀清掉该会话**所有轮次**"。而框架会**重叠处理同一会话**
          （作者自己在 `_ckey` 注释里引用的线上日志就是：第 2 轮的 llm_request 先到、
          第 1 轮 4 秒后才结束），于是：
              第 1 轮抢发 A ⇒ 台账[(sid,turn-1)] = [A]
              第 2 轮（短回复）先跑完 ⇒ observe_final ⇒ 把**第 1 轮**的台账一起清掉
              第 1 轮这才走到 send_xml_messages ⇒ n=0 ⇒ 不剥离 ⇒ **框架把 A 再发一次** ✗
          ⇒ 现在**只清本轮**（`(sid, 本轮轮次)` 复合键），与 `_reset_turn_ledger` 对齐。
          "残留到下一轮"的风险由两处覆盖，不需要"按会话全清"这个更粗的动作：
            · 每轮开始前 `_reset_turn_ledger`（只清本轮）
            · `_clear_stale_round_state`（>600s 未活动的键）
          event_id 缺省（None）时退化为**只清无轮次的旧键**，绝不波及带轮次的其它轮。
        守卫：tests/test_cross_turn_clear_safety.py
        """
        try:
            if not sid:
                return
            if event_id:
                # ★ 只清"本轮"这一份（复合键）
                _k = self._ckey(sid, event_id)
                for _d in (self._sent_ledger, self._resp_by_sid, self._early_results,
                        self._chain_records):
                    _d.pop(_k, None)
                return
            # 拿不到轮次 id（理论上不该发生）⇒ **保守**：只清"不带轮次"的旧键。
            # 带轮次的其它键一律不动 —— 宁可让 `_clear_stale_round_state`
            # 稍后按 600s 窗口回收，也绝不误清仍在跑的另一轮（那会造成重复发送）。
            for _d in (self._sent_ledger, self._resp_by_sid, self._early_results,
                        self._chain_records):
                _d.pop(sid, None)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 清理本轮发送状态失败（不影响发送）")

    @on.final_result(priority=Priority.LOW)
    async def observe_final(self, event: KiraMessageBatchEvent, final_result, *_):
        # ★ 无论观测是否开启，**都要**清理本轮发送状态（否则台账会残留到下一轮，
        #   下一轮若文本恰好相同就会被误剥离 = 丢内容）。
        # ★★★ 2026-09-28：必须把 event_id 一起传（只清本轮）。
        #   按 sid 前缀全清会把**仍在跑的另一轮**的台账清掉 ⇒ 那一轮剥离失败
        #   ⇒ 框架重发 ⇒ 用户看到重复（审计 P1 实测复现）。
        self._clear_round_state(getattr(event, "sid", None),
                                getattr(event, "event_id", None))
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
            # ★ v1.0.78：把「本轮实际有没有把思考参数发出去」也打出来。
            #   以前只打判定结果，判定与实际脱节时（例如旧默认下判了"关"却不发
            #   关闭参数）日志会说"思考=关"而模型仍在思考 —— 现在一眼可见。
            applied = getattr(d, "applied", "") or ""
            if applied:
                think_txt += "→%s" % applied
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
          一旦没执行，台账就会**残留到下一轮**
          ⇒ 下一轮要么不剥离（**重复**）、要么误剥离（**丢内容**）。
        ⇒ 这里在**每轮请求前**兜一次底：凡是超过 10 分钟没动过的状态一律清掉。
          正常一轮远短于 10 分钟，所以不会误清正在用的状态。
        """
        try:
            now = time.time()
            # ⚠️ 必须遍历**各字典的键并集**，不能只看 _ledger_ts ——
            #    否则某些键（比如只出现在 _resp_by_sid 里的）永远清不掉 ⇒
            #    多群聊场景下这几个字典会**随会话数缓慢增长**（内存泄漏）。
            keys = set()
            for d in (self._ledger_ts, self._sent_ledger, self._resp_by_sid, self._early_results,
                            self._chain_records):
                if isinstance(d, dict):
                    keys |= set(d.keys())
            for k in keys:
                # ★ 现在是复合键 (sid\x00轮次)，**每个键都有自己的时间戳**
                #   （`_emit_segment` / `_remember` 写的）⇒ 直接查即可。
                ts = float(self._ledger_ts.get(k, 0) or 0) if isinstance(self._ledger_ts, dict) else 0
                # ★★★ 2026-09-28（慢轮次加固）：窗口从 600s 放宽到 **1800s**。
                #   600s（10 分钟）对"工具多、模型慢"的轮次偏紧：一轮里连着几个
                #   大工具 + 本地模型推理，超过 10 分钟完全可能；那时下一轮的清理
                #   会把**仍在跑的那一轮**台账清掉 ⇒ 它走到发送层 n=0 ⇒ 框架重发。
                #   放宽的代价只是"残留多留 20 分钟"，而残留本身有上限、且
                #   正确性由轮次键保证（不会误用）⇒ 划算。
                # 没有时间戳的（陈旧遗留）按「更早」处理 ⇒ 一并清掉
                if ts and (now - ts) <= 1800:
                    continue
                for _d in (self._ledger_ts, self._sent_ledger, self._resp_by_sid, self._early_results,
                            self._chain_records):
                    _d.pop(k, None)
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
        # ★ 新一轮开始 ⇒ 只清**本轮**的抢发台账（按 (sid,轮次) 复合键；
        #   绝不能清整个会话 —— 框架可能还在跑同一个会话的上一轮，见 _ckey 说明）
        self._reset_turn_ledger(sid_now, getattr(event, "event_id", None))

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
        # ★ 跨世代：3.0 把该类搬到了 core.provider.openai_compatible
        #   （原来硬编码 `from core.utils import model_clients` ⇒ 3.0 上
        #    ImportError ⇒ **HTTP 客户端复用整个失效**，实测。）
        client_cls = openai_compatible_client()
        if client_cls is None:
            logger.warning("[accel] 未找到 OpenAI 兼容客户端，跳过 HTTP 客户端复用")
            return

        cache = self._client_cache

        def factory(original):
            def make(self, *args, **kwargs):
                # ★ v1.0.90：签名原样透传（框架给 _build_client 加参数也不炸）
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
                    c = original(self, *args, **kwargs)
                    if len(cache) > 32:          # 防着配置反复变更把缓存撑大
                        cache.clear()
                    cache[key] = c
                return c
            return make

        h = PatchHandle("reuse_http_client")
        if install(h, client_cls, "_build_client", factory) is not None:
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
            def get_model_client(self, *args, **kwargs):
                # ★ v1.0.90：签名原样透传（框架加参数/换顺序都不会把我们打死）
                client = original(self, *args, **kwargs)
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
            # ★★★ 必须 `return` 投递结果（2026-09-28，审计 P0 —— 一处静默的断链）：
            #   `StreamEngine` 是**按返回值**判断"这一段到底发出去没有"的
            #   （stream_engine.py: `ok = await self.emit(seg)`，`False/None` = 没投递），
            #   而 `_emit_segment` 的契约本来就是 `-> bool`。
            #   少了这个 return ⇒ 返回值恒为 None ⇒ 引擎一律判"没投递" ⇒
            #     · 抢先发送退化成"只有第一段"（整轮提速几乎归零）
            #     · 响应私有标记（_accel_early_sent_count / _segments）恒为空
            #       ⇒ 剥离只能走台账兜底；工具轮防护（写 _accel_tool_turn_early）也不再触发
            #   而这一切**完全静默**（None 既不是异常也不是 False）。
            #   守卫：tests/test_production_emit_contract.py（走真 _make_engine）
            async def _emit(seg: str) -> bool:
                return await self._emit_segment(seg, ctx)
            emit = _emit

        def _remember(resp):
            # 记录本轮响应，供发送层（send_xml_messages）读取标记做剥离。
            # ★ 按 sid 分开存：并发会话下用一个实例变量会拿错响应
            #   ⇒ 要么重复发送、要么把内容丢掉。
            if ctx is not None and ctx.sid:
                # ★ 按 (sid, 轮次) 存：并发/重叠会话下按 sid 存会拿错响应
                #   ⇒ 要么重复发送、要么把内容丢掉（见 _ckey 的说明）。
                _k = self._ckey(ctx.sid, getattr(ctx.event, "event_id", None))
                self._resp_by_sid[_k] = resp
                # ★★★ 2026-09-28（慢轮次加固）：**同时写活动时间戳**。
                #   以前只有 `_emit_segment`（真抢发过）才写 `_ledger_ts`；
                #   而"响应已记录、但还没走到发送层"的键**没有时间戳** ⇒
                #   下一轮的陈旧清理把它按"更早"处理、直接清掉 ⇒
                #   那一轮走到发送层时拿不到响应与台账 ⇒ 剥离失败 ⇒ 框架重发。
                #   写时间戳后，这类键会被正确地按活动时间保护。
                try:
                    if not isinstance(getattr(self, "_ledger_ts", None), dict):
                        self._ledger_ts = {}
                    self._ledger_ts[_k] = time.time()
                except Exception:  # noqa: BLE001
                    pass
                if len(self._resp_by_sid) > 32:          # 防泄漏
                    self._resp_by_sid.clear()
                    self._resp_by_sid[_k] = resp
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
            event_handler_reg, EventType = event_system()
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

    @staticmethod
    def _ckey(sid, event_id):
        """(会话, 轮次) **复合键** —— 抢发台账 / 响应缓存 / 活动时间都用它。

        ★★★ 为什么必须带上轮次（2026-09-27，线上重复发送的**真根因**）：
          框架会**重叠处理同一个会话**。用户日志实证：
             15:35:33 [llm] Running agent using ...   ← 第 2 轮的 llm_request
             15:35:37 [accel] 一轮结束 sid=... steps=1 ← 第 1 轮这时才结束
          即第 1 轮"已经抢发、但还没走到 send_xml_messages"时，第 2 轮就开始了。
          而原来这些状态**只按 sid 存** ⇒ 第 2 轮一开始就把第 1 轮的
          台账 + 响应**一起清掉/覆盖** ⇒ 第 1 轮走到发送层时
          n=0、也拿不到自己的响应 ⇒ **剥离失败** ⇒
          框架把它**抢发过的内容再发一次** = 用户看到的"重复发送"。
        ⇒ 带上轮次 id 后，两轮各用各的键，互不干扰；也不再需要
          "轮次变了就重新开账"那套（键本身就是轮次）。
        """
        return "%s\x00%s" % (sid, event_id if event_id else "-")

    #: `发送入口是否接受某关键字` 的缓存（键=函数对象）
    _kwarg_probe_cache: dict = {}

    @classmethod
    def _accepts_kwarg(cls, fn, name: str) -> bool:
        """`fn` 是否接受某个关键字（含 `**kwargs`）。**只探测，不重试**。

        为什么要探测而不是"试着传一下，报错再退"：发送是**有副作用的**，
        try/except 重试等于把消息发两遍（历史教训）。这里用 inspect 静态判断，并缓存。
        """
        try:
            key = (fn, name)
            if key in cls._kwarg_probe_cache:
                return cls._kwarg_probe_cache[key]
        except Exception:  # noqa: BLE001
            key = None
        ok = False
        try:
            import inspect
            params = inspect.signature(fn).parameters
            ok = name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
        except Exception:  # noqa: BLE001
            ok = False
        try:
            if key is not None:
                cls._kwarg_probe_cache[key] = ok
        except Exception:  # noqa: BLE001
            pass
        return ok

    @staticmethod
    def _event_bot(ctx):
        """本轮事件上的 bot 身份（rc2 起框架用它记录出站历史）；拿不到返回 None。"""
        try:
            return getattr(getattr(ctx, "event", None), "bot", None)
        except Exception:  # noqa: BLE001
            return None

    async def _parse_seg_actions(self, seg, tag_set, mp):
        """把一段 XML 解析成动作列表（双世代）。

        3.0 起 `MessageProcessor._parse_xml_msg` 已删，解析迁到
        `MessageDeliveryService.parse_xml`（静态异步方法，ctx.message_delivery 可达）；
        2.x 用 `mp._parse_xml_msg`。逐级探测，不绑版本号。
        """
        delivery = getattr(self.ctx, "message_delivery", None)
        parse = getattr(delivery, "parse_xml", None) if delivery is not None else None
        if parse is not None:
            # ★ v1.0.86：标记"这是我们自己为了抢发而解析"，让包装过的 parse_xml
            #   不要对我们自己的段做"解析前剥离"（否则会剥掉刚要发的内容）。
            self._accel_parse_inflight = getattr(self, "_accel_parse_inflight", 0) + 1
            try:
                return await parse(seg, tag_set)
            finally:
                self._accel_parse_inflight = max(0, getattr(self, "_accel_parse_inflight", 1) - 1)
        return await mp._parse_xml_msg(seg, tag_set)

    @on.llm_response(priority=Priority.LOW)
    async def _remember_active_response(self, event, resp, *_):
        """把「本任务当前响应」放进 ContextVar，供随后的 parse_xml 精确定位。

        只按文本反查在两种生产情形下会拿错轮次：
          · 重叠轮次/上一轮残留里存在相同文本；
          · 字典中旧条目先于当前条目，旧条目台账为空 ⇒ 当前轮不剥离。
        ContextVar 是 task-local，不会像实例变量那样串会话；只作**首选**，
        找不到时仍回退到文本择优。
        """
        try:
            _ACTIVE_RESP.set((id(self), resp))
        except Exception:  # noqa: BLE001
            pass

    def _active_response(self):
        try:
            value = _ACTIVE_RESP.get()
            if isinstance(value, tuple) and len(value) == 2 and value[0] == id(self):
                return value[1]
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _response_segments(resp) -> list:
        try:
            segs = getattr(resp, "_accel_early_sent_segments", None)
            return list(segs) if isinstance(segs, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _response_for_text(self, text: str, key: str = None):
        """按「当前任务响应 → 指定键 → 最新同文响应」的顺序定位本轮状态。

        `parse_xml` 是静态方法，签名不带 sid/event_id；因此：
          1. ContextVar 里的当前响应最精确；
          2. 2.x 还能拿到 event 对应的 key，先校验；
          3. 最后才按文本反查，并**优先最新时间戳**，避免旧同文条目抢先命中。
        """
        want = (text or "").strip()
        if not want:
            return None, None
        active = self._active_response()
        if active is not None and (getattr(active, "text_response", "") or "").strip() == want:
            for k, resp in list(getattr(self, "_resp_by_sid", {}).items()):
                if resp is active:
                    return k, active
        if key:
            resp = getattr(self, "_resp_by_sid", {}).get(key)
            # 精确轮次键最可信（与旧行为一致）；兼容只带计数标记的响应桩。
            if resp is not None:
                return key, resp

        matches = []
        for order, (k, resp) in enumerate(list(getattr(self, "_resp_by_sid", {}).items())):
            if (getattr(resp, "text_response", "") or "").strip() != want:
                continue
            try:
                ts = float((getattr(self, "_ledger_ts", {}) or {}).get(k, 0) or 0)
            except Exception:  # noqa: BLE001
                ts = 0.0
            matches.append((ts, order, k, resp))
        if matches:
            matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return matches[0][2], matches[0][3]
        return key, None

    def _turn_for_text(self, text):
        """把"框架那遍手里的响应文本"对回本轮状态 `(key, 已抢发段, 抢发结果)`。

        为什么必须靠文本反查：3.0 的 `parse_xml(xml_data, tag_set)` 与
        `add_message_ids(xml_data, results)` 都是**静态方法**，签名里**不带 sid/轮次**，
        而我们要剥的恰恰是"**本轮**抢发了哪些段"。
        现在首选 task-local 当前响应；缺失时才按文本反查，并选最新轮次，
        不再让旧的同文缓存抢占本轮。
        """
        k, resp = self._response_for_text(text)
        if resp is None:
            return None, [], []
        return (k, list(self._sent_ledger.get(k) or []) or self._response_segments(resp),
                list(self._early_results.get(k) or []))

    def _parse_strip_factory(self, original):
        """包 `MessageDeliveryService.parse_xml`：解析**之前**刨掉已抢发段。

        为什么必须在这一层（v1.0.86 的实测依据）：3.0 的解析在
        `execute.py:63`（`delivery.parse_xml(text, tag_set)`），而 acc 原来的接管点
        `send_message_chain` 在第 77 行 —— 等它拿到控制权，标签处理器（出图/TTS/视频）
        已经**又执行了一遍**。包住 `parse_xml` 后框架只解析"还没发过的部分"，
        与 2.x 在自己的 `send_xml_messages` 入口剥离完全等价。

        安全边界：
          · 只对"**在本轮响应文本里**"的调用生效（`_turn_for_text` 精确匹配）——
            别的插件自己解析 XML 不会被误伤；
          · acc 自己为了抢发而解析时打了 `_accel_parse_inflight` 标记 ⇒ 直接放行；
          · 剥离函数复用 2.x 的 `strip_early_sent_smart`（按内容剥离，抗改写）；
          · 任何异常都只告警并按**原文本**解析（宁可多生成一次，也不改变发送语义）。
        """
        async def parse_xml(xml_data=None, tag_set=None, *args, **kwargs):
            if getattr(self, "_accel_parse_inflight", 0):
                return await original(xml_data, tag_set, *args, **kwargs)
            try:
                key, segs, early = self._turn_for_text(xml_data)
                if key and segs:
                    from .early_sent import strip_early_sent_smart
                    stripped = strip_early_sent_smart(xml_data, len(segs), segs)
                    # 先统计剥离实际剥掉多少（必须在去重之前，否则去重删掉残留
                    # 块后会被误算成"没剥掉"，打出假的"会被框架再发一次"警告）
                    try:
                        from .early_sent import count_segments_removed
                        _removed = count_segments_removed(stripped, segs, xml_data)
                    except Exception:  # noqa: BLE001
                        _removed = len(segs)
                    # ★ v1.0.92：内容级去重 —— 与剥离是否改变文本无关都要跑
                    _dropped_n = 0
                    _drop_pos = []
                    try:
                        from .early_sent import drop_duplicate_blocks
                        _deduped, _dropped = (
                            drop_duplicate_blocks(stripped, segs)
                            if getattr(self, "dedupe_blocks", True) else (stripped, []))
                        if _dropped:
                            try:
                                from .early_sent import dropped_positions
                                _positions = dropped_positions(xml_data, _dropped)
                            except Exception:  # noqa: BLE001
                                _positions = []
                            # 与 2.x 同一安全约定：映射不全就不去重，避免后续消息 ID 错位。
                            if len(_positions) == len(_dropped):
                                stripped = _deduped
                                _drop_pos = _positions
                                _dropped_n = len(_dropped)
                            else:
                                logger.warning(
                                    "[accel] 解析前内容去重位置映射不完整（%d/%d），为保消息 ID 对位本次不去重",
                                    len(_positions), len(_dropped))
                    except Exception:  # noqa: BLE001
                        pass
                    if stripped != xml_data:
                        self._strip_ctx[xml_data] = (list(early), _drop_pos)
                        if len(self._strip_ctx) > 32:      # 防泄漏
                            self._strip_ctx.pop(next(iter(self._strip_ctx)), None)
                        self._stats["preparse_strip"] = self._stats.get("preparse_strip", 0) + 1
                        if _dropped_n:
                            logger.info(
                                "[accel] 解析前剥离 %d/%d 段 + 内容去重 %d 块"
                                "（不会重复发送）", _removed, len(segs), _dropped_n)
                        elif _removed < len(segs):
                            logger.warning(
                                "[accel] 解析前剥离：台账 %d 段，**实际只剥掉 %d 段** —— "
                                "残留的段会被框架再发一次（群里出现重复消息时，这就是原因）",
                                len(segs), _removed)
                        else:
                            logger.info(
                                "[accel] 解析前剥离 %d 段（框架不会再解析它们 ⇒ "
                                "不会重复生成/重复发送）", len(segs))
                        xml_data = stripped
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 解析前剥离失败（按原文本解析）")
            return await original(xml_data, tag_set, *args, **kwargs)
        return parse_xml

    def _ids_splice_factory(self, original):
        """包 `MessageDeliveryService.add_message_ids`：把抢发结果按顺序拼回去。

        2.x 里框架的 `send_xml_messages` 会 `return` 结果列表，acc 直接
        `return early + rest` 就完成了对位；3.0 的 `execute.py` 自己收集结果、
        再调 `add_message_ids(text, message_results)` **按 `<msg>` 位置**贴 ID
        ⇒ 少解析一段就少一个结果，必须在这个函数入口把抢发结果补回最前面
        （顺序 = 抢发顺序 = 文档顺序），否则模型历史里的 message_id 会整体错位。
        """
        def add_message_ids(xml_data=None, message_results=None, *args, **kwargs):
            # ★ v1.0.88：同样**原样透传**（框架加参数不能让这个接管点炸掉）
            ctx = self._strip_ctx.pop(xml_data, None)
            if ctx:
                early, drop_pos = ctx if isinstance(ctx, tuple) else (ctx, [])
                combined = list(early) + list(message_results)
                for pos in drop_pos:
                    if 0 <= pos <= len(combined):
                        combined.insert(pos, _make_id_placeholder(message_results))
                return original(xml_data, combined, *args, **kwargs)
            return original(xml_data, message_results, *args, **kwargs)
        return add_message_ids

    def _consume_chain_record(self, prefix, norm, idents, kinds):
        """按「抢发时那条链自己的签名」对账 —— 3.0 的**首选**判据（v1.0.85）。

        判据（按记账顺序，一条链最多消费一条记录）：
          · **文字**：两侧都用 `chain_signature` 的 `norm_text_only` ⇒ 相等即覆盖；
          · **身份**：`ident_match`（两侧都由元素侧生成 ⇒ `<file>` 的路径解析、
            `Image.md5` 惰性算出这些差异**天然不存在**）；
          · **生成类/结构类**（出图 `<img>`/`<selfie>`、TTS `<record>`、
            生成视频 `<video>`、`<json>`、`<forward>`）：它们的元素身份是
            **生成结果**、与台账里的提示词不可比 ⇒ 唯一可靠的判据是
            「两侧都没有文字、都没有身份、**元素种类完全一致**」+ 本轮顺序。
            没有这条，这类段永远出不了账 ⇒ 框架把它们再发一次（重复）。
          · 记录里的抢发结果若明确失败（`ok=False`）⇒ **只销账、不返回**
            （那一段其实没发出去，要放行框架去发，否则丢内容）。

        为什么这套判据比"段原文对账"可靠：抢发时链就在手上，签名由**同一个函数**
        产出 ⇒ 不受解析期改写影响。段对账保留为兜底（见 `_consume_early_match`）。
        """
        from .early_sent import ident_match

        for key in list(self._chain_records.keys()):
            if not (isinstance(key, str) and key.startswith(prefix)):
                continue
            recs = self._chain_records.get(key) or []
            for i, rec in enumerate(recs):
                r_text, r_ids, r_kinds = rec.get("sig") or ("", [], [])
                text_hit = bool(norm) and bool(r_text) and (
                    norm == r_text or norm in r_text or r_text in norm)

                ids_hit = False
                rest = list(r_ids)
                if idents and r_ids:
                    ids_hit = True
                    for need in idents:
                        j = next((x for x, c in enumerate(rest)
                                  if ident_match(need, c)), None)
                        if j is None:
                            ids_hit = False
                            break
                        rest.pop(j)

                # 无信号段：种类一致即视为同一条
                #   · Json / Forward 这类**两侧都没有身份**的：两侧身份都为空即可；
                #   · 生成类（img/selfie/record/video）：链侧身份是"生成结果"、
                #     与抢发那遍不是同一个值 ⇒ 只要**记账时标了 volatile**
                #     且**元素种类完全一致**就认（这是唯一可靠的判据）。
                same_kind = (bool(kinds) and list(kinds) == list(r_kinds)
                             and not norm and not r_text
                             and (bool(rec.get("volatile")) or (not idents and not r_ids)))
                if not (text_hit or ids_hit or same_kind):
                    continue

                if same_kind:
                    # 生成类/结构类：身份维度天然不可比 ⇒ 种类一致即视为整段覆盖
                    pass
                else:
                    text_covered = (not r_text) or (norm == r_text)
                    ids_covered = (not r_ids) or (ids_hit and not rest)
                    if not (text_covered and ids_covered):
                        continue

                recs.pop(i)
                if not recs:
                    self._chain_records.pop(key, None)
                result = rec.get("result")
                if getattr(result, "ok", True) is False:
                    # 销账但放行：那一段其实**没投递成功**，框架这次发的正是它
                    return None
                return result
        return None

    def _consume_early_match(self, sid, chain):
        """3.0 按链对账：该链若与某已抢发段内容一致，消费台账并还回当时的结果。

        2.x 的发送层剥离按"段文本"工作；3.0 的框架发送按"链"逐条走
        MessageDeliveryService.send_message_chain —— 拦截点改到这里后，
        用与 2.x 相同的规范化做内容比对，位置与动作天然 1:1
        （每条链至多消费一条台账），add_message_ids 的位置对齐不破。
        台账/结果数量不齐时放弃对账放行原发送（宁可重发，不还错结果）。

        ★★★ v1.0.83（线上：**表情包被发两次、文字正常**）：
          原来只按"可见文字"比对（`getattr(e, "text", "")`），而贴纸/图片/语音
          这类元素**没有 text** ⇒ 纯媒体链永远匹配不上 ⇒
          `if not norm: return None` ⇒ 框架把已抢发的表情包**再发一次**。
          触发条件正是"表情独立成行"这类 AFTER_XML_PARSE 改写：
          它把一个 `<msg>` 拆成"贴纸一条 + 文字一条"，于是框架发来的是一条
          **纯媒体链**，正好落进原来的盲区（文字链能对上 ⇒ 文字不重复）。

          现在两侧都用**身份令牌**（`early_sent.elem_ident` / `seg_idents`）：
            · 文字链：比对规则与原来完全一致（不回归）；
            · 媒体链：链上每个身份都能在台账段里找到（各自最多用一次）即命中。
          Tag 名不参与比对 ⇒ `<file type="image">url</file>` 生成的 Image 元素也能对上；
          拿不到身份的生成类（`<img>` 出图 / `<record>` TTS）**不参与**比对 ——
          保守取向：宁可不剥（最多重复一次），也不误吃（会丢内容）。

        ★ 部分消费：同一条台账段可以被**两条链分别消费**（贴纸链吃媒体部分、
          文字链吃文字部分），只有"整段都被这条链覆盖"时才出账。
          否则贴纸链先把整段吃掉，兄弟文字链就找不到台账 ⇒
          **文字会被重复发送**（与本 bug 对称的另一种重复）。
          台账按 (sid, 轮次) 分键、轮末清理，所以"多留一会儿"是安全的。
        """
        from .early_sent import (chain_signature, elem_ident, ident_match,
                                 norm_text_only, seg_idents)

        # ★★★ v1.0.85：链侧签名与**抢发时**记下的签名同源（同一个 `chain_signature`）
        norm, idents, kinds = chain_signature(chain)
        if not norm and not idents and not kinds:
            return None
        prefix = sid + "\x00"

        # ① 首选：按"抢发时那条链自己的签名"对账（元素侧，无解析期差异）
        try:
            hit = self._consume_chain_record(prefix, norm, idents, kinds)
            if hit is not None:
                return hit
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 抢发链签名对账异常（回退到段对账）")

        # ② 兜底：按**段原文**对账（v1.0.83/84 的逻辑）
        #   用于"框架拿到的链与抢发时那条链不同形"的情形（例如插件在
        #   框架那遍改写了链的顺序/粒度，或本条链是抢发之后新增的）。
        #   注意这里用的仍是链侧签名（norm/idents），只是台账换成了段原文。
        for k in list(self._sent_ledger.keys()):
            if not (isinstance(k, str) and k.startswith(prefix)):
                continue
            segs = self._sent_ledger.get(k) or []
            for i, seg in enumerate(segs):
                seg_text = norm_text_only(seg)        # 只算可见文字（媒体 id 不算文字）
                seg_ids = seg_idents(seg)

                text_hit = bool(norm) and bool(seg_text) and (
                    norm == seg_text or norm in seg_text or seg_text in norm)

                ids_hit = False
                rest_ids = list(seg_ids)
                if idents:
                    ids_hit = True
                    for need in idents:
                        # ★★ v1.0.84：这里必须是 ident_match，不能写 `need in rest_ids`。
                        #   框架解析 <file> 时会把本地路径解析成**绝对路径**
                        #   （kira-ai 的 resolve_local_send_path：data/files/x.txt →
                        #   <数据根>/files/x.txt），而台账里存的是**原始 XML 段**
                        #   ⇒ 严格相等时整段身份都在、却一个都对不上 ⇒ 台账不出账
                        #   ⇒ 框架把文件再发一次（线上：文件重复发送）。
                        #   放宽规则只针对「data/ 相对路径 vs 它的绝对形式」，
                        #   同名不同目录**不算匹配**（宁可重复，也不误吃丢内容）。
                        _at = next((j for j, c in enumerate(rest_ids)
                                    if ident_match(need, c)), None)
                        if _at is None:
                            ids_hit = False
                            break
                        rest_ids.pop(_at)

                if not (text_hit or ids_hit):
                    continue

                results = self._early_results.get(k) or []
                if i >= len(results):
                    return None
                result = results[i]

                # 整段覆盖 = 该段的文字被这条链完整给出（段无文字则免）
                #           + 该段的身份全被消费（段本来没有身份则天然满足）
                text_covered = (not seg_text) or (norm == seg_text)
                ids_covered = (not seg_ids) or (ids_hit and not rest_ids)
                fully_covered = text_covered and ids_covered
                if fully_covered:
                    results.pop(i)
                    segs.pop(i)
                    if not segs:
                        self._sent_ledger.pop(k, None)
                        self._early_results.pop(k, None)
                return result
        return None

    def _reset_turn_ledger(self, sid: str, event_id=None) -> None:
        """清掉**这一轮**的抢发台账（**不是整个会话**！）。

        ★★★ 2026-09-27 修正：原来这里是"每轮开始时清掉**该会话**的台账"，
          但框架会重叠处理同一个会话（见 `_ckey` 的说明），
          于是第 2 轮一开始就把**还在跑的第 1 轮**的台账清掉了
          ⇒ 第 1 轮剥离失败 ⇒ 重复发送。
        ⇒ 现在只清 (sid, 本轮轮次) 那一份；其他轮次的**绝不触碰**。
        """
        if not sid:
            return
        try:
            k = self._ckey(sid, event_id)
            self._sent_ledger.pop(k, None)
            self._resp_by_sid.pop(k, None)
            self._early_results.pop(k, None)
            self._chain_records.pop(k, None)
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
        # ★ v1.0.88：发送入口可能是 `ctx.send_message_chain`（框架推荐路径），
        #   此时没有 `message_processor` 也能抢发 ⇒ 守卫不能再要求 mp 非空。
        _ctx_send = getattr(self.ctx, "send_message_chain", None)
        if ctx is None or (mp is None and not callable(_ctx_send)) \
                or not ctx.sid or ctx.tag_set is None:
            # ★ 2026-10-05 降级为 debug：这是**可预期情形**而非故障 ——
            #   不经过消息管线的 LLM 调用（记忆整理 / JEV 决策等后台调用）
            #   本来就没有发送上下文，整段交回框架是完全正确的行为。
            logger.debug("[accel] 无发送上下文（多为后台 LLM 调用），本段交回框架发送")
            return False

        # ★★★ 2026-09-28（串会话加固 —— 用户："消息发送到错误会话，比如使用了跨会话功能后"）：
        #   **发送目标必须与"框架最终会用的那个 sid"一致**，否则同一条回复会被劈到两个会话：
        #     · 我们：用请求时捕获的 `ctx.sid`
        #     · 框架：`send_xml_messages(event, ...)` 里取 **事件当前** 的 `event.sid`
        #   两者在正常轮次里恒等；但只要**有插件在轮次中途更换了事件的会话**
        #   （生态里确实存在这种形状：主动消息 / 定时任务先构造事件、再 `event.session = ...`
        #    覆盖成真实会话；任何在钩子里改写事件的插件同理），二者就会分叉 ——
        #   那一刻我们若继续"按旧 sid 抢发"，这一句就会进**旧会话**，
        #   而框架随后把同一段再发进**新会话** ⇒ 用户看到"消息发错会话"。
        #   ⇒ 这里做一次显式核对：**分叉就拒绝抢发、整段交回框架**
        #     （由它按当前会话统一发送）。宁可少抢发一段（只影响首字速度），
        #     也绝不把内容送进错误的会话。
        #   为什么用"当前事件"而不是实例缓存：`ctx.event` 是**本次请求**的事件对象，
        #   它被改写时我们看到的就是改写后的值 —— 正是要与框架保持一致的那个值。
        try:
            _ev_now = getattr(ctx, "event", None)
            _sid_now = getattr(_ev_now, "sid", None) if _ev_now is not None else None
            _sess = getattr(_ev_now, "session", None)
            if _sid_now is None and _sess is not None:
                _sid_now = getattr(_sess, "sid", None)
            if _sid_now and _sid_now != ctx.sid:
                logger.warning(
                    "[accel] 会话在轮次中途被更换（%s → %s）⇒ 本段不再抢发，"
                    "交回框架按当前会话发送（防串会话）", ctx.sid, _sid_now)
                return False
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 会话一致性核对失败（按原 sid 继续）")

        # 本段在"抢发结果 / 台账"里共用的键：`(sid, 轮次)` 复合键（见 `_ckey`）。
        # ★ 一次算好、处处用同一个 —— 之前 `_early_results` 用裸 sid、其它两处用
        #   复合键，导致两轮之间串账（审计 P2，见下方记账处的说明）。
        _k = self._ckey(ctx.sid, getattr(getattr(ctx, "event", None), "event_id", None))

        # 解析失败 ⇒ 没投递过任何东西，交回框架（由它统一报错/处理）。
        #
        # ★★★ 2026-09-28（用户："根据 KiraAI 最新规范，修复这一报错"）：
        #   模型写出未转义的 `&` / `<` 时，这一段 XML 就不可解析 ——
        #   这是**常见且可预期**的情形，不是插件故障：
        #     · 框架自己解析失败时也只打一行 error、不给堆栈
        #       （core/plugin/builtin_plugins/kira-ai/main.py: `except ET.ParseError`）；
        #     · 生态的约定是"先修再发"，而修复发生在 ON_LLM_RESPONSE 阶段
        #       （xml_tag_fixer 在 ON_LLM_RESPONSE 里转义；内置 kira-ai 则调另一个
        #        模型去修）—— **都在我们之后**；
        #     · 我们在**流式中途**，按规范不得自行转义/改写 XML：
        #       局部修复会与下游修复产生分歧 ⇒ 重复发送或内容对不上。
        #   ⇒ 正确姿势是"安静地交回去"：一行 warning、不带堆栈
        #     （细节降级到 debug）。行为完全不变，只是日志不再像故障。
        #   守卫：tests/test_unparsable_handback.py
        try:
            actions = await self._parse_seg_actions(seg, ctx.tag_set, mp)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[accel] 抢先发送：本段暂不可解析（多为模型写了未转义的 & 或 <），"
                "已交回框架按原流程处理")
            logger.debug("[accel] 解析失败详情（%s）: %s", type(e).__name__, e)
            return False

        # ★ Root 级标签动作（RootTagAction）必须交回框架 —— 本函数的发送循环
        #   只处理 MessageChain，遇到它会**静默跳过**；若这段是「链 + root 动作」
        #   混合，链发了、整段又记了台账 ⇒ 框架剥离后 root 动作**永远不会执行**。
        #   交回框架由它走 `tag.handle(...)`（语义唯一正确的去处）。
        #   放在广播**之前**：钩子只该在框架那遍见到这段，不该被执行两次。
        try:
            from core.tag import RootTagAction as _RTA
        except Exception:  # noqa: BLE001
            _RTA = None
        if _RTA is not None and any(isinstance(a, _RTA) for a in actions):
            logger.debug("[accel] 本段含 root 级标签动作，交回框架执行（不抢发）")
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
        from core.chat import MessageChain
        # ★ 广播前先记下"本来有没有可发内容" —— 供后面识别「插件接管」（见循环后）。
        had_sendable = any(
            isinstance(a, MessageChain) and not a.is_empty() for a in actions)
        actions = await self._broadcast_after_xml_parse(ctx, actions)

        # ★★ 与框架语义对齐：AFTER_XML_PARSE 阶段事件被停止 ⇒ **整批不发**。
        #   框架 send_xml_messages 此时直接 `return None`（一条都不发）；
        #   而这里之前只 break 出广播循环、**仍照常发送** —— 内容过滤/拦截类
        #   插件的「停止」会被抢发路径无视。交回框架，由它走完「停止 ⇒ 不发」
        #   的统一逻辑（广播里已打过 INFO，这里不再重复记日志）。
        if getattr(ctx.event, "is_stopped", False):
            return False

        delivered = False

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

            # ★★★ v1.0.87：抢发链的签名必须在**发送之前**取！
            #   下游插件可能在发送层改写这条链（生态实例：qqbot-fullmsg-bridge
            #   会在 `_send_message_inner` 里做媒体强转 `coerce_media_chain`，
            #   至于摘走富媒体/键盘元素的实现同样是"在发送那一刻动链"）。
            #   若等发送之后再取，记进台账的就是**被改写后**的链，
            #   而框架那遍解析出的是**原始**链 ⇒ 对账失配（多生成/多发送一类问题）。
            #   所以先取签名（= acc 自己解析出来的产物），再发。
            _pre_sig = None
            try:
                _pre_sig = _chain_signature(action)
            except Exception:  # noqa: BLE001
                logger.debug("[accel] 取抢发链签名失败（不记签名，仅影响兜底对账）")

            # ── 投递本身：这一步成功即等于"已经发出去了"，后面任何失败都不改这个事实 ──
            _inflight_key = str(ctx.sid or "-")
            _inflight_map = getattr(self, "_early_inflight_sids", None)
            if not isinstance(_inflight_map, dict):
                _inflight_map = self._early_inflight_sids = {}
            _inflight_map[_inflight_key] = _inflight_map.get(_inflight_key, 0) + 1
            try:
                # ★ v1.0.88：优先走**框架推荐且未弃用**的入口
                #   （新版框架会把 `MessageProcessor.send_message_chain` 标弃用：
                #    线上日志里每发一段就刷一条 DeprecationWarning，且其签名随时可能变）。
                #
                # ★★★ v1.0.89：把"出站消息的 bot 身份"也带上。
                #   rc2 起框架把 `send_message_chain` 的 `self_id=` 换成了 `bot: User`
                #   （`execute.py` 里是 `bot=event.bot`），并用它记录出站历史：
                #   `record_outgoing(self_id=bot.user_id, sender_name=bot.nickname)`。
                #   不带 ⇒ 记录里 `self_id=None`、名字回落人设名 ⇒ 与框架自己发的
                #   记录**不一致**（而 acc 抢发的正是大多数消息）。
                #   ⚠️ 只在目标**接受** `bot=` 时才传（rc1 / 2.x 都没有这个参数）；
                #      判据用 `inspect`（缓存），**绝不 try/except 重试** ——
                #      发送有副作用，重试等于重发。
                _fn = _ctx_send if callable(_ctx_send) else mp.send_message_chain
                _kw = {}
                if self._accepts_kwarg(_fn, "bot"):
                    _kw["bot"] = self._event_bot(ctx)
                result = await _fn(ctx.sid, action, **_kw)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 抢先发送失败，本段交回框架发送（不丢内容）")
                return delivered
            finally:
                _left = _inflight_map.get(_inflight_key, 1) - 1
                if _left > 0:
                    _inflight_map[_inflight_key] = _left
                else:
                    _inflight_map.pop(_inflight_key, None)
            delivered = True

            # ── 以下都是"投递之后的杂事"，失败只告警，绝不改变 delivered ──
            try:
                # ★ 记下这一段的结果：发送层的剥离会把"已抢先发出的段"从文本里去掉，
                #   而框架的 _add_message_ids 是**按位置**把 message_results 贴到 <msg> 上的。
                #   只还回"剩余段"的结果 ⇒ 位置全错 ⇒ 模型在自己历史里读到**错位的
                #   message_id**（提示词明确说这个 ID 由系统添加，模型会用它引用消息）。
                # ★★★ 2026-09-28（审计 P2）：键必须是**复合键**（与台账 / 响应缓存一致）。
                #   原来这里用 `ctx.sid` 裸会话键、且**从不清** ⇒ 一轮"抢发了却没走到发送"
                #   （事件被 stop / 并发另一轮抢先走到发送）时，残留结果会被**下一轮**
                #   `pop(sid)` 取走 ⇒ 返回序列里混进上一轮的结果 ⇒ id 全部错位，
                #   甚至"本轮没抢发也被判定 early"。现已与 `_sent_ledger` 完全对齐：
                #   同键写、同键读、同键清（见 `_install_early_sent_strip`）。
                self._early_results.setdefault(_k, []).append(result)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 记录抢发结果失败（不影响已投递的事实）")

            try:
                # ★★★ v1.0.85：同时记下**这条链自己的签名**（元素侧、与对账同一函数）。
                #   这是 3.0 按链对账的首选判据 —— 段原文是"解析前"的文本，
                #   链是"解析后"的元素，两者异源 ⇒ 解析期任何改写（路径解析、
                #   惰性 md5、生成类结果）都会让台账出不了账 ⇒ 框架再发一次。
                #   记元素侧签名后，两侧由同一函数产出 ⇒ 从根上没有这类差异；
                #   生成类（出图/TTS/视频）没有可比身份，靠 `kinds`（元素种类）
                #   在**本轮内按顺序**配对。
                self._chain_records.setdefault(_k, []).append({
                    "sig": _pre_sig if _pre_sig is not None else _chain_signature(action),
                    "result": result,
                    # ★ 生成类标签（出图/自拍/TTS/生成视频）：元素身份是**生成结果**，
                    #   与框架那遍**再次生成**的结果必然不同 ⇒ 身份维度天然不可比，
                    #   只能靠"元素种类一致 + 本轮顺序"配对（见 `_consume_chain_record`）。
                    #   抢发的这一刻我们从原始段就知道是不是生成类 ⇒ 在这里标出来。
                    "volatile": bool(re.search(r"<(?:img|selfie|record|video)\b",
                                               seg or "", re.I)),
                })
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 记录抢发链签名失败（不影响已投递的事实）")

            try:
                self._mark_sent(ctx)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 记录发送时刻失败（不影响已投递的事实）")

            # ★ 补广播 ON_MESSAGE_SENT（绕过框架发送层就必须补）
            try:
                event_handler_reg, EventType = event_system()
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

        # ★★★ 插件接管识别（2026-10-05，「折扇」插件重复发送事故的根因修复）：
        #   「在 AFTER_XML_PARSE 里自己发送、并把链从待发列表删掉」是生态惯用
        #   形状（fold_fan_formatter 等）。广播前有可发链、广播后一条不剩 =
        #   钩子**接管了本段的投递**。若仍按"我自己没发 = 没投递"把段交回框架，
        #   框架会再派发一次同一钩子 ⇒ 插件再发一遍 ⇒ 用户看到重复发送
        #   （线上实证：穗文本 ×2 + 合并转发 ×2）。
        #   ⇒ 识别为已投递：记台账（发送层的剥离会把它从文本里拿掉，框架那遍
        #     插件根本见不到它），并补占位结果保持 message_id 位置对齐。
        #   安全论证：插件若是"过滤器"语义（故意丢弃），不剥离时框架那遍也会
        #     被同一过滤器再丢一次，结果一致 ⇒ 不丢内容；若是"自发"语义 ⇒
        #     正好防重复。两种语义下都不丢、不重。
        #   守卫：tests/test_plugin_takeover.py
        if not delivered and had_sendable and not any(
                isinstance(a, MessageChain) and not a.is_empty() for a in actions):
            logger.info("[accel] 本段已被 AFTER_XML_PARSE 插件接管（待发列表被清空）"
                        " ⇒ 视为已投递，不再交回框架（防重复发送）")
            delivered = True
            try:
                # 占位结果：框架 _add_message_ids 按位置贴 ID，message_id=None
                # 会被置为空串（核心 message_manager.py 已处理 None），对齐不破。
                from core.chat.message_utils import KiraIMSentResult
                self._early_results.setdefault(_k, []).append(
                    KiraIMSentResult(message_id=None, ok=True))
            except Exception:  # noqa: BLE001
                logger.debug("[accel] 记录接管占位结果失败（不影响已投递的事实）")
            try:
                self._mark_sent(ctx)
            except Exception:  # noqa: BLE001
                pass
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
                # ★★★ 按 (sid, 轮次) **复合键**记账 —— 这是修掉"重叠轮次互相清账"
                #   的关键：两轮各用各的键，谁也不会清掉谁（详见 `_ckey`）。
                #   `_k` 在函数开头已算好（`_early_results` 用的是同一个键）。
                self._sent_ledger.setdefault(_k, []).append(seg)
                # 记"最后一次活动时间"，供 `_clear_stale_round_state` 判断陈旧
                if not isinstance(getattr(self, "_ledger_ts", None), dict):
                    self._ledger_ts = {}
                self._ledger_ts[_k] = time.time()
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
        # ★ 跨世代：同上，3.0 该类的路径变了
        try:
            _cls = openai_compatible_client()
            if _cls is not None:
                targets.append((_cls, "_build_request_kwargs", "openai"))
            else:
                logger.warning("[accel] 未找到 OpenAI 兼容客户端，跳过该注入点")
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 解析 OpenAI 兼容客户端失败，跳过该注入点")
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
            # 用户显式关了「不需要时显式关闭思考」⇒ 尊重选择、不去动提供商设置。
            # 但把这一点记进判定对象：日志会显示"未注入关闭参数"，
            # 让"判了关、实际没关"永远不会再悄悄发生（2026-10-05 用户实测的坑）。
            decision.applied = "未注入关闭参数"
            return kwargs
        # ★★★ 2026-09-28（用户："是否能由我们来成功控制"）：
        #   注入时**清掉提供商那份同维度的思考键**，让"思不思考"在请求体里
        #   只剩一个声音（否则两把不同拼写的钥匙同时到达网关，谁生效是轮盘赌——
        #   实测提供商配 `thinking:{enabled/disabled}` 时我们**压不住也打不开**）。
        #   清场范围 = 我们实际表达的维度：
        #     · 我们仍在发强度（或压根没开「跟随」）⇒ 强度维度也归我们 ⇒ 清
        #     · 「跟随提供商强度」开着、且我们没发强度 ⇒ 强度归它 ⇒ **不清**
        #     · 我们本轮在**关思考** ⇒ 强度没有意义 ⇒ 清掉（免得网关看到
        #       disabled + max 这种混合体）
        _has_our_effort = any(("effort" in str(k).lower() or "budget" in str(k).lower())
                              for k in params)
        _clear_effort = ((not decision.enabled) or (not self.thinking_follow_provider)
                         or _has_our_effort)
        kwargs = apply_thinking_params(kwargs, params, client_kind,
                                       clear_effort=_clear_effort)
        # ★ v1.0.78：回填"实际动作"（日志展示用）——判定与请求体脱节时一眼可见
        decision.applied = "已注入开启参数" if decision.enabled else "已注入关闭参数"
        return kwargs


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
            async def execute_tool(self, *args, **kwargs):
                # ★ v1.0.90：原样透传；自己要的三个值从 args/kwargs 里取
                event = args[0] if args else kwargs.get("event")
                resp = args[1] if len(args) > 1 else kwargs.get("resp")
                tool_set = args[2] if len(args) > 2 else kwargs.get("tool_set")
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
                    return await original(self, *args, **kwargs)

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
                event_handler_reg, EventType = event_system()
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
            # 3.0 起延迟配置在 MessageDeliveryService 上；2.x 在 MessageProcessor 上。
            # 先探 delivery、回落 mp（getattr 默认值即旧路径，两代都读到框架属性）。
            _delay_src = getattr(self.ctx, "message_delivery", None)
            lo = float(getattr(_delay_src, "min_message_delay", None)
                       or getattr(mp, "min_message_delay", 0.0) or 0.0) if _delay_src is not None \
                else float(getattr(mp, "min_message_delay", 0.0) or 0.0)
            hi = float(getattr(_delay_src, "max_message_delay", None)
                       or getattr(mp, "max_message_delay", lo) or lo) if _delay_src is not None \
                else float(getattr(mp, "max_message_delay", lo) or lo)
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
            def _save_memory(self, *args, **kwargs):
                # ★ v1.0.90：框架调用点，签名同样要透传
                memory = args[0] if args else kwargs.get("memory")
                path = args[1] if len(args) > 1 else kwargs.get("path")
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
            async def send_xml_messages(self, event=None, xml_data=None,
                                        tag_set=None, *args, **kwargs):
                # ★ 按 sid 取"本轮响应"。
                #   ⚠️ 刻意**不**回退到 self._current_resp：那是实例级的，
                #   并发会话下可能是**别的会话**的响应，拿它去剥离就会
                #   把本会话还没发过的内容剪掉（丢消息）。
                #   查不到就按"没抢发过"处理（n=0，全部交给框架发）——
                #   最坏是重复一条，而不是丢内容。
                sid_now = getattr(event, "sid", None)
                # ★★★ 复合键 (sid, 轮次) —— 必须与 `_emit_segment` / `_remember`
                #   用**同一个**键。这样"重叠的两轮"各拿各的台账与响应，
                #   不会再互相清掉/覆盖（线上重复发送的真根因，详见 `_ckey`）。
                _k = plugin._ckey(sid_now, getattr(event, "event_id", None)) if sid_now else None
                # 当前任务响应优先；键失配/旧同文条目时再按最新文本择优。
                _k, resp = plugin._response_for_text(xml_data, _k)
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
                ledger = plugin._sent_ledger.get(_k, []) if _k else []
                # ★★★ 【2026-09-27 第五次修复】按【步】消费台账 ——
                #   彻底删掉"已消费 ⇒ 跳过剥离"这个判断（它才是重复的来源）。
                #
                #   前四版都建立在同一个**错误前提**上：
                #       「抢发只发生在第一步，后续步的文本是新生成的、从没发过」
                #   —— 这个前提是错的。流式引擎 patch 在
                #   `ProviderManager.get_model_client` 上（见 `_install_stream_engine`），
                #   **每一次 LLM 请求都会新建一个引擎**（`_make_engine`），
                #   而 `emit` 回调就是 `_emit_segment`
                #   ⇒ **多步 loop 的每一步都会抢发它自己那一段**。
                #
                #   于是旧逻辑必然出错（框架 `core/message_manager.py:766`）：
                #       `event` 是**整个多步循环共用的同一个对象**
                #       （`async for step in agent_executor.run(...)` 在循环外创建）
                #       ⇒ 同轮各步的 `event_id` **完全相同**
                #       ⇒ 第二步命中"已消费 ⇒ n=0 ⇒ 不剥离"
                #       ⇒ 第二步**自己刚抢发的** C,D 被框架**再发一次**
                #       ⇒ 用户看到 `C,D | C,D`（每一步各重复一次）。
                #
                #   ⇒ 正确模型：**每一次 send_xml_messages 对应一步，
                #     要剥的就是"上一次发送之后、这一步抢发的那些段"。**
                #     而 `finally` 里每次调用后清台账，天然就是这个语义。
                #
                #   `event_id` 依然有用，但用途**完全不同**：不是"跳过剥离"，
                #   而是**判断台账是否属于本轮** —— 万一上一轮"抢发了但没走到发送"
                #   （事件被 stop），残留台账若参与本轮剥离就会**误剪 = 丢内容**。
                # ★ 键里**已经带了轮次** ⇒ 不需要再比较"轮次键"：
                #   别的轮次的台账根本不在这个键下面，不可能被误用（也就不会误剪丢内容）。
                #   （原来那套 `sid\x00ev` 比较 + "丢弃残留台账" 已经多余，删掉。）
                if ledger:
                    # 台账优先（更权威）
                    if n > 0 and n != len(ledger):
                        # 两边都有但不一致 —— 自洽性已破但台账能兜住，
                        # 记 debug 即可（2026-10-05 降噪：不值得吓用户）
                        logger.debug(
                            "[accel] 已发段数不一致：响应标记 %d 段、台账 %d 段，以台账为准",
                            n, len(ledger),
                        )
                    n = len(ledger)
                    segs = list(ledger)
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

                # ★★★ 2026-09-28（审计 P2）：`_early_results` 也改用**同一个复合键**
                #   （原来按裸 sid 存取且从不清 ⇒ 上一轮残留会混进本轮的返回序列
                #   ⇒ 框架按位置贴 message_id 时全部错位；详见 `_emit_segment` 处说明）。
                #   键 `_k` 与 `_sent_ledger` / `_resp_by_sid` 完全一致 ⇒ 同键写、同键读、同键清。
                early = plugin._early_results.pop(_k, []) if _k else []
                # 必须在 n==0 路径之前初始化：early 非空但当前响应标记/台账取不到时，
                # 返回结果仍会走到占位插入逻辑。
                _drop_pos = []
                if n > 0:
                    # ★ 用"按内容剥离"：count 是按**原始**文本数的，
                    #   而 xml_tag_fixer 这类插件可能已改写 text_response
                    #   （补 <msg> / 拆分消息块 / 合并块 / 转义实体），
                    #   按序号切会少切（重复）或多切（丢内容）。
                    original_xml_data = xml_data
                    xml_data = strip_early_sent_smart(xml_data, n, segs)

                    # ★★ v1.0.93：2.x 也必须做「同正文、仅差 reply/@」内容去重。
                    # 1.0.92 只在 3.0 的 parse_xml 路径接入，2.x 的 send_xml_messages
                    # 仍会把残留块再发一次（群里就是「正文 @昵称 正文」）。
                    # 同时记录原块位置，返回结果时插入空占位，保证后续消息 ID 不错位。
                    if getattr(plugin, "dedupe_blocks", True):
                        try:
                            from .early_sent import drop_duplicate_blocks, dropped_positions
                            _deduped, _dropped = drop_duplicate_blocks(xml_data, segs)
                            if _dropped:
                                _positions = dropped_positions(original_xml_data, _dropped)
                                # 映射不全时保守保留：宁可重复，也不让后续 ID 错位的去重。
                                if len(_positions) == len(_dropped):
                                    xml_data = _deduped
                                    _drop_pos = _positions
                                    logger.info("[accel] 发送前内容去重 %d 块（仅差 reply/@，不会重复发送）",
                                                len(_dropped))
                                else:
                                    logger.warning(
                                        "[accel] 内容去重位置映射不完整（%d/%d），为保消息 ID 对位本次不去重",
                                        len(_positions), len(_dropped))
                        except Exception:  # noqa: BLE001
                            logger.exception("[accel] 发送前内容去重失败（按剥离后原文发送）")

                    # ★★ 事后核对（**唯一有意义的重复判据**）：
                    #   剥离之后，剩余文本里若**仍然整段包含**某个已抢发段的内容，
                    #   那才是真的会把同一段再发一次 —— 这时才报。
                    #   与"同一 sid 出现两次"那种结构判据不同，这个只在真有重叠时触发。
                    try:
                        from .early_sent import _norm_seg, ident_match, seg_idents
                        rest_norm = _norm_seg(xml_data)
                        rest_ids = seg_idents(xml_data)       # ★ v1.0.83：媒体身份也要核对
                        for _s in segs:
                            _piece = _norm_seg(_s)
                            if _piece and _piece in rest_norm:
                                logger.warning(
                                    "[accel] 剥离后剩余文本里仍含已抢发的段"
                                    "（这一段可能被重复发送）：%s", _piece[:48])
                                break
                            _hit_id = next((i for i in seg_idents(_s)
                                            if any(ident_match(i, c) for c in rest_ids)), "")
                            if _hit_id:
                                logger.warning(
                                    "[accel] 剥离后剩余文本里仍含已抢发的媒体"
                                    "（这一段可能被重复发送）：%s", _hit_id[:48])
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
                    rest = await original(self, event, xml_data, tag_set,
                                          *args, **kwargs)
                    # ★ 把抢先发出的结果按【顺序】拼回去：
                    #   框架随后用 _add_message_ids 按位置给 <msg> 贴 ID，
                    #   只还回剩余段会让 ID 全部错位（模型会引用错消息）。
                    #   注意：这里只是"还账"，框架**并没有重发**那些段，
                    #   所以锁里也不会为它们多睡一次（提速收益不受影响）。
                    if early:
                        combined = early + list(rest or [])
                        for pos in _drop_pos:
                            if 0 <= pos <= len(combined):
                                combined.insert(pos, _make_id_placeholder(rest or early))
                        return combined
                    return rest
                finally:
                    # ★ 只清"跟这一次调用绑定"的东西（响应标记 / 响应缓存）。
                    if resp is not None:
                        resp.__dict__.pop("_accel_early_sent_count", None)
                    if _k:
                        plugin._resp_by_sid.pop(_k, None)
                        # ★★★ 台账**按步消费后立即清**（连同轮次键）。
                        #
                        #   这不是"过早清理" —— 它正是正确语义：
                        #   每次 `send_xml_messages` 对应多步 loop 的**一步**，
                        #   要剥的就是"上一次发送之后、这一步抢发的那几段"。
                        #   清掉之后，下一步在 `_emit_segment` 里重新开账
                        #   （它会带上新的轮次键）。
                        #
                        #   历史教训（这段注释以前自相矛盾，现在只留正确的那条）：
                        #   这里一度被改成"留到轮结束才清"，理由是
                        #   "第二步台账已空 ⇒ n=0 ⇒ 整批重复"。
                        #   但那个推断是错的 —— 第二步的台账是**第二步自己抢发时建的**
                        #   （见 `_emit_segment`），并不会因为这里清了就变空。
                        #   真正造成重复的是后来据此加上的"已消费 ⇒ 跳过剥离"，已删除。
                        plugin._sent_ledger.pop(_k, None)
            # ★★ 声明「有不可撤销的副作用」：这个函数的调用会**真的把消息发出去**。
            #   一旦它抛异常，`patches.guard` 默认会"回落原实现"——那等于**再发一遍**
            #   （用户线上看到的就是同一段回复重复出现）。
            #   声明之后 guard 不再回退，宁可把异常交给上层。
            return mark_side_effects(send_xml_messages)

        h = PatchHandle("early_sent_strip")
        if hasattr(MessageProcessor, "send_xml_messages"):
            # 2.x 形态：类级包裹 send_xml_messages（按段文本剥离）
            if install(h, MessageProcessor, "send_xml_messages", factory) is not None:
                self.patches.add(h)
                logger.info("[accel] 发送层已接管（剥离已抢发段，避免重复发送）")
        else:
            # 3.0+ 形态：send_xml_messages 已删，框架发送按链走
            # MessageDeliveryService.send_message_chain —— 改包它做按链对账去重。
            delivery = getattr(self.ctx, "message_delivery", None)
            dcls = type(delivery) if delivery is not None else None
            if dcls is not None and hasattr(dcls, "send_message_chain"):
                def delivery_factory(original):
                    # ★★★ v1.0.88：签名**必须**容忍框架加参数，并原样透传。
                    #   线上事故（2026-10-13 20:49，用户环境的新版框架）：
                    #   框架给 `send_message_chain` 加了 `bot=` 关键字
                    #   （`MessageProcessor.send_message_chain` 已弃用，转发时会带上它），
                    #   而这里原来把签名写死成 `(d_self, session, chain, *, memory_message=None, self_id=None)`
                    #   ⇒ `TypeError: got an unexpected keyword argument 'bot'`。
                    #   更糟的是：这个接管点声明了"有不可撤销的副作用、**不回退原实现**"
                    #   ⇒ 不是退回旧行为，而是**整条发送失败** —— 框架直发/弃用入口/抢发
                    #   三个路径全被挡死，实测投递次数 0（消息一条都发不出去）。
                    #   ⇒ 现在只按位置取 session/chain，其余 `*args, **kwargs` 原样转发。
                    async def send_message_chain(d_self, session=None, chain=None,
                                                 *args, **kwargs):
                        _session_key = session if isinstance(session, str) else getattr(session, "sid", None)
                        _inflight = getattr(self, "_early_inflight_sids", {}).get(
                            str(_session_key or "-"), 0)
                        if not _inflight:
                            try:
                                hit = self._consume_early_match(session, chain)
                                if hit is not None:
                                    self._stats["strip_calls"] = self._stats.get("strip_calls", 0) + 1
                                    return hit
                            except Exception:  # noqa: BLE001
                                logger.exception("[accel] 抢发对账异常（放行原发送）")
                            # ★★ 交接节奏（v1.0.83）：框架的 sleep 在**每段之后**，
                            #   它发的第一条不会等待 ⇒ 抢发段与框架首段之间会出现
                            #   **零间隔**（"两段挤成一条"）—— 2.x 侧当年修过同一个坑，
                            #   3.0 的链级路径没搬过来，这里补齐。
                            #
                            #   判据天然收敛、不会双重等待：只有"距上次发送 < 配置下限"
                            #   时才等；而框架自己每发一条就 sleep 一次（min..max），
                            #   所以同轮后续链到来时已经满足间隔，不会再等。
                            try:
                                _lo, _hi = self._frame_delay()
                                _last = self._last_seg_ts.get(session)
                                if _hi > 0 and _last is not None and (time.monotonic() - _last) < _lo:
                                    _ctx_now = SendCtx(session, None, None)
                                    await self._pace(_ctx_now)
                                    self._mark_sent(_ctx_now)
                            except Exception:  # noqa: BLE001
                                logger.exception("[accel] 交接节奏处理失败（不影响发送）")
                        return await original(d_self, session, chain, *args, **kwargs)
                    return mark_side_effects(send_message_chain)

                h2 = PatchHandle("early_sent_dedupe_delivery")
                if install(h2, dcls, "send_message_chain", delivery_factory) is not None:
                    self.patches.add(h2)
                    logger.info("[accel] 发送层已接管（按链对账去重已抢发段）")

                # ── ★★★ v1.0.86：把接缝补到**解析之前**（等价 2.x 的入口剥离）──
                #   这是"生成类被生成两次"的根治：只要框架那遍**解析不到**已抢发段，
                #   标签处理器（出图/TTS/视频）就不会被再执行一次。
                #
                #   ⚠️ `parse_xml` / `add_message_ids` 都是 **staticmethod**：
                #      直接 setattr 一个普通函数会让**实例调用多传一个 self**
                #      （execute.py 里是 `delivery.parse_xml(...)`）⇒ 必须包回
                #      `staticmethod`，还原时同样要还原成 staticmethod。
                for _attr, _factory, _name in (
                    ("parse_xml", self._parse_strip_factory, "early_sent_parse_strip"),
                    ("add_message_ids", self._ids_splice_factory, "early_sent_ids_splice"),
                ):
                    if not hasattr(dcls, _attr):
                        continue
                    _orig = getattr(dcls, _attr)              # staticmethod 解包后的函数
                    _hh = PatchHandle(_name)
                    if install(_hh, dcls, _attr, _factory) is None:
                        continue
                    setattr(dcls, _attr, staticmethod(getattr(dcls, _attr)))
                    _hh.installed = getattr(dcls, _attr)      # 还原判据 = 我们装的函数
                    _hh.original = staticmethod(_orig)        # 还原成 staticmethod 语义
                    self.patches.add(_hh)
                    logger.info("[accel] 解析层已接管（%s：已抢发段在解析前剥离）", _attr)
            else:
                logger.warning("[accel] 未找到可接管的发送服务，抢发剥离未安装（可能重复发送）")

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
                    for mid, entry in llm.items():
                        # 展示名优先 3.0 的 model_name；2.x 条目非 dict 自动回退 mid。
                        # value 保持 pid:内部ID（3.0 规范引用值）不动。
                        mname = entry.get("model_name") if isinstance(entry, dict) else None
                        mname = mname or mid
                        out.append({"value": f"{pid}:{mid}",
                                    "label": f"{mname} ({pname})",
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
