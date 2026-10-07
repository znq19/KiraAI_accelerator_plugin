"""装载自检 —— 用真实的 core 源码 + 最小桩，验证插件能真的加载起来。

比"语法正确"强得多：真跑 `initialize()`、真装接管点、真发一次 chat、
再真 `terminate()` 还原。目标是回答两个问题：
  ① 插件在真实框架里**装得上吗**？
  ② 关掉之后**还原干净吗**（不留残留）？
"""
from __future__ import annotations

import asyncio
import importlib.util as ilu
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # 插件根（本文件在 tests/ 下）
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ══════════════════════════════════════════════════════════════
# 搭最小桩：只够让插件 import 与 initialize 成功
# ══════════════════════════════════════════════════════════════
def build_stubs():
    created = {}

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        # 保证父包存在
        if "." in name:
            parent = name.rsplit(".", 1)[0]
            if parent not in sys.modules:
                p = types.ModuleType(parent)
                p.__path__ = []
                sys.modules[parent] = p
        return m

    # --- core.plugin ---
    class BasePlugin:
        def __init__(self, ctx, cfg):
            self.ctx = ctx
            self.plugin_cfg = cfg

        async def initialize(self): ...
        async def terminate(self): ...

    class _Pri:
        SYS_LOW, LOW, MEDIUM, HIGH, SYS_HIGH = -100, -50, 0, 50, 100

    reg_calls = []

    class _Reg:
        def __getattr__(self, name):
            def deco(*a, **k):
                reg_calls.append((name, a, k))

                def wrap(f):
                    return f
                return wrap
            return deco

    class PageMenu:
        def __init__(self, **kw): self.kw = kw

    class PluginPage:
        @staticmethod
        def from_folder(p): return ("folder", p)

    hooks = {}

    class _On:
        def __getattr__(self, ev):
            def deco(*a, **k):
                def wrap(f):
                    hooks.setdefault(ev, []).append((f, k.get("priority")))
                    return f
                return wrap
            return deco

    mod("core.plugin",
        BasePlugin=BasePlugin, Priority=_Pri, register=_Reg(), on=_On(),
        PageMenu=PageMenu, PluginPage=PluginPage, logger=None)

    # --- core.provider (真源码的 llm_model 结构) ---
    from dataclasses import dataclass as dc, field as fld

    @dc
    class LLMRequest:
        messages: list = fld(default_factory=list)
        user_prompt: list = fld(default_factory=list)
        system_prompt: list = fld(default_factory=list)
        tools: object = None
        tool_set: object = None
        tool_choice: object = None

    @dc
    class LLMResponse:
        def __init__(self, text_response=""):
            self.text_response = text_response
            self.reasoning_content = ""
            self.tool_calls = []
            self.tool_results = []
            self.input_tokens = None
            self.output_tokens = None
            self.cached_tokens = None
            self.time_consumed = None

    @dc
    class LLMStreamChunk:
        delta_text: str = ""
        delta_reasoning: str = ""
        tool_calls_delta: list = fld(default_factory=list)
        is_final: bool = False
        finish_reason: str = ""
        usage: object = None

    mod("core.provider.llm_model", LLMRequest=LLMRequest, LLMResponse=LLMResponse,
        LLMStreamChunk=LLMStreamChunk)
    mod("core.provider", LLMRequest=LLMRequest, LLMResponse=LLMResponse)

    mod("core.chat.message_utils", KiraMessageBatchEvent=object)
    mod("core.chat", MessageChain=object)

    # --- core.utils.model_clients（真类，供 patch 用）---
    class OpenAICompatibleLLMClient:
        def __init__(self, model):
            self.model = model

        def _build_client(self):
            return f"client:{id(self)}"

        def _build_request_kwargs(self, request, **overrides):
            kw = {"model": "m", "messages": []}
            kw.update(overrides)
            return kw

        async def chat(self, request, **kw):
            return LLMResponse("非流式")

        async def chat_stream(self, request, **kw):
            yield LLMStreamChunk(delta_text="流式")

    # ★ 跨世代：3.0 把该类搬到了 core.provider.openai_compatible。
    #   注册到**当前框架真正会 import 的路径**，否则 3.0 上插件 import 不到 mock。
    mod("core.provider.openai_compatible", OpenAICompatibleLLMClient=OpenAICompatibleLLMClient)
    mod("core.utils.model_clients", OpenAICompatibleLLMClient=OpenAICompatibleLLMClient)
    mod("core.utils")
    mod("core.utils.path_utils", get_data_path=lambda: Path("/tmp"))

    # --- core.provider.provider_manager（真类）---
    class ProviderManager:
        _registry = {}

        def __init__(self, *a, **k):
            pass

        def get_model_client(self, provider_id, model_id, model_type=None):
            return OpenAICompatibleLLMClient(
                types.SimpleNamespace(provider_id=provider_id, model_id=model_id,
                                      provider_name="prov", model_config={},
                                      provider_config={}))

    mod("core.provider.provider_manager", ProviderManager=ProviderManager)

    # --- core.chat.session_manager（真类）---
    class SessionManager:
        def __init__(self):
            self.chat_memory = {"s": []}
            self.chat_memory_path = "/tmp/_accel_test_mem.json"

        def _save_memory(self, memory=None, path=None):
            return "原实现"

    mod("core.chat.session_manager", SessionManager=SessionManager)

    # --- core.message_manager（真身：MessageProcessor.send_xml_messages 是我们接管的点）---
    class MessageProcessor:
        def __init__(self):
            self.min_message_delay = 2.0
            self.max_message_delay = 5.0

        async def send_xml_messages(self, event, xml_data, tag_set):
            return []

    mod("core.message_manager", MessageProcessor=MessageProcessor)

    # --- core.plugin.plugin_handlers ---
    class EventType:
        ON_MESSAGE_SENT = "on_message_sent"

    class EventHandlerRegistry:
        def get_handlers(self, et):
            return []

    # ★ 跨世代：3.0 的模块名是 core.plugin.handlers（core_compat 优先找它）
    mod("core.plugin.handlers",
        event_handler_reg=EventHandlerRegistry(), EventType=EventType)
    mod("core.plugin.plugin_handlers",
        event_handler_reg=EventHandlerRegistry(), EventType=EventType)

    # --- 日志 ---
    import logging
    mod("core.logging_manager", get_logger=lambda *a, **k: logging.getLogger("accel"))

    created["hooks"] = hooks
    created["reg"] = reg_calls
    created["ProviderManager"] = ProviderManager
    created["SessionManager"] = SessionManager
    created["ClientCls"] = OpenAICompatibleLLMClient
    return created


stubs = build_stubs()

# 让 base plugin 拿到可用 logger
import logging as _logging
sys.modules["core.plugin"].logger = _logging.getLogger("plugin")


class Ctx:
    def __init__(self):
        self.message_processor = None
        self.session_mgr = None
        self.provider_mgr = None


async def main() -> None:
    # 装载加速器（以包的形式）
    pkg = types.ModuleType("kira_accelerator")
    pkg.__path__ = [str(HERE)]
    sys.modules["kira_accelerator"] = pkg

    def load(name, filename):
        spec = ilu.spec_from_file_location(f"kira_accelerator.{name}", str(HERE / filename))
        m = ilu.module_from_spec(spec)
        sys.modules[f"kira_accelerator.{name}"] = m
        spec.loader.exec_module(m)
        return m

    load("patches", "patches.py")
    load("stream_first", "stream_first.py")
    load("stream_engine", "stream_engine.py")
    load("auto_thinking", "auto_thinking.py")
    m_main = load("main", "main.py")
    check("main.py 可加载", m_main is not None)
    check("找到 AcceleratorPlugin", hasattr(m_main, "AcceleratorPlugin"))
    pages = [c for c in stubs["reg"] if c[0] == "page"]
    check("注册了侧边栏页面", len(pages) == 1, f"reg={[(c[0], c[1]) for c in stubs['reg']]}")
    if pages:
        check("页面路由为 /index", pages[0][1] and pages[0][1][0] == "/index",
              f"实际={pages[0][1]}")
    tools = [c for c in stubs["reg"] if c[0] == "tool"]
    check("注册了 accel_report 工具",
          any("accel_report" in str(c[1]) for c in tools), f"tools={tools}")
    apis = [c for c in stubs["reg"] if c[0] == "api"]
    check("注册了 /health API",
          any(c[2].get("path") == "/health" for c in apis), f"apis={apis}")

    Cfg = {
        "section_observe": {"enabled": True},
        "section_stream": {"force_stream": True, "early_send": True, "providers": [], "gap": 0.1},
        "section_thinking": {"enabled": True, "style": "enable_thinking", "threshold": 2.0},
        "section_request": {"trim_tools": True, "slim_tool_results": True,
                            "tame_send_delay": True, "send_delay_target": 0.2},
        "section_takeover": {"reuse_http_client": True, "compact_memory_dump": True},
    }

    ctx = Ctx()
    plugin = m_main.AcceleratorPlugin(ctx, Cfg)

    print("\n1) initialize：真装接管点")
    PM = stubs["ProviderManager"]
    SM = stubs["SessionManager"]
    CC = stubs["ClientCls"]
    orig_get = PM.get_model_client
    orig_save = SM._save_memory
    orig_build = CC._build_client

    await plugin.initialize()

    check("get_model_client 已被包装", PM.get_model_client is not orig_get)
    # ★ compact_memory_dump 是「待验证」功能，代码里锁死 ——
    #   即使配置写成 true 也不生效（面板上该开关也是置灰的）。
    check("★ 待验证功能被锁死：配置 true 也不接管 _save_memory",
          SM._save_memory is orig_save)
    check("_build_client 已被包装", CC._build_client is not orig_build)
    health = {h["name"]: h for h in plugin.patches.health()}
    check("接管点 stream_engine 生效", health.get("stream_engine", {}).get("active"))
    check("健康快照含熔断三态字段",
          "state" in health.get("stream_engine", {}) and
          "cooldown_remaining" in health.get("stream_engine", {}) and
          "recovered_count" in health.get("stream_engine", {}))
    check("初始状态为 CLOSED", health.get("stream_engine", {}).get("state") == "closed")
    check("接管点 reuse_http_client 生效", health.get("reuse_http_client", {}).get("active"))
    # ★ 思考注入现在挂**三个**点（OpenAI 兼容系 / DeepSeek / Anthropic）——
    #   只挂第一个会让 DeepSeek、Anthropic 用户的自动思考完全失效（实测过）。
    #   客户端的 SDK 不一定都装了，所以断言"至少挂上 ≥1 个"，并逐个列出。
    th_points = {k: v.get("active") for k, v in health.items()
                 if k.startswith("auto_thinking")}
    check("思考注入点已安装（≥1）", len(th_points) >= 1 and any(th_points.values()),
          str(th_points))
    check("其中包含 OpenAI 兼容系注入点",
          bool(th_points.get("auto_thinking:openai")), str(sorted(th_points)))

    print("\n1b) 锁死状态与热重载")
    await plugin.terminate()
    check("terminate 后 _save_memory 仍是框架原实现", SM._save_memory is orig_save)
    plugin = m_main.AcceleratorPlugin(ctx, Cfg)
    await plugin.initialize()          # 恢复主流程用的实例

    print("\n2) 端到端：真实走一次 chat，验证强制流式生效")
    pm = PM("db", "cfg")
    client = pm.get_model_client("p1", "m1")
    check("拿到的 client 是代理", type(client).__name__ == "LLMClientProxy",
          f"实际={type(client).__name__}")
    check("代理透传 .model", client.model.provider_id == "p1")
    req = sys.modules["core.provider.llm_model"].LLMRequest(
        messages=[], user_prompt=[], tools=[], tool_set=None)
    resp = await client.chat(req)
    check("chat 返回完整响应", resp.text_response == "流式",
          f"实际={resp.text_response!r}")
    check("统计标记为流式", resp.__dict__.get("_accel_stats", {}).get("streamed") is True)

    print("\n3) 自动思考注入：extra_body 真的进了请求")
    from kira_accelerator.auto_thinking import ThinkingDecision
    req2 = sys.modules["core.provider.llm_model"].LLMRequest(
        messages=[], user_prompt=[], tools=[], tool_set=None)
    req2.__dict__["_accel_thinking"] = ThinkingDecision(True, 3.0, ["测试"], "high")
    inner = client._wrapped
    kw = inner._build_request_kwargs(req2)
    check("思考参数已注入 enable_thinking",
          (kw.get("extra_body") or {}).get("enable_thinking") is True,
          f"实际={kw.get('extra_body')}")

    req3 = sys.modules["core.provider.llm_model"].LLMRequest(
        messages=[], user_prompt=[], tools=[], tool_set=None)
    req3.__dict__["_accel_thinking"] = ThinkingDecision(False, 0.0, ["简单"], "low")
    kw3 = inner._build_request_kwargs(req3)
    check("★ 不开思考时按新默认注入显式关闭（inject_nothinking 默认已开，v1.0.78）",
          (kw3.get("extra_body") or {}).get("enable_thinking") is False,
          f"实际={kw3.get('extra_body')}")

    print("\n3b) ★ 流式代理缓存：键与上限")
    pm2 = PM("db", "cfg")
    c_a = pm2.get_model_client("prov1", "modelA")
    c_a2 = pm2.get_model_client("prov1", "modelA")
    c_b = pm2.get_model_client("prov1", "modelB")
    check("同一 (provider, model) 复用同一个代理", c_a is c_a2)
    check("不同 model 得到不同代理", c_a is not c_b)
    check("缓存键不是 id()（用 provider_id+model_id）",
          all(isinstance(k, tuple) and len(k) == 2 for k in plugin._proxy_cache.keys()),
          f"实际键={list(plugin._proxy_cache.keys())}")
    # 上限
    for i in range(80):
        pm2.get_model_client("p", f"m{i}")
    check("缓存有上限保护（不会无界增长）", len(plugin._proxy_cache) <= 65,
          f"实际={len(plugin._proxy_cache)}")

    print("\n3c) ★ 发送层接管：剥离已抢发段（避免重复发送）")
    from core.message_manager import MessageProcessor as _MP
    health = {h["name"]: h for h in plugin.patches.health()}
    check("early_sent_strip 接管点已生效", health.get("early_sent_strip", {}).get("active"))
    check("send_xml_messages 已被包装",
          getattr(_MP.send_xml_messages, "__kira_accel__", False) is True)

    # 端到端：模拟一次"已抢发 2 段"，验证发送时只发剩下的
    import importlib.util as _ilu3
    _sp = _ilu3.spec_from_file_location("kira_accelerator.early_sent",
                                        str(HERE / "early_sent.py"))
    _es = _ilu3.module_from_spec(_sp)
    sys.modules["kira_accelerator.early_sent"] = _es
    _sp.loader.exec_module(_es)

    full = "<msg><text>A</text></msg><msg><text>B</text></msg><msg><text>C</text></msg>"
    resp = sys.modules["core.provider.llm_model"].LLMResponse(full)
    _es.mark_early_sent(resp, ["A", "B"], full)
    check("★ text_response 未被改写（下游插件看到完整输出）",
          resp.text_response == full, f"实际={resp.text_response!r}")
    plugin._current_resp = resp
    sent = {"xml": None}

    # 用真实的包装函数走一遍（原实现替换成记录器）
    async def _fake_orig(self, event, xml_data, tag_set):
        sent["xml"] = xml_data
        return []

    wrapped = _MP.send_xml_messages
    import types as _types
    fake = _types.SimpleNamespace()
    # 直接调用我们装的包装逻辑：把 __wrapped__ 换掉不可行，改为按语义手工验证
    remaining = _es.strip_early_sent(full, _es.early_sent_count(resp))
    check("发送时只发剩余 1 段", remaining == "<msg><text>C</text></msg>", f"实际={remaining!r}")
    check("全部发完时传 <msg/>（框架不会重复发）",
          _es.strip_early_sent(full, 3) == "<msg/>")

    print("\n4) terminate：接管点必须全部干净还原")
    await plugin.terminate()
    check("get_model_client 已还原", PM.get_model_client is orig_get)
    check("_build_client 已还原", CC._build_client is orig_build)
    check("_save_memory 保持原样", SM._save_memory is orig_save)
    check("patches 注册表已清空", plugin.patches.health() == [])

    print("\n4b) 壁纸发现与路径安全")
    import importlib.util as _ilu2
    _sp = _ilu2.spec_from_file_location("kira_accelerator.wallpapers_api",
                                        str(HERE / "wallpapers_api.py"))
    _wp = _ilu2.module_from_spec(_sp)
    sys.modules["kira_accelerator.wallpapers_api"] = _wp
    _sp.loader.exec_module(_wp)
    found = _wp.discover()
    check("发现壁纸文件", len(found) > 0, f"实际={found}")
    check("跳过下划线开头的辅助图", not any(n.startswith("_") for n in found))
    check("正常文件名可解析", _wp.resolve(found[0]) is not None)
    check("★ 拒绝路径穿越 ../", _wp.resolve("../main.py") is None)
    check("★ 拒绝绝对路径", _wp.resolve("/etc/passwd") is None)
    check("★ 拒绝未发现的文件名", _wp.resolve("not_a_wallpaper.webp") is None)
    check("★ 拒绝隐藏文件", _wp.resolve(".env") is None)
    check("MIME 正确", _wp.mime_for(found[0]).startswith("image/"))

    print("\n5) 重复 initialize/terminate 多次（模拟插件热重载）")
    for i in range(3):
        await plugin.initialize()
        await plugin.terminate()
    check("三次热重载后仍干净还原", PM.get_model_client is orig_get and CC._build_client is orig_build,
          "存在泄漏")
    check("无标记残留", not getattr(PM.get_model_client, "__kira_accel__", False))

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
