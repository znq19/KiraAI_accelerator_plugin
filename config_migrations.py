"""一次性配置迁移（v1.0.78 起）—— 把 inject_nothinking 的旧默认值 false 改成 true。

## 为什么需要这个文件（背景，必须记牢）

「不需要时显式关闭思考」（`section_thinking.inject_nothinking`）在 v1.0.77 及以前
默认 **关**。后果（2026-10-05 用户实测）：

    日志：`思考=关(得分-2.0)[26字]`
    同轮：`reasoning_content='主人周武戳了戳我…'`、耗时 7.57 秒

判定明明判了"这轮不要思考"，模型却仍在思考 —— 因为默认关时，插件**根本不给
网关发关闭参数**，提供商面板/模型自己的默认思考照常生效。"自动开关"于是名不副实。

v1.0.78 起该开关默认改为 **开**。但**光改 schema 默认值对老用户无效**：
框架 `_ensure_plugin_config` 的合并规则是"只给**缺失**的键补默认值"
（core/plugin/plugin_registry.py）——老用户的配置文件里早就写着 `false`，
它会一直被保留。⇒ 必须做一次**显式迁移**。

## 设计（安全第一）

- **仅一次**：迁移完成的标记放在插件数据目录（`plugin_data/<id>/config_migrations.json`）。
  标记存在 ⇒ 以后一律不动用户的设置（用户之后手动关掉，不会被再次打开）。
- **走框架自己的通道**：用 `plugin_mgr.update_plugin_config()` 写配置
  （与面板"保存并热重载"完全同一条路：更新内存 + 落盘 + 重载插件）。
  **绝不直接改配置文件** —— 那样内存缓存与磁盘会不一致，面板和下次保存都会乱。
- **不重入**：该调用会热重载本插件，所以不能在 ``initialize()`` 的调用栈里立刻执行；
  由 ``main.py`` 延迟到初始化完成后执行一次。
- **不折腾新用户**：配置里已是 ``true``（或缺失）⇒ 只写标记、不改配置、不触发重载。
  标记的作用：防止将来把用户**手动关掉**的 false 误当"旧默认"再次翻回去。
- **失败安静**：任何一步失败（无管理器、无数据目录、写盘失败…）都只记日志跳过，
  绝不影响插件其余功能；下次加载会重试。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: 迁移延迟（秒）—— 等插件初始化完成后再执行（它会把插件热重载一次）
MIGRATION_DELAY_S = 3.0

#: 标记文件名（插件数据目录内）
MARKER_FILE = "config_migrations.json"

#: 本次迁移的键名（将来还有别的迁移就往后追加，互不干扰）
KEY = "nothink_default_on_v1"


def marker_applied(data_dir) -> bool:
    """迁移标记是否已存在且表示"已处理过"。"""
    if data_dir is None:
        return False
    try:
        f = Path(data_dir) / MARKER_FILE
        if not f.is_file():
            return False
        data = json.loads(f.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get(KEY))
    except Exception:  # noqa: BLE001
        return False


def write_marker(data_dir, *, flipped: bool) -> None:
    """写入迁移标记（先写临时文件再原子替换；保留文件里其它迁移的键）。"""
    if data_dir is None:
        return
    f = Path(data_dir) / MARKER_FILE
    data: dict = {}
    try:
        if f.is_file():
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception:  # noqa: BLE001
        data = {}
    data[KEY] = {"applied": True, "flipped": bool(flipped), "at": int(time.time())}
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def decide_plan(current_value) -> str:
    """决定动作：'flip'=旧默认值需迁移为开；'mark'=无需改动、只记标记。

    只有**严格等于 False**（旧默认的形态）才迁移；缺失/true/其它怪值一律不动。
    """
    return "flip" if current_value is False else "mark"


async def run_nothink_migration(plugin, plugin_id: str, data_dir, log) -> str:
    """执行迁移。返回 'flipped' / 'marked' / 'skip' / 'error'（也是测试的断言接口）。

    调用前提（由调用方保证）：插件已加载完成、data_dir 为插件数据目录、log 为 logger。
    本函数自带全部守卫，任何情况下不抛异常（失败返回 'error'/'skip'）。
    """
    if marker_applied(data_dir):
        return "skip"
    mgr = getattr(getattr(plugin, "ctx", None), "plugin_mgr", None)
    if mgr is None:
        return "skip"                      # 无管理器（测试环境等）：不迁移
    # 插件还在册（没被卸载/替换）才继续 —— 定时器可能晚于卸载触发
    insts = getattr(mgr, "plugin_instances", None)
    if isinstance(insts, dict) and insts.get(plugin_id) is not plugin:
        return "skip"
    try:
        if not mgr.is_plugin_enabled(plugin_id):
            return "skip"                  # 已被用户停用：不动，等重新启用
    except Exception:  # noqa: BLE001
        pass
    try:
        current = mgr.get_plugin_config(plugin_id)
        think = current.get("section_thinking") if isinstance(current, dict) else None
        value = think.get("inject_nothinking") if isinstance(think, dict) else None
        if decide_plan(value) == "flip":
            new_think = dict(think or {})
            new_think["inject_nothinking"] = True
            # ★ 只替换 section_thinking（其它 section 原样保留），且走框架通道
            await mgr.update_plugin_config(plugin_id, {"section_thinking": new_think})
            write_marker(data_dir, flipped=True)
            log.info("[accel] 配置迁移（仅一次）：inject_nothinking 已按 v1.0.78 "
                     "新默认自动打开；如需关闭可在面板里改回")
            return "flipped"
        write_marker(data_dir, flipped=False)
        return "marked"
    except Exception as e:  # noqa: BLE001
        log.warning("[accel] 配置迁移未完成（不影响其它功能，下次加载会重试）：%s", e)
        return "error"
