"""★★ v1.0.78 一次性配置迁移：inject_nothinking false → true（仅一次）。

用户需求（2026-10-05）：
  「默认 true 吧那，不然用户肯定会忘记设置从而根本起不到自动开关的效果，
    不是吗？做好迁移，让用户更新时仅一次默认改这个为开。」

背景（为什么必须迁移而不是只改默认值）：
  框架 `_ensure_plugin_config` 的合并规则是"只给**缺失**的键补默认值"，
  老用户配置文件里早已写着 `inject_nothinking: false` ⇒ 光改 schema 默认值
  对他们**完全无效**。必须把旧值显式改成 true —— 且只做一次：
  之后用户手动改回 false，不许再被翻回去。

本测试（纯逻辑，不需要框架源码）：
  1) decide_plan / 标记文件读写
  2) run_nothink_migration 全分支：flip / mark / skip(已迁移) / skip(无管理器)
     / skip(已被替换) / skip(停用) / error(失败不写标记、下次重试)
  3) 回收检查：只走框架通道 update_plugin_config、只替换 section_thinking
  4) 默认链路守卫：schema 默认 true、main.py 默认链路、initialize 调度、terminate 取消
  5) ★★★ 最关键的反向用例：用户手动关掉之后不会被再次翻回
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

cm = _env.load("config_migrations")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


LOG = logging.getLogger("accel-migration-test")
LOG.setLevel(logging.INFO)     # 捕获 info 级迁移日志（root 默认 WARNING 会过滤）


class FakeMgr:
    """够用的假 PluginManager：只实现迁移用到的四个成员。"""

    def __init__(self, cfg, enabled=True, fail_update=False):
        self.cfg = copy.deepcopy(cfg)
        self.enabled = enabled
        self.fail_update = fail_update
        self.calls = []
        self.plugin_instances = {}

    def is_plugin_enabled(self, pid):
        return self.enabled

    def get_plugin_config(self, pid):
        return copy.deepcopy(self.cfg)

    async def update_plugin_config(self, pid, new):
        self.calls.append(("update", pid, copy.deepcopy(new)))
        if self.fail_update:
            raise RuntimeError("disk full (simulated)")
        for k, v in new.items():                  # 与框架一致的合并语义
            self.cfg[k] = copy.deepcopy(v)
        return dict(self.cfg)


def make_env(cfg, enabled=True, register=True, fail_update=False):
    mgr = FakeMgr(cfg, enabled=enabled, fail_update=fail_update)
    plugin = SimpleNamespace(ctx=SimpleNamespace(plugin_mgr=mgr))
    if register:
        mgr.plugin_instances["kid"] = plugin
    d = Path(tempfile.mkdtemp(prefix="accel_mig_"))
    return plugin, mgr, d


def run(coro):
    return asyncio.run(coro)


print("1) decide_plan：只有『显式 false』才迁移")
check("false ⇒ flip", cm.decide_plan(False) == "flip")
check("true ⇒ mark（新装/已开：只记标记）", cm.decide_plan(True) == "mark")
check("缺失 ⇒ mark", cm.decide_plan(None) == "mark")
check("非布尔怪值 ⇒ mark（不冒险）",
      cm.decide_plan("false") == "mark" and cm.decide_plan(0) == "mark")

print("\n2) 标记文件读写")
d = Path(tempfile.mkdtemp(prefix="accel_mig_"))
check("初始：无标记", cm.marker_applied(d) is False)
cm.write_marker(d, flipped=True)
check("写入后可读", cm.marker_applied(d) is True)
data = json.loads((d / cm.MARKER_FILE).read_text(encoding="utf-8"))
check("标记含 flipped=True 与时间戳",
      data[cm.KEY]["flipped"] is True and data[cm.KEY]["at"] > 0)
check("data_dir=None 安全", cm.marker_applied(None) is False)
(d / cm.MARKER_FILE).write_text("{broken", encoding="utf-8")
check("损坏文件按未迁移处理（不炸）", cm.marker_applied(d) is False)
cm.write_marker(d, flipped=False)
check("损坏文件可被覆盖修复", cm.marker_applied(d) is True)

print("\n3) run_nothink_migration 全分支")
# 3a) 旧默认 false ⇒ flip：改配置 + 写标记 + 走框架通道
cfg = {"section_thinking": {"enabled": True, "inject_nothinking": False,
                            "threshold": 2.0},
       "section_stream": {"force_stream": True}}
plugin, mgr, d = make_env(cfg)
_buf = []


class _Cap(logging.Handler):
    def emit(self, rec):
        _buf.append(rec.getMessage())


_h = _Cap()
LOG.addHandler(_h)
r = run(cm.run_nothink_migration(plugin, "kid", d, LOG))
LOG.removeHandler(_h)
check("false ⇒ flipped", r == "flipped", r)
check("调用了框架通道 update_plugin_config（不是直接改文件）",
      len(mgr.calls) == 1 and mgr.calls[0][1] == "kid", str(mgr.calls))
newsec = mgr.calls[0][2]["section_thinking"]
check("inject_nothinking 已改成 True", newsec.get("inject_nothinking") is True)
check("同 section 其它键保留（threshold）", newsec.get("threshold") == 2.0)
check("只提交 section_thinking 一个 section（别的原样）",
      set(mgr.calls[0][2].keys()) == {"section_thinking"})
check("标记已写", cm.marker_applied(d) is True)
check("有可见的迁移日志（用户可查）",
      any("配置迁移" in m for m in _buf), str(_buf))

# 3b) 再跑一次 ⇒ skip（已迁移，绝不再动）
_before_calls = len(mgr.calls)
r = run(cm.run_nothink_migration(plugin, "kid", d, LOG))
check("已迁移 ⇒ skip、零调用",
      r == "skip" and len(mgr.calls) == _before_calls, r)

# 3c) 已是 true ⇒ mark：不改配置、不调用通道，只写标记
cfg2 = {"section_thinking": {"inject_nothinking": True}}
plugin2, mgr2, d2 = make_env(cfg2)
r = run(cm.run_nothink_migration(plugin2, "kid", d2, LOG))
check("true ⇒ marked", r == "marked", r)
check("没调用 update（不触发重载）", mgr2.calls == [])
check("标记已写（防止将来把用户手动关的 false 当旧默认翻回）",
      cm.marker_applied(d2) is True)

# 3d) ★★★ 最关键的反向用例：用户手动关掉后，不许再次翻回去
cfg3 = {"section_thinking": {"inject_nothinking": False}}
plugin3, mgr3, d3 = make_env(cfg3)
run(cm.run_nothink_migration(plugin3, "kid", d3, LOG))       # 第一次：flip
check("（前置）第一次已 flip",
      mgr3.cfg["section_thinking"]["inject_nothinking"] is True)
mgr3.cfg["section_thinking"]["inject_nothinking"] = False    # 用户回面板关掉
r = run(cm.run_nothink_migration(plugin3, "kid", d3, LOG))   # 第二次：必须 skip
check("★★★ 用户关掉后：不会再被自动打开（标记守护）",
      mgr3.cfg["section_thinking"]["inject_nothinking"] is False, r)

# 3e) 无管理器 ⇒ skip（测试/异常环境安全）
plugin4 = SimpleNamespace(ctx=None)
d4 = Path(tempfile.mkdtemp(prefix="accel_mig_"))
r = run(cm.run_nothink_migration(plugin4, "kid", d4, LOG))
check("无 plugin_mgr ⇒ skip 且不写标记",
      r == "skip" and cm.marker_applied(d4) is False)

# 3f) 已被替换/卸载 ⇒ skip（定时器晚触发）
cfg5 = {"section_thinking": {"inject_nothinking": False}}
plugin5, mgr5, d5 = make_env(cfg5, register=False)
mgr5.plugin_instances["kid"] = SimpleNamespace()             # 装着"别人"
r = run(cm.run_nothink_migration(plugin5, "kid", d5, LOG))
check("插件已被替换 ⇒ skip、零调用", r == "skip" and mgr5.calls == [])
check("未写标记（留给下次真正加载时处理）", cm.marker_applied(d5) is False)

# 3g) 已被停用 ⇒ skip、不写标记（重新启用时会再试）
cfg6 = {"section_thinking": {"inject_nothinking": False}}
plugin6, mgr6, d6 = make_env(cfg6, enabled=False)
r = run(cm.run_nothink_migration(plugin6, "kid", d6, LOG))
check("停用 ⇒ skip、零调用、未写标记",
      r == "skip" and mgr6.calls == [] and cm.marker_applied(d6) is False)

# 3h) 写盘失败 ⇒ error、不写标记（下次重试），绝不半途而废
cfg7 = {"section_thinking": {"inject_nothinking": False}}
plugin7, mgr7, d7 = make_env(cfg7, fail_update=True)
r = run(cm.run_nothink_migration(plugin7, "kid", d7, LOG))
check("update 失败 ⇒ error（不炸）", r == "error", r)
check("失败不写标记（下次加载重试）", cm.marker_applied(d7) is False)

print("\n4) 架构守卫：走框架通道、不碰配置本体、不依赖框架")
src = (_env.ROOT / "config_migrations.py").read_text(encoding="utf-8")
check("使用 update_plugin_config（与面板保存同一通道）",
      "update_plugin_config" in src)
check("不直接读写插件配置文件（无 PLUGIN_CONFIG_DIR / 无 .json 配置直写）",
      "PLUGIN_CONFIG_DIR" not in src and "kira_accelerator.json" not in src)
check("纯逻辑模块（不 import core.*，可在无框架环境跑）",
      "from core" not in src and "import core" not in src)
check("标记文件名/键名可扩展（具名常量）",
      isinstance(cm.MARKER_FILE, str) and "nothink" in cm.KEY)

print("\n5) 默认链路与调度守卫（schema / main.py）")
schema = json.loads((_env.ROOT / "schema.json").read_text(encoding="utf-8"))
thk = schema.get("section_thinking", {}).get("fields", {})
inj = thk.get("inject_nothinking", {})
check("★★ schema：inject_nothinking 默认 = true", inj.get("default") is True)
check("schema 中文提示提到迁移",
      "迁移" in (inj.get("locales", {}).get("zh", {}).get("hint", "")))
main_src = (_env.ROOT / "main.py").read_text(encoding="utf-8")
check("main.py：读取默认值也是 True（schema 与代码一致）",
      'c_thk.get("inject_nothinking", True)' in main_src)
check("initialize 会调度一次性迁移",
      "self._schedule_nothink_migration()" in main_src)
check("terminate 会取消定时器",
      "_migration_timer" in main_src and ".cancel()" in main_src)
check("迁移延迟常量存在且合理（给初始化留时间）", cm.MIGRATION_DELAY_S >= 1.0)
check("迁移走主插件 ID（kira_accelerator）",
      'PLUGIN_ID = "kira_accelerator"' in main_src)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 一次性迁移全部通过 —— 老用户自动改开一次，之后以用户设置为准")
