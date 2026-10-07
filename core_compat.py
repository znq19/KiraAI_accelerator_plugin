"""KiraAI 世代兼容层 —— 把「两代之间只是换了位置」的核心符号解析出来。

## 为什么需要

提速器有 **3 个事件广播点**（`AFTER_XML_PARSE` / `ON_MESSAGE_SENT` / `ON_TOOL_RESULT`），
都从核心拿同一对符号 `event_handler_reg` + `EventType`。

而这两个符号在两代框架里**换了位置**（实测 KiraAI v2.34.8 与 v3.0.0-alpha）：

| 世代 | 模块路径 |
|------|----------|
| 2.x  | `core.plugin.plugin_handlers` |
| 3.0  | `core.plugin.handlers` |

**两个文件逐行对比几乎完全相同**（101 行 vs 102 行；3.0 只多了个 `ON_COMMENT` 枚举、
内部把 `plugin_registry` 改名成 `registry`）⇒ 属**纯位置迁移**，符号名与语义一致。

不处理的话，3.0 上每个广播点都会抛：

    ModuleNotFoundError: No module named 'core.plugin.plugin_handlers'

……用户日志里那一大片 `广播 AFTER_XML_PARSE 失败` / `补广播 ON_MESSAGE_SENT 失败`
就是它。后果不只是刷屏：**AFTER_XML_PARSE 广播失效 ⇒ 挂在它上面的插件功能全部失效**
（例如 qq-enhance 的「表情独立成行」）。

## 设计取舍

* **集中一处解析 + 缓存**：同一对符号有 3 处使用，散落三份 `try/except` 容易漏改、
  也难测。集中后只解析一次，全进程复用。
* **解析顺序：新路径在前、旧路径在后** —— 核心将来再改名时新名字自然优先命中，
  而且**两代都能用**（不需要先判断世代号，少一处可能判错的逻辑）。
* **拿不全就换下一个候选**：用 `getattr` 校验 `event_handler_reg` 与 `EventType`
  都在，避免命中「同名但内容不同」的模块。
* **解析不出来时返回 *空对象*，不返回 `None`** —— 这是关键：
  调用点写的是 `event_handler_reg.get_handlers(EventType.X)`，若给 `None`，
  其中一个调用点（`parallel_tools` 的 `ON_TOOL_RESULT` 广播）**没有 try 保护**，
  会直接 `AttributeError` 炸在工具执行路径上。
  给空对象 ⇒ **三处调用点一行都不用改**，语义自动退化成「没有插件挂这个钩子」，
  与原有容错语义（「广播失败就按未改写的内容发送」）完全一致。
* **降级时打一条 WARNING（只打一次）**：空对象是静默的，必须留个可见信号 ——
  否则将来核心再改名，会表现为「插件功能悄悄失效」而无人察觉。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Optional, Tuple

__all__ = ["event_system", "reset_cache",
           "openai_compatible_client", "reset_openai_cache"]

_logger = logging.getLogger(__name__)

#: 候选模块路径，**新世代在前**（详见模块 docstring 的「解析顺序」）
_EVENT_MODULE_CANDIDATES = (
    "core.plugin.handlers",          # KiraAI 3.0
    "core.plugin.plugin_handlers",   # KiraAI 2.x
)


class _NullRegistry:
    """没有事件系统时的空注册表：`get_handlers(...)` 恒返回空元组。"""

    __slots__ = ()

    @staticmethod
    def get_handlers(*_args: Any, **_kwargs: Any) -> tuple:
        return ()


class _NullEventType:
    """没有事件系统时的空枚举：任何属性访问都返回 `None`。

    这样 `EventType.AFTER_XML_PARSE` 这类写法不会 AttributeError；
    配合 `_NullRegistry`，`get_handlers(None)` 返回空 ⇒ 循环体不执行。
    """

    __slots__ = ()

    def __getattr__(self, _name: str) -> None:
        return None


_NULL: Tuple[Any, Any] = (_NullRegistry(), _NullEventType())

#: 解析结果缓存。`None` = 还没解析过。
_CACHE: Optional[Tuple[Any, Any]] = None


def event_system() -> Tuple[Any, Any]:
    """返回 `(event_handler_reg, EventType)`。

    解析不出来时返回**空对象**（不是 `None`）—— 详见模块 docstring。
    该函数**永不抛异常**，可安全放在抢发热路径里（结果有缓存，仅首次解析）。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    for module_name in _EVENT_MODULE_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 —— 该世代没有这个路径，试下一个
            continue
        registry = getattr(module, "event_handler_reg", None)
        event_type = getattr(module, "EventType", None)
        if registry is not None and event_type is not None:
            _CACHE = (registry, event_type)
            return _CACHE

    # ★ 两个候选都不行 —— 这不是"某个钩子没人挂"，而是"我们认不出这个核心"。
    #   保留可见信号，免得将来核心再改名时表现为"插件功能悄悄失效"。
    _logger.warning(
        "[accel] 未找到事件系统（试过 %s）—— 事件广播将降级为空操作；"
        "若核心已改名，请更新 core_compat._EVENT_MODULE_CANDIDATES",
        " / ".join(_EVENT_MODULE_CANDIDATES),
    )
    _CACHE = _NULL
    return _CACHE


def reset_cache() -> None:
    """清掉解析缓存（热重载 / 测试用）。"""
    global _CACHE
    _CACHE = None


# --------------------------------------------------------------------------- #
# OpenAI 兼容 LLM 客户端类 —— 同样是跨代搬家的符号
# --------------------------------------------------------------------------- #
_OPENAI_CLIENT_CANDIDATES = (
    "core.provider.openai_compatible",   # KiraAI 3.0
    "core.utils.model_clients",          # KiraAI 2.x
)

_OPENAI_CACHE: Optional[Any] = None
_OPENAI_TRIED = False


def openai_compatible_client() -> Optional[Any]:
    """返回 `OpenAICompatibleLLMClient` 类；两代都找不到时返回 `None`。

    ★ 为什么也要集中解析（2026-10-07 实测）：这个类在两代里**也换了位置**，
    而提速器有**两处功能**挂在它上面：

      * `_install_client_cache` —— HTTP 客户端复用（核心提速点之一）
      * 思考注入（OpenAI 兼容系）

    原来两处都写 `from core.utils import model_clients`，3.0 上抛
    `ImportError: cannot import name 'model_clients' from 'core.utils'`
    ⇒ 各自 `logger.exception` 记一条 ERROR 然后 `return` ⇒
    **这两个功能在 3.0 上静默消失**（只是刷了一条日志，功能没了）。
    实测：3.0 上 `test_client_cache` / 思考注入相关用例全红。

    结果缓存（含"确认没有"），可安全放在安装流程里重复调用。
    """
    global _OPENAI_CACHE, _OPENAI_TRIED
    if _OPENAI_TRIED:
        return _OPENAI_CACHE
    _OPENAI_TRIED = True

    for module_name in _OPENAI_CLIENT_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        cls = getattr(module, "OpenAICompatibleLLMClient", None)
        if cls is not None:
            _OPENAI_CACHE = cls
            return cls

    _logger.warning(
        "[accel] 未找到 OpenAICompatibleLLMClient（试过 %s）—— "
        "HTTP 客户端复用与 OpenAI 系思考注入将跳过；"
        "若核心已改名，请更新 core_compat._OPENAI_CLIENT_CANDIDATES",
        " / ".join(_OPENAI_CLIENT_CANDIDATES),
    )
    return None


def reset_openai_cache() -> None:
    """清掉 OpenAI 客户端解析缓存（测试用）。"""
    global _OPENAI_CACHE, _OPENAI_TRIED
    _OPENAI_CACHE = None
    _OPENAI_TRIED = False
