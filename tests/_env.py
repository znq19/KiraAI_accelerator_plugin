"""测试公共环境：定位「被测插件根」与「框架源码」。

为什么要有这个文件：
  1. 测试搬进 `tests/` 后，`Path(__file__).parent` 不再等于插件根，
     而是 `tests/` —— 路径全靠猜容易再错一次，集中在这里最省事。
  2. 之前有几个测试**硬编码了开发机上的绝对路径**，结果在别处跑时
     import 到的是开发目录的副本，而不是被测的这份代码 —— 等于自欺欺人。
     现在统一按 `__file__` 相对定位，任何人 clone 下来都能跑。
"""
import importlib
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

#: 插件根（本文件在 tests/ 下）
ROOT = Path(__file__).resolve().parent.parent

#: 被测插件在 sys.modules 里的别名（与目录名解耦，clone 成什么名字都行）
ALIAS = "_plugin_under_test"

#: 框架源码候选位置，按优先级。
#  刻意**不**去猜机器上的其它克隆（例如 /tmp 下随便一个 KiraAI）——
#  版本可能不同，那样"通过"就是假的。要么显式给，要么跳过。
_FW_CANDIDATES = [
    os.environ.get("KIRA_FW"),
    str(ROOT / "kira_fw_v2346"),
    str(ROOT.parent / "kira_fw_v2346"),
]


def framework():
    """返回可用的 KiraAI 框架源码目录；找不到返回 None（调用方应优雅跳过）。"""
    for c in _FW_CANDIDATES:
        if c and os.path.isdir(os.path.join(c, "core", "plugin")):
            return c
    return None


def load(submodule: str):
    """按**路径**导入被测插件的子模块。

    不走 `import <目录名>` —— 目录名可能带 `-` 之类不能做标识符的字符；
    这里用固定别名注册成包，目录名只当路径用。
    """
    if ALIAS not in sys.modules:
        # 手工造一个命名空间包：spec_from_file_location 只认文件，不认目录
        spec = importlib.machinery.ModuleSpec(ALIAS, None, is_package=True)
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(ROOT)]
        sys.modules[ALIAS] = module
    return importlib.import_module(f"{ALIAS}.{submodule}")


#: 跳过标记 —— run_tests.sh 靠它区分"跳过"与"通过"，
#  所以不能拿"跳过"这种普通词去猜（别的测试正文里本来就有）。
SKIP_MARK = "[SKIP]"


def skip(why: str) -> None:
    """打印跳过原因并以 0 退出（跳过不算失败）。"""
    print(f"⚠ {SKIP_MARK} {why}")
    raise SystemExit(0)


def plugin_file(*parts):
    """插件根下的文件路径，例如 plugin_file("web", "index.html")。"""
    return ROOT.joinpath(*parts)


# --------------------------------------------------------------------------- #
# 跨世代符号解析
# --------------------------------------------------------------------------- #
#: 同一个符号在两代框架里的位置。**新世代在前**。
#:
#: ★ 这些测试原本硬编码了 2.x 的路径 ⇒ 在 3.0 上整个测试**跑不到被测代码**
#:   （表现为 ModuleNotFoundError，看起来像"插件坏了"，其实测试根本没执行）。
#:   框架把模块搬了家，测试也得跟着走，否则 3.0 的回归保护是假的。
_CROSSGEN_SYMBOLS = {
    "event_system": (
        ("core.plugin.handlers", "event_handler_reg", "EventType"),        # 3.0
        ("core.plugin.plugin_handlers", "event_handler_reg", "EventType"),  # 2.x
    ),
    "OpenAICompatibleLLMClient": (
        ("core.provider.openai_compatible", "OpenAICompatibleLLMClient"),   # 3.0
        ("core.utils.model_clients", "OpenAICompatibleLLMClient"),           # 2.x
    ),
    "model_clients": (
        ("core.provider.openai_compatible",),                                # 3.0
        ("core.utils.model_clients",),                                       # 2.x
    ),
    "plugin_registry": (
        ("core.plugin.registry",),                                           # 3.0
        ("core.plugin.plugin_registry",),                                    # 2.x
    ),
    "PluginManager": (
        ("core.plugin.manager", "PluginManager"),                           # 3.0
        ("core.plugin.plugin_registry", "PluginManager"),                    # 2.x
    ),
    "get_obj_plugin_id": (
        ("core.plugin.registry", "get_obj_plugin_id"),                       # 3.0
        ("core.plugin.plugin_registry", "get_obj_plugin_id"),                # 2.x
    ),
}


def resolve(symbol: str, *extra: str):
    """按跨世代表解析符号，返回**该符号本身的元组**（可含 extra 项）。

    用法::

        reg, et = env.resolve("event_system")           # → (registry, EventType)
        cls = env.resolve("OpenAICompatibleLLMClient")  # → (类,)

    全部候选都拿不到时抛 ImportError（保持与直接 import 一致的失败语义）。
    """
    tried = []
    for cand in _CROSSGEN_SYMBOLS[symbol]:
        module_name, *attrs = cand
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            tried.append(f"{module_name}({type(exc).__name__})")
            continue
        got = []
        ok = True
        for a in attrs:
            v = getattr(module, a, None)
            if v is None:
                ok = False
                break
            got.append(v)
        if ok:
            return tuple(got)
        tried.append(f"{module_name}(缺符号)")
    raise ImportError(f"跨世代解析 {symbol} 失败，试过: {', '.join(tried)}")


def resolve_module(symbol: str):
    """返回该符号所在模块（供 `mod("core.xxx", **kw)` 这类需要模块名的场景）。"""
    for cand in _CROSSGEN_SYMBOLS[symbol]:
        module_name = cand[0]
        try:
            importlib.import_module(module_name)
            return module_name
        except Exception:  # noqa: BLE001
            continue
    return _CROSSGEN_SYMBOLS[symbol][-1][0]   # 兜底返回旧路径名
