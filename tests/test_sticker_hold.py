"""★ 回归：含表情的段不能被抢发（否则「表情独立成行」在 XML 改写前就定型了）。

## 线上事故（2026-11-14）

模型输出（同一个 <msg>，文字在前）：

    <msg>
    <text>这表情，欠爱了</text>
    <sticker>1</sticker>
    </msg>

- 抢发：这一段已闭合 ⇒ 立刻按**合并形态**发出去
  ⇒ QQ 把它渲染成一个气泡（文字套在表情气泡里）。
- qq-enhance 的表情控制随后在 ON_LLM_RESPONSE 里改写 XML，把它拆成
  「表情一条 + 正文一条」—— 但已发出的消息收不回来 ⇒ **表情独立成行失效**。
- 更糟的是 accel 的剥离按内容锚定：改写后文本里已经没有原始合并段，
  剥离 0 段 ⇒ 残留块又被链级对账"部分消费" ⇒ 两块拿到**同一个 message_id**，
  框架根本没再发一条独立表情。

## 判定规则

只拦**结构会变**的段（>=2 个 sticker，或 sticker + 其它内容）；
纯表情单条结构不变，继续抢发，不牺牲速度。
"""
import pathlib
import sys
import types

# ---- 最小 stub：stream_first 只在模块级 import llm_model 的类型 ----
_stub = types.ModuleType("core.provider.llm_model")


class _Request:
    pass


class _Response:
    pass


class _Chunk:
    pass


_stub.LLMRequest = _Request
_stub.LLMResponse = _Response
_stub.LLMStreamChunk = _Chunk
for _name in ("core", "core.provider"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        _m.__path__ = []
        sys.modules[_name] = _m
sys.modules["core.provider.llm_model"] = _stub

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parent))
_pkg = HERE.name
_sf = __import__(f"{_pkg}.stream_first",
                 fromlist=["SegmentEmitter", "sticker_restructure_needed", "sticker_control_active"])
SegmentEmitter = _sf.SegmentEmitter
needs = _sf.sticker_restructure_needed
sticker_control_active = _sf.sticker_control_active

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def feed_all(*chunks):
    e = SegmentEmitter()
    for c in chunks:
        e.feed(c)
    return e


def main():
    print("== 判定函数")
    check("纯文字段不拦", not needs("<msg><text>hi</text></msg>"))
    check("纯表情单条不拦", not needs("<msg><sticker>1</sticker></msg>"))
    check("带换行的纯表情不拦", not needs("<msg>\n<sticker>1</sticker>\n</msg>"))
    check("文字+表情要拦", needs("<msg><text>hi</text><sticker>1</sticker></msg>"))
    check("表情+文字要拦", needs("<msg><sticker>1</sticker><text>hi</text></msg>"))
    check("两个表情要拦", needs("<msg><sticker>1</sticker><sticker>2</sticker></msg>"))
    check("引用+表情要拦", needs('<msg><reply id="9"/><sticker>1</sticker></msg>'))
    check("空白 text 不算内容", not needs("<msg><text>   </text><sticker>1</sticker></msg>"))

    print("== 抢发行为")
    e = feed_all("<msg><sticker>1</sticker></msg>")
    check("纯表情仍然抢发", e.pending == ["<msg><sticker>1</sticker></msg>"], str(e.pending))
    check("纯表情 hold=False", e.hold is False)

    merged = "<msg>\n<text>这表情，欠爱了</text>\n<sticker>1</sticker>\n</msg>"
    e = feed_all(merged)
    check("合并段不抢发", e.pending == [], str(e.pending))
    check("合并段 hold=True", e.hold is True)
    check("合并段留在 buf 里交给框架", merged in e.remaining(), repr(e.remaining()))

    # 前缀段照常抢发，遇到合并段后停止（与 StreamEngine 一样：每次 feed 后 pop）
    e = SegmentEmitter()
    e.feed("<msg><text>第一句</text></msg>")
    first = e.pop_pending()
    check("前缀段先抢发", first == ["<msg><text>第一句</text></msg>"], str(first))
    e.feed(merged)
    e.feed("<msg><text>第三句</text></msg>")
    check("遇到合并段后不再抢发", e.pop_pending() == [], "仍有待发段")
    check("后续段也留在 buf（顺序不颠倒）",
          e.remaining().index("这表情") < e.remaining().index("第三句"), repr(e.remaining()))

    # 开关关闭 ⇒ 回到旧行为（合并段照发，速度快，但「表情独立成行」失效）
    e = SegmentEmitter(hold_sticker=False)
    e.feed(merged)
    check("hold_sticker=False 时照常抢发", e.pop_pending() == [merged], "未抢发")
    check("hold_sticker=False 时 hold 不置位", e.hold is False)

    # 同一段拆成多次 delta 也要能识别
    e = SegmentEmitter()
    for part in ["<msg><text>这表情", "，欠爱了</text>", "<sticker>1</sticker></msg>"]:
        e.feed(part)
    check("分片喂入也能识别", e.hold is True and e.pending == [], f"hold={e.hold} pending={e.pending}")

    print()
    print("== 表情控制检测")
    check("无 plugin_mgr ⇒ 未知(None)", sticker_control_active(None) is None)
    check("老框架没有 get_plugin_config ⇒ 未知", sticker_control_active(object()) is None)

    class PM:
        def __init__(self, cfg=None, installed=(), enabled=None):
            self.cfg = cfg or {}
            self.installed = set(installed)
            self.enabled = enabled or {}

        def get_plugin_config(self, pid):
            return dict(self.cfg.get(pid, {}))

        def has_plugin(self, pid):
            return pid in self.installed

        def is_plugin_enabled(self, pid):
            return self.enabled.get(pid, True)

    check("qq-enhance 开关=开 ⇒ True",
          sticker_control_active(PM({"kira-ai-plugin-qq-enhance": {"sticker_control_enabled": True}})) is True)
    check("qq-enhance 开关=关、没装独立版 ⇒ False",
          sticker_control_active(PM({"kira-ai-plugin-qq-enhance": {"sticker_control_enabled": False}})) is False)
    check("qq-enhance 没装 ⇒ False", sticker_control_active(PM()) is False)
    check("qq-enhance 装了但被禁用（开关还留着 true）⇒ False",
          sticker_control_active(PM({"kira-ai-plugin-qq-enhance": {"sticker_control_enabled": True}},
                                    installed={"kira-ai-plugin-qq-enhance"},
                                    enabled={"kira-ai-plugin-qq-enhance": False})) is False)
    check("独立版已装且启用 ⇒ True",
          sticker_control_active(PM(installed={"sticker_control"})) is True)
    check("独立版已装但被禁用 ⇒ False",
          sticker_control_active(PM(installed={"sticker_control"},
                                    enabled={"sticker_control": False})) is False)

    class Boom(PM):
        def get_plugin_config(self, pid):
            raise RuntimeError("config broken")

    check("检测抛异常 ⇒ 未知(按开启处理)", sticker_control_active(Boom()) is None)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
