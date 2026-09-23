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
        # ★ 记下"这一轮的响应"被发送层消费过没有 —— 用来发现"同一轮被发两次"
        self._sent_once: set[str] = set()
        self._proxy_cache: dict[tuple, LLMClientProxy] = {}
        self._stats = {
            "turns": 0, "steps": 0,
            "tool_signals": 0, "strip_calls": 0, "parallel_batches": 0, "content_normalized": 0,
            "streamed_calls": 0, "early_sent": 0, "first_seg_s": None,
            "thinking_on": 0, "thinking_off": 0, "thinking_skipped_budget": 0,
        }

    # ══════════════════════════════════════════════════════════
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

    async def terminate(self) -> None:
        self.patches.uninstall_all()
        self._proxy_cache.clear()
        logger.info("[accel] terminate，所有接管点已还原")

    # ══════════════════════════════════════════════════════════
    # L1 观测
    # ══════════════════════════════════════════════════════════
    @on.step_result(priority=Priority.SYS_LOW)
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

    @on.final_result(priority=Priority.SYS_LOW)
    async def observe_final(self, event: KiraMessageBatchEvent, final_result, *_):
        if not self.observe_enabled:
            return
        try:
            self._stats["turns"] += 1
            sid = getattr(event, "sid", "?")
            # 把"本轮有没有用工具"记为"上一轮"，供下一轮判定使用
            self.thinking.commit_turn(sid)
            d = self.thinking.last_decision(sid)
            # ★ 文案要让"这不是全局开关"看得明白：
            #   以前自动思考没开时也打「思考=关」⇒ 用户（尤其自己已在提供商
            #   那边开了思考的人）会以为插件把思考关掉了，看着吓人。
            #   现在：没启用的功能就说「未启用」，启用后不打光秃秃的「关」，
            #   而是把得分带上，明确这是**本轮判定结果**。
            if not self.thinking_enabled:
                think_txt = "未启用"
            elif d is None:
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
    @on.llm_request(priority=Priority.SYS_LOW)
    async def capture_and_optimize(self, event: KiraMessageBatchEvent, req: LLMRequest,
                                   tag_set=None, *_):
        """排在 SYS_LOW（实测无任何插件占用）⇒ 保证在所有插件之后收口，顺序确定。

        只做两件事（**刻意不做工具裁剪**，见 README「为什么不做工具裁剪」）：
          1. 记录本轮上下文（sid/event/tag_set），供抢先发送使用
          2. 自动思考判定（结果在 request 上打标记，由 _build_request_kwargs 注入）
        """
        sid_now = getattr(event, "sid", None)
        # 仅供面板展示"最近一次判定属于哪个会话"，功能路径一律用 req 上的 SendCtx
        self._current_sid = sid_now
        req.__dict__["_accel_ctx"] = SendCtx(sid_now, event, tag_set)

        if self.thinking_enabled and sid_now:
            decision = self.thinking.decide(sid_now, req)
            req.__dict__["_accel_thinking"] = decision
            if decision.enabled:
                self._stats["thinking_on"] += 1
            elif "超预算" in decision.signals:
                self._stats["thinking_skipped_budget"] += 1
            else:
                self._stats["thinking_off"] += 1

    @on.tool_result(priority=Priority.SYS_LOW)
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
                if allowed:
                    pname = getattr(getattr(client, "model", None), "provider_name", "") or ""
                    pid = getattr(getattr(client, "model", None), "provider_id", "") or ""
                    if pname not in allowed and pid not in allowed:
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
                self._sent_ledger.setdefault(ctx.sid, []).append(seg)
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

                # ★ 旁证：响应标记取不到时，用本轮台账兜底。
                #   没有这一层，`n=0` 会让**整份回复被重新发送**（全量重复）。
                from .early_sent import early_sent_segments, strip_early_sent_smart
                segs = early_sent_segments(resp) if resp is not None else []
                ledger = plugin._sent_ledger.get(sid_now, []) if sid_now else []
                if n <= 0 and ledger:
                    n = len(ledger)
                    segs = list(ledger)
                    logger.warning(
                        "[accel] 响应标记缺失，改用本轮台账剥离 %d 段（避免重复发送）",
                        n,
                    )
                elif n > 0 and not segs:
                    # 标记在但段原文不在（理论上不该发生）⇒ 用台账补上原文
                    segs = list(ledger)

                # ★ 结构性异常检测：框架正常只把一轮交给 send_xml_messages **一次**。
                #   真被调第二次时标记已消费，n=0 ⇒ 会把已发过的段**再发一遍**。
                #   这不该发生，但发生了必须留下明确线索（原来会静默重复）。
                if sid_now:
                    if sid_now in plugin._sent_once:
                        logger.error(
                            "[accel] 同一轮（%s）被再次交给发送层 —— "
                            "已抢发的内容可能被重复发送。请把这条日志反馈给插件作者。",
                            sid_now,
                        )
                    plugin._sent_once.add(sid_now)
                    if len(plugin._sent_once) > 64:
                        plugin._sent_once.clear()

                early = plugin._early_results.pop(sid_now, []) if sid_now else []
                if n > 0:
                    # ★ 用"按内容剥离"：count 是按**原始**文本数的，
                    #   而 xml_tag_fixer 这类插件可能已改写 text_response
                    #   （补 <msg> / 拆分消息块 / 合并块 / 转义实体），
                    #   按序号切会少切（重复）或多切（丢内容）。
                    xml_data = strip_early_sent_smart(xml_data, n, segs)
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
                    # 本轮已消费完，清掉标记，避免影响下一步/下一轮
                    if resp is not None:
                        resp.__dict__.pop("_accel_early_sent_count", None)
                    if sid_now:
                        plugin._resp_by_sid.pop(sid_now, None)
                        plugin._sent_ledger.pop(sid_now, None)
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
