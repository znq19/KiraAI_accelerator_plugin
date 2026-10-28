"""3.x 当前响应定位 + 会话级 inflight 防重复回归。

线上形状：同一文本的旧响应缓存仍在，且另一个会话正在抢发。旧实现会：
  1. `_turn_for_text` 先命中旧条目（台账为空）⇒ 当前轮不剥离；
  2. 实例级 `_early_inflight` 让本会话链级对账也跳过；
  ⇒ 框架把当前回复再发一次。
本套件锁住三项修复：ContextVar 当前响应、文本最新轮次择优、按会话 inflight。
"""
import asyncio
import pathlib
import sys
import time
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 3.x 框架源码（设 KIRA_FW=/path/to/KiraAI-3.x）")
sys.path.insert(0, FW)
from core.chat import MessageChain  # noqa: E402
from core.chat.message_elements import Text  # noqa: E402
from core.chat.message_utils import KiraIMSentResult  # noqa: E402
from core.message_manager import MessageProcessor  # noqa: E402

if hasattr(MessageProcessor, "send_xml_messages"):
    _env.skip("本套件专测 3.x parse_xml/send_message_chain 路径")

main_mod = _env.load("main")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


SID = "qq:gm:188395693"
B1 = '<msg><reply>335594849</reply><text>省的那点 权重那儿全还回去（</text></msg>'
C = '<msg><text>后续新内容</text></msg>'
FULL = B1 + '\n' + C
CURRENT = [None]


class Delivery:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.parsed = []
        self.forwarded = []
        CURRENT[0] = self

    @staticmethod
    async def parse_xml(xml_data, tag_set):
        CURRENT[0].parsed.append(xml_data)
        return [('CHAIN', xml_data)] if xml_data.strip() else []

    @staticmethod
    def add_message_ids(xml_data, results):
        return xml_data, list(results)

    async def send_message_chain(self, sid, chain, **kwargs):
        self.forwarded.append(chain)
        return KiraIMSentResult(message_id='FORWARDED', ok=True)


class Ctx:
    def __init__(self, delivery):
        self.message_delivery = delivery
        self.message_processor = None
        self.adapter_mgr = None


async def main():
    d = Delivery()
    plugin = main_mod.AcceleratorPlugin(Ctx(d), {})
    plugin._install_early_sent_strip()
    try:
        # 1) 当前任务响应精确命中，旧同文条目先插入也不影响。
        current = types.SimpleNamespace(text_response=FULL)
        key = plugin._ckey(SID, 'current')
        plugin._resp_by_sid['stale'] = types.SimpleNamespace(text_response=FULL)
        plugin._resp_by_sid[key] = current
        plugin._sent_ledger[key] = [B1]
        plugin._early_results[key] = [KiraIMSentResult(message_id='E1', ok=True)]
        await plugin._remember_active_response(None, current)
        d.parsed.clear()
        await d.parse_xml(FULL, None)
        check('ContextVar 当前响应优先于旧同文条目',
              d.parsed and B1 not in d.parsed[-1] and C in d.parsed[-1], d.parsed[-1])

        # 2) 没有 ContextVar 时，同文按最新轮次选，不选空台账旧条目。
        await plugin._remember_active_response(None, types.SimpleNamespace(text_response='other'))
        plugin._resp_by_sid.pop(key, None)
        fresh = types.SimpleNamespace(text_response=FULL)
        key2 = plugin._ckey(SID, 'fresh')
        plugin._resp_by_sid['stale2'] = types.SimpleNamespace(text_response=FULL)
        plugin._resp_by_sid[key2] = fresh
        plugin._sent_ledger[key2] = [B1]
        plugin._early_results[key2] = [KiraIMSentResult(message_id='E2', ok=True)]
        plugin._ledger_ts['stale2'] = 0
        plugin._ledger_ts[key2] = time.time()
        d.parsed.clear()
        await d.parse_xml(FULL, None)
        check('无活动响应时同文按最新轮次择优',
              d.parsed and B1 not in d.parsed[-1] and C in d.parsed[-1], d.parsed[-1])

        # 3) 另一个会话抢发不阻断本会话链级对账。
        plugin._sent_ledger.clear()
        plugin._early_results.clear()
        plugin._chain_records.clear()
        key3 = plugin._ckey(SID, 'chain')
        plugin._sent_ledger[key3] = [B1]
        plugin._early_results[key3] = [KiraIMSentResult(message_id='E3', ok=True)]
        plugin._early_inflight_sids['other:gm:1'] = 1
        d.forwarded.clear()
        result = await d.send_message_chain(SID, MessageChain([Text('省的那点 权重那儿全还回去（')]))
        check('跨会话 inflight 不影响本会话链级对账',
              result.message_id == 'E3' and not d.forwarded, (result.message_id, d.forwarded))

        # 4) 本会话自己抢发中仍要跳过对账，避免吃掉尚未完成的自投递。
        key4 = plugin._ckey(SID, 'self')
        plugin._sent_ledger[key4] = [B1]
        plugin._early_results[key4] = [KiraIMSentResult(message_id='E4', ok=True)]
        plugin._early_inflight_sids[SID] = 1
        d.forwarded.clear()
        result2 = await d.send_message_chain(SID, MessageChain([Text('省的那点 权重那儿全还回去（')]))
        check('本会话 inflight 仍跳过链级对账',
              result2.message_id == 'FORWARDED' and len(d.forwarded) == 1,
              (result2.message_id, len(d.forwarded)))
    finally:
        plugin._early_inflight_sids.clear()
        plugin.patches.uninstall_all()

    print(f'\n通过 {len(PASS)}，失败 {len(FAIL)}')
    if FAIL:
        print('FAILED:', FAIL)
        sys.exit(1)
    print('TURN CONTEXT + SESSION INFLIGHT TEST PASSED')


asyncio.run(main())
