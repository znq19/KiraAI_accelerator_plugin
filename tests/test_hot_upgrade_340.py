"""★★★ 热升级：旧实例的流式代理必须被新实例接管，不能让它继续抢先发送。

线上现象（更新插件后**每条回复发两遍**）：
  1. 插件安装器在运行中的进程里热替换 `data/plugins/kira_accelerator`；
  2. 旧实例 terminate 时，`ProviderManager.get_model_client` 已被**别的包装**覆盖
     ⇒ 日志「接管点 stream_engine 已被他人覆盖，跳过还原」；
  3. 旧实例的 `LLMClientProxy` 留在链上，`_engine_factory` 仍绑定**旧插件对象**；
  4. 新实例的 `parse_xml` 台账是空的，旧引擎却抢先发出了一段
     ⇒ 新实例剥不到它 ⇒ 框架再发一次 ⇒ 用户看到两条一样的气泡。

本测试构造"旧代理 + 新实例"的最小现场：新实例必须把旧代理的引擎换成自己的，
并且旧引擎不能再被调用；随后按真实 `parse_xml` → `send_message_chain` 数投递次数。
"""
import asyncio
import os
import sys
import types
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)
if _env.gen() != "3":
    _env.skip("热升级链路是 3.x 形态")

from core.provider.provider_manager import ProviderManager                 # noqa: E402
from core.provider.llm_model import LLMStreamChunk                        # noqa: E402
from core.workflow.src.im.message_delivery import MessageDeliveryService  # noqa: E402
from core.chat.message_utils import KiraIMSentResult, MessageChain        # noqa: E402
from core.chat.message_elements import Text                               # noqa: E402
from core.tag.base import BaseTag                                         # noqa: E402
from core.tag.tag_registry import TagSet                                  # noqa: E402

main_mod = _env.load("main")
se = _env.load("stream_engine")
PASS, FAIL = [], []

def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))

class TextTag(BaseTag):
    name = "text"

    async def handle(self, value, **kwargs):
        return [Text(value)]

class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.info = types.SimpleNamespace(platform="test")

    def get_capability(self, cap):
        return self

    async def send_group_message(self, pid, chain):
        self.sent.append("".join(getattr(e, "text", "") for e in chain.message_list))
        return KiraIMSentResult(message_id="M%d" % len(self.sent))

    async def send_direct_message(self, pid, chain):
        return await self.send_group_message(pid, chain)

class FakeAdapters:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_adapter(self, name):
        return self.adapter

class FakeConfig:
    def get_config(self, key, default=None):
        return default

class Delivery(MessageDeliveryService):
    def __init__(self, adapter):
        self.kira_config = FakeConfig()
        self.adapter_mgr = FakeAdapters(adapter)
        self.persona_mgr = None
        self.message_history = None
        self.session_locks = {}

class Ev:
    def __init__(self):
        self.sid = "bot:gm:1"
        self.event_id = "ev-upgrade"
        self.is_stopped = False
        self.bot = None

class FakeLLM:
    type = "llm"

    def __init__(self):
        self.model = types.SimpleNamespace(
            provider_id="p", model_id="m", provider_name="P",
            provider_config={}, model_config={})

    async def chat(self, request, **kwargs):
        return None

    def chat_stream(self, request, **kwargs):
        async def gen():
            yield LLMStreamChunk(delta_text="<msg><text>hi</text></msg>", is_final=True)
        return gen()

def ctx_of(delivery):
    async def send(session, chain, **kwargs):
        return await delivery.send_message_chain(session, chain, **kwargs)
    return types.SimpleNamespace(
        message_delivery=delivery,
        message_processor=types.SimpleNamespace(min_message_delay=0.0, max_message_delay=0.0),
        send_message_chain=send, provider_mgr=types.SimpleNamespace(),
        adapter_mgr=None, config={}, event_bus=None, persona_mgr=None,
        sticker_mgr=None, tool_mgr=None, session_mgr=None, prompt_mgr=None,
        db=None, plugin_mgr=None, get_plugin_data_dir=lambda: "/tmp/itest/data/plugin")

async def main():
    print("#" * 70)
    print("# 热升级：旧流式代理被新实例接管（不再重复发送）")
    print("#" * 70)
    adapter = FakeAdapter()
    delivery = Delivery(adapter)
    ctx = ctx_of(delivery)
    old_emit_calls = []

    async def old_emit(seg):
        old_emit_calls.append(seg)
        return True

    fake_llm = FakeLLM()
    old_proxy = se.LLMClientProxy(
        fake_llm,
        lambda _req: se.StreamEngine(force_stream=True, emit=old_emit),
    )
    # 模拟"旧实例留下的代理"：没有归属者（1.0.98 的代理没有 _accel_owner 槽）

    original_build = ProviderManager.get_model_client

    def fake_build(self, *args, **kwargs):
        return old_proxy

    ProviderManager.get_model_client = fake_build
    plugin = main_mod.AcceleratorPlugin(ctx=ctx, cfg={"section_stream": {"early_send": True}})
    plugin._proxy_cache = {}
    plugin.patches.uninstall_all()
    try:
        plugin._install_stream_engine()
        plugin._install_early_sent_strip()
        client = ProviderManager.get_model_client(types.SimpleNamespace(), "p", "m")
        owner = getattr(getattr(client, "_engine_factory", None), "__self__", None)
        check("旧代理被新实例接管（引擎换成新实例）", owner is plugin,
              f"owner={type(owner).__name__ if owner is not None else None}")
        check("旧代理对象本身被复用（event.model_group 里的也能生效）", client is old_proxy)

        ev = Ev()
        tag_set = TagSet()
        tag_set.register(TextTag)
        sctx = main_mod.SendCtx(ev.sid, ev, tag_set)
        req = types.SimpleNamespace()
        req.__dict__["_accel_ctx"] = sctx
        await client.chat(req)
        check("旧引擎没有被调用（不会抢先发）", not old_emit_calls, str(old_emit_calls))

        actions = await delivery.parse_xml("<msg><text>hi</text></msg>", tag_set)
        for action in actions:
            if isinstance(action, MessageChain) and not action.is_empty():
                await delivery.send_message_chain(ev.sid, action)
        check("只投递一次（新实例抢发 + 解析剥离）", len(adapter.sent) == 1,
              f"sent={adapter.sent}")
    finally:
        try:
            plugin.patches.uninstall_all()
        except Exception:  # noqa: BLE001
            pass
        ProviderManager.get_model_client = original_build

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
        sys.exit(1)
    print("🎉 全部通过 —— 热升级后旧引擎不会再把同一条消息发两遍")

asyncio.run(main())
