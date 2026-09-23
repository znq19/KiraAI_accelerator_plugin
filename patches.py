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


def install(handle: PatchHandle, owner: Any, attr: str, factory: Callable) -> Optional[PatchHandle]:
    """安装一个接管点。

    :param factory: 接收入参 `original`，返回"加速版实现"（同步/异步与原函数一致）。
    :return: 成功返回 handle；已装过（R1 幂等）返回 None。
    """
    original = getattr(owner, attr, None)
    if original is None:
        logger.warning("[accel] 接管点 %s 不存在（%s.%s），跳过", handle.name, owner, attr)
        return None

    if getattr(original, MARK, False):
        # R1 幂等：已经装过（通常是上一次实例留下的）—— 不再包第二层。
        #
        # ★ 但仍然**返回 handle**，让本次实例"认领"这个接管点。
        #   为什么必须认领：框架重载时一般会先 terminate（plugin_registry.py:1900），
        #   可万一那次 terminate 抛了异常，框架只记一条日志就继续初始化；
        #   此时旧补丁还在，而新实例如果不认领它，就**再也没人能还原**
        #   —— 表现为"插件已经关了，行为却还在"，且完全没有报错。
        #
        #   认领是安全的：uninstall() 有守卫，当前实现若不是我们装的那个就不碰。
        logger.info("[accel] 接管点 %s 已存在，本次认领（不重复包装）", handle.name)
        handle.owner, handle.attr = owner, attr
        handle.installed = original
        handle.original = getattr(original, MARK_ORIGINAL, None)
        return handle

    wrapper = guard(handle.name, original, factory(original))
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
