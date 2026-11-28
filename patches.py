"""接管护栏（Patch Handle）—— 让 monkey patch 具备"幂等 / 可还原 / 可熔断"。

这是整个提速器最关键的一个模块：**没有它，接管就是不可维护的**。

三条铁律：
  R1 幂等：重复安装必须安全（插件热重载会重复走 initialize）。
  R2 精确还原：只还原"当前还是我们自己装的那一个"，别人后来改过就不动。
  R3 熔断旁路（**带半开恢复，不是永久死亡**）：连续失败 N 次进入熔断，
     冷却后自动半开试探；试探成功即恢复正常，失败则冷却翻倍。
     详见 breaker.py（用户质疑过"永久熔断太严格"，已改）。

★ 关键：**同步/异步都能包**。
  实测 KiraAI 里被接管的函数三种都有：
    - `OpenAICompatibleLLMClient.chat`            → async
    - `OpenAICompatibleLLMClient._build_client`   → **同步**
    - `ProviderManager.get_model_client`          → **同步**
    - `SessionManager._save_memory`               → **同步**
  如果一律用 async wrapper 去包同步函数，调用方拿到的是 coroutine 而不是返回值，
  整个框架会静默崩掉（本项目装载自检抓到过这个 bug）。
  所以这里用 `asyncio.iscoroutinefunction` 判定，分别生成对应形状的包装。

用法：
    handle = PatchHandle("build_client")
    install(handle, cls, "_build_client", factory)
    ...
    handle.uninstall()      # 在 terminate() 里逐个调用
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, Optional

try:
    from .breaker import (
        BREAKER_THRESHOLD, BREAKER_BASE_COOLDOWN, BREAKER_MAX_COOLDOWN,
        Breaker, BreakerState, breaker_for,
    )
except ImportError:  # 独立运行（自检脚本直接 import patches）
    from breaker import (
        BREAKER_THRESHOLD, BREAKER_BASE_COOLDOWN, BREAKER_MAX_COOLDOWN,
        Breaker, BreakerState, breaker_for,
    )

logger = logging.getLogger("kira_accelerator")

# 标记：证明某个函数"是提速器装的"
MARK = "__kira_accel__"
# 反向引用：从包装函数拿回原函数（多层包装时用得上）
MARK_ORIGINAL = "__kira_accel_original__"
# 标记：这个接管点**有不可撤销的副作用**（例：消息真的发出去了）
SIDE_EFFECTS = "__kira_accel_side_effects__"
# 标记：这个包装属于哪个插件实例（热升级后旧实例的包装必须替换，不能认领）
MARK_OWNER = "__kira_accel_owner__"


def mark_side_effects(fn):
    """声明「这个实现有不可撤销的副作用」。

    ★ 为什么必须声明：`guard` 默认在实现抛异常时**回落原实现**。
      对纯函数（建客户端、读配置）这是很好的兜底；
      但对**已经发了消息**的函数，回落 = **再发一遍** ——
      用户线上看到的就是"同一段回复重复出现"。

      所以这类接管点出错时**必须让异常抛出去**：
      宁可让上层知道这次失败，也绝不重复执行副作用。
    """
    setattr(fn, SIDE_EFFECTS, True)
    return fn


def has_side_effects(fn) -> bool:
    """判断实现（或其被包裹的内层）是否声明了副作用。"""
    cur = fn
    for _ in range(5):                      # 解掉 functools.wraps 的几层
        if getattr(cur, SIDE_EFFECTS, False):
            return True
        nxt = getattr(cur, "__wrapped__", None) or getattr(cur, "__func__", None)
        if nxt is None or nxt is cur:
            break
        cur = nxt
    return False


def is_accel_wrapper(fn) -> bool:
    """这个函数**本身**是不是本插件装上去的包装（不是"链上有"）。

    ★ 为什么不只认 MARK：第三方用 `functools.wraps(我们的包装)` 时，
      MARK / MARK_ORIGINAL 会被原样复制到它的包装上。只看 MARK 会把
      **别人的包装**误当成我们的，热升级时甚至会把别人拆掉。
    ★ 真包装的特征：`__wrapped__` 就是 `MARK_ORIGINAL`（_mark 与 functools.wraps
      指向同一个 original）。复制标记的外来包装：`__wrapped__` 是"我们的包装"，
      而 MARK_ORIGINAL 是更内层的 original ⇒ 两者不同。
    """
    fn = getattr(fn, "__func__", fn)
    try:
        if not getattr(fn, MARK, False):
            return False
        return getattr(fn, "__wrapped__", None) is getattr(fn, MARK_ORIGINAL, None)
    except Exception:  # noqa: BLE001
        return False


def contains_mark(fn) -> bool:
    """这个函数（或它包裹的任意一层）是不是**本插件**装上去的。

    用途：别的插件把实例方法换成自己的包装时，我们决定"要不要再链一层"。
    链上已经有我们的东西就不要再套 —— 否则每次请求都会加深一层，
    既浪费又会让熔断计数/健康面板失真。

    ★ 必须顺着 `__wrapped__` 走：functools.wraps 会保留它，
      而 third-party 的包装器普遍用 wraps。
    ★ 每一层都用 is_accel_wrapper 判：第三方复制了 MARK 的包装不算。
    """
    cur, seen = fn, set()
    for _ in range(16):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        try:
            if is_accel_wrapper(cur):
                return True
            nxt = getattr(cur, "__wrapped__", None)
        except Exception:  # noqa: BLE001
            # 第三方包装器的 __wrapped__ 可能是个会抛错的 property；
            # 认不出来就当"不是我们的"（最多多链一层，绝不会误判成已装而漏装）。
            break
        if nxt is None or nxt is cur:
            break
        cur = nxt
    return False

class PatchHandle:
    """一个接管点的句柄。装得上、还原得回、坏得掉。"""

    def __init__(self, name: str, owner: Any = None, attr: str = ""):
        self.name = name
        self.owner = owner
        self.attr = attr
        self.original: Optional[Callable] = None
        self.installed: Optional[Callable] = None

    # ── R2 精确还原 ──────────────────────────────────────────────
    def uninstall(self) -> bool:
        """还原。只有"当前还是我们装的那一个"才还原。"""
        if self.owner is None or self.installed is None:
            return False
        current = getattr(self.owner, self.attr, None)
        if current is not self.installed:
            # 别人后来改了 —— 不碰，以免把别人的实现抹掉
            logger.warning(
                "[accel] 接管点 %s 已被他人覆盖，跳过还原（不破坏别人）", self.name
            )
            return False
        setattr(self.owner, self.attr, self.original)
        logger.info("[accel] 已还原接管点 %s", self.name)
        return True

    @property
    def active(self) -> bool:
        return self.installed is not None and (
            self.owner is None or getattr(self.owner, self.attr, None) is self.installed
        )


# ── R1 幂等安装 + R3 熔断包装 ────────────────────────────────────
def _mark(wrapper: Callable, original: Callable) -> Callable:
    setattr(wrapper, MARK, True)
    setattr(wrapper, MARK_ORIGINAL, original)
    return wrapper


def guard(name: str, original: Callable, impl: Callable) -> Callable:
    """把 impl 包成"失败即回落 original"的包装函数。

    ★ 按 original 的形状生成：async 函数生成 async wrapper，同步函数生成同步 wrapper。
      否则调用方拿到 coroutine，框架会静默崩掉（实测过的坑）。
    """
    if asyncio.iscoroutinefunction(original):

        @functools.wraps(original)
        async def awrapper(*args, **kwargs):
            br = breaker_for(name)
            if br.should_bypass():
                return await original(*args, **kwargs)
            try:
                result = await impl(*args, **kwargs)
                br.ok()
                return result
            except Exception as exc:  # noqa: BLE001
                br.fail(exc)
                if has_side_effects(impl):
                    # ★★ 有副作用 ⇒ **不回退**。回退等于把副作用再做一遍
                    #    （消息重发、回复重复）。宁可把异常交给上层。
                    logger.exception(
                        "[accel] 接管点 %s 出错；该实现有不可撤销的副作用，"
                        "**不回退原实现**（否则会重复发送）", name)
                    raise
                logger.exception("[accel] 接管点 %s 出错，回落原实现", name)
                return await original(*args, **kwargs)

        return _mark(awrapper, original)

    @functools.wraps(original)
    def swrapper(*args, **kwargs):
        br = breaker_for(name)
        if br.should_bypass():
            return original(*args, **kwargs)
        try:
            result = impl(*args, **kwargs)
            br.ok()
            return result
        except Exception as exc:  # noqa: BLE001
            br.fail(exc)
            if has_side_effects(impl):
                logger.exception(
                    "[accel] 接管点 %s 出错；该实现有不可撤销的副作用，"
                    "**不回退原实现**（否则会重复发送）", name)
                raise
            logger.exception("[accel] 接管点 %s 出错，回落原实现", name)
            return original(*args, **kwargs)

    return _mark(swrapper, original)


def install(handle: PatchHandle, owner: Any, attr: str, factory: Callable,
            owner_token: Any = None) -> Optional[PatchHandle]:
    """安装一个接管点。

    :param factory: 接收入参 `original`，返回"加速版实现"（同步/异步与原函数一致）。
    :param owner_token: 本次安装的归属（传插件实例）。同一个实例重复初始化 ⇒ 认领；
        不同实例（热升级）⇒ **替换**，绝不认领旧实例的包装 —— 旧包装的闭包绑定的是
        已 terminate 的对象，认领会让旧引擎/旧台账继续工作（重复发送的根因）。
    :return: 成功返回 handle；已装过且属于同一实例（R1 幂等）返回 None。
    """
    original = getattr(owner, attr, None)
    if original is None:
        logger.warning("[accel] 接管点 %s 不存在（%s.%s），跳过", handle.name, owner, attr)
        return None

    if is_accel_wrapper(original):
        same = owner_token is None or getattr(original, MARK_OWNER, None) is owner_token
        if same:
            # R1 幂等：同一实例重复初始化 —— 认领，不再包第二层。
            logger.info("[accel] 接管点 %s 已存在，本次认领（不重复包装）", handle.name)
            handle.owner, handle.attr = owner, attr
            handle.installed = original
            handle.original = getattr(original, MARK_ORIGINAL, None)
            return handle

        # ★★ 不同实例（热升级）：绝不能认领。
        #    旧包装的闭包绑定的是**已 terminate 的旧插件对象**：它的引擎还会抢先发送、
        #    它的台账/响应缓存与新实例完全分离 ⇒ 新实例的 parse_xml 剥不到已发段
        #    ⇒ 框架再发一次（线上："更新插件后每条都发两遍"）。
        #    能拆就直接拆到它包的那一层，再按当前实例重新包。
        prev = getattr(original, MARK_ORIGINAL, None)
        if prev is not None:
            try:
                setattr(owner, attr, prev)
            except Exception:  # noqa: BLE001
                pass
            else:
                original = prev
                logger.info("[accel] 接管点 %s 是旧实例留下的，已替换为当前实例", handle.name)

    wrapper = guard(handle.name, original, factory(original))
    setattr(wrapper, MARK_OWNER, owner_token)
    setattr(owner, attr, wrapper)

    handle.owner, handle.attr = owner, attr
    handle.original, handle.installed = original, wrapper
    logger.info("[accel] 已安装接管点 %s（%s.%s）", handle.name, getattr(owner, "__name__", owner), attr)
    return handle


class PatchRegistry:
    """持有全部接管点，统一安装 / 统一还原。"""

    def __init__(self) -> None:
        self._handles: list[PatchHandle] = []

    def add(self, handle: PatchHandle) -> None:
        self._handles.append(handle)

    def uninstall_all(self) -> None:
        for handle in reversed(self._handles):
            try:
                handle.uninstall()
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 还原接管点 %s 出错", handle.name)
        self._handles.clear()

    def health(self) -> list[dict]:
        out = []
        for h in self._handles:
            br = breaker_for(h.name)
            snap = br.snapshot()
            snap["active"] = h.active
            out.append(snap)
        return out
