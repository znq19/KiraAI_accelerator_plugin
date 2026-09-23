"""兼容性修复：给下游保留「模型原始输出」，同时不破坏框架的"不重复发送"。

## 问题

抢先发送后，我们已经把前面几段发给了用户。为了让框架不要重复发送，
`resp.text_response` 被替换成了"还没发出去的那部分"。

但这会**误导下游**——实测有两处依赖 `resp.text_response` 是**模型的完整输出**：

1. **框架内置 `kira-ai` 插件**（`builtin_plugins/kira-ai/main.py:111`）：
   在 `ON_LLM_RESPONSE` 里对整个回复做 XML 校验，解析失败时用另一个模型去修。
   只拿到尾巴 ⇒ `<root>` 包不住 ⇒ 误判成解析失败 ⇒ **多花一次 LLM 调用去"修"一个本来没坏的东西**。

2. **sustained-chat 插件**（`main.py:1681`）：
   `ai_text = (resp.text_response or "").strip()` 用来判断 AI 是否发了内容（决定是否继续维持对话窗口）。
   只看到尾巴 ⇒ 可能**误判为"AI 没说话"**。

## 修法

把"给框架发送用的文本"与"给下游看的完整文本"**分开**：

- `resp.text_response`  ← **模型的完整输出**（下游看到的就是它）
- `resp.__dict__` 上的私有标记 ← 告诉**我们自己的发送层**："前 N 段已经发出去了"

真正的剥离发生在**发送时**（`_strip_early_sent`），而不是在构造响应时。
这样：
  - 下游插件的语义**完全不变**（拿到的就是完整输出）
  - 框架仍然**不会重复发送**已发出的段

### ⚠️ 标记绝不能放进 `resp.tool_results`（我踩过的坑）

第一版我把标记 append 进了 `resp.tool_results`，理由是"框架对没有 tool_calls 的响应
不会读它"。**这个推理是错的**：模型完全可能"先说一句，再决定调工具"——同一个响应里
`text_response` 有已抢发的段、`tool_calls` 又非空。这时 `agent_executor` 会走
tool_calls 分支并执行：

    tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError` ⇒ **整个回合崩掉**。

所以标记只放 `resp.__dict__`——框架从不遍历它，绝无副作用。
"""
from __future__ import annotations

# 历史遗留：曾经的标记键名。**已废弃** —— 标记现在只放 resp.__dict__，
# 放 tool_results 会让框架的 OpenAIMessage(**r) 崩（见 mark_early_sent 的说明）。
MARKER_KEY = "_accel_early_sent"

# 正则：匹配一个**已闭合**的 <msg> 段（与 stream_first 保持一致）
import re  # noqa: E402
from xml.sax.saxutils import unescape  # noqa: E402

_MSG_CLOSED = re.compile(r"<msg(?:\s[^>]*)?>.*?</msg>|<msg(?:\s[^>]*)?/>", re.DOTALL)


def _norm_seg(s: str) -> str:
    """把一段内容规范化成"可比对的纯文本"。

    · 去掉所有标签（只留文字）
    · **反转义 XML 实体** —— xml_tag_fixer 会先转义再解析，
      已发段里的 `&` 在改写后可能变成 `&amp;`，不反转义就匹配不上
    · 去掉所有空白 —— 对重新换行/重新包裹也稳健
    """
    if not s:
        return ""
    txt = re.sub(r"<[^>]*>", "", s)
    txt = unescape(txt)
    return re.sub(r"\s+", "", txt)


def _top_level_msgs(text: str) -> list[tuple[int, int]]:
    """列出文本里所有 `<msg>` 块的 (start, end)。"""
    out = []
    for m in _MSG_CLOSED.finditer(text or ""):
        out.append((m.start(), m.end()))
    return out


def strip_by_content(text: str, segments: list[str]) -> str | None:
    """按**内容**剥掉已发出的段。成功返回剩余文本；无法匹配返回 None。

    判据：从头往下逐块累加规范化文本，只要"累加结果仍是已发内容的前缀"就吃掉这一块。
    这样对"一条被拆成多条"和"多条被并成一条"都成立。
    """
    if not text or not segments:
        return None
    emitted_full = "".join(_norm_seg(s) for s in segments)
    blocks = _top_level_msgs(text)
    if not blocks:
        return None

    acc = ""
    consumed_end = 0
    consumed = 0
    for (s, e) in blocks:
        piece = _norm_seg(text[s:e])
        if piece == "":
            # 没有文字的块（<msg/>、只含媒体）：既然我们发过同等数量的空段，
            # 就一并吃掉；否则停下来。
            if consumed < len(segments):
                consumed += 1
                consumed_end = e
                continue
            break
        cand = acc + piece
        if cand and emitted_full.startswith(cand):
            acc = cand
            consumed += 1
            consumed_end = e
        else:
            break

    if consumed_end == 0:
        return None
    rest = text[consumed_end:]
    return rest if rest.strip() else "<msg/>"


def mark_early_sent(resp, segments: list[str], full_text: str) -> None:
    """在响应上记录"这些段已经抢先发出去了"。

    ★★ 只写**私有属性**，绝不碰 `resp.tool_results`！

    我第一版把标记塞进了 `tool_results`，注释还写着"框架对没有 tool_calls 的响应
    不会读它"——**这个推理是错的**：模型完全可能"先说一句，再决定调工具"，
    于是同一个响应里 `text_response` 有已抢发的段、`tool_calls` 又非空。
    这时 `agent_executor` 会走 tool_calls 分支并执行：

        tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

    我的标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError`
    ⇒ **整个回合崩掉**。

    所以标记只放 `resp.__dict__`：框架从不遍历它，绝无副作用。
    """
    if not segments:
        return
    try:
        resp.__dict__["_accel_full_text"] = full_text or ""
        resp.__dict__["_accel_early_sent_count"] = len(segments)
        # ★ 同时记下**已发段的原文**：剥离改为按内容匹配（见 strip_by_content），
        #   这样即使别的插件改写了 text_response（补标签/拆分/合并），也仍然能正确剥离。
        resp.__dict__["_accel_early_sent_segments"] = list(segments)
    except Exception:  # noqa: BLE001
        pass


def early_sent_count(resp) -> int:
    """读取"已经抢先发出了几段"。"""
    try:
        return int(resp.__dict__.get("_accel_early_sent_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def strip_early_sent_smart(text: str, count: int, segments: list | None = None) -> str:
    """剥离的**推荐入口**：优先按内容匹配，匹配不上才退回按序号。

    为什么要按内容：`count` 是流式期间按**原始**文本数出来的，
    而别的插件可能在发送前改写 `text_response`（补 `<msg>`、拆分消息块、
    合并块、转义实体）—— 那时 count 就对不上了：
      · count 偏小 ⇒ 少切 ⇒ 重复发送
      · count 偏大 ⇒ 多切 ⇒ 丢内容
    按内容匹配对这两种改写都成立。
    """
    if count <= 0 or not text:
        return text
    if segments:
        try:
            got = strip_by_content(text, segments)
            if got is not None:
                return got
            # 内容对不上：退回按序号（旧行为），但要留下线索
            import logging
            logging.getLogger("kira_accelerator").warning(
                "[accel] 按内容剥离没匹配上（文本可能被其它插件改写过），"
                "退回按序号剥离 %d 段", count)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("kira_accelerator").exception(
                "[accel] 按内容剥离出错，退回按序号剥离")
    return strip_early_sent(text, count)


def early_sent_segments(resp) -> list:
    """读取"已经抢先发出的段"的原文（供按内容剥离用）。"""
    try:
        segs = resp.__dict__.get("_accel_early_sent_segments") or []
        return list(segs)
    except Exception:  # noqa: BLE001
        return []


def strip_early_sent(text: str, count: int) -> str:
    """从完整文本里去掉**前 count 个已闭合的段**，返回仍需框架发送的部分。

    - 全部发完 ⇒ 返回 "<msg/>"（框架解析为空消息，不会发东西）
    - 一个都没发 ⇒ 原样返回
    """
    if count <= 0 or not text:
        return text
    out = text
    removed = 0
    while removed < count:
        m = _MSG_CLOSED.search(out)
        if not m:
            break
        out = out[:m.start()] + out[m.end():]
        removed += 1
    return out if out.strip() else "<msg/>"
