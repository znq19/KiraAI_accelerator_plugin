import re, pathlib, json, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "web/index.html").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
PY_ALL = {p.name: p.read_text(encoding="utf-8") for p in ROOT.glob("*.py")}
OK = []; BAD = []
def check(n, c, d=""):
    (OK if c else BAD).append(n)
    print(("  \u2713 " if c else "  \u2717 ") + n + (("  [" + str(d) + "]") if d else ""))

print("=== v1.0.2 新增功能 ===")

# 1) providers 多选
_f = SCHEMA["section_stream"]["fields"]["providers"]
check("① providers 用框架规范的 multi_select", _f.get("type") == "multi_select", _f.get("type"))
check("① source=model（选项来自已配置模型）", _f.get("source") == "model")
check("① allow_custom 兜底（拿不到列表仍可手填）", _f.get("allow_custom") is True)
_m = re.search(r"not \(\{pname, pid, f\"\{pid\}:\{mid\}\"\} & allowed\)", PY_ALL.get("main.py", ""))
check("① 后端同时认 model/provider/name 三种取值", bool(_m))
check("① 面板也支持多选（list → multi_select 同一分支）",
      'type === "list" || type === "multi_select"' in HTML)
check("① 面板有 /providers 加载（拿不到就退化为手填）",
      "loadProviderOptions" in HTML and "__ACCEL_PROVIDERS__" in HTML)
check("① 新增 GET /providers API", 'path="/providers"' in PY_ALL.get("main.py", ""))

# 2) 标题扫光
check("② neon-drop 关键帧存在（由上往下）", "@keyframes neonDrop" in HTML)
check("② drop 每轮重掷随机量", "startDropCycle" in HTML and "--drop-x" in HTML and "--drop-dur" in HTML)
check("② drop 靠 .neon-drop-run 触发（否则只跑一次）", "neon-drop-run" in HTML)
check("② drop 有描边 ⇒ 字不会被遮住", re.search(r"neon-drop[^{]*\{[^}]*text-stroke", HTML) is not None)
check("② orbit 真正启用（orbitSpin 被引用）",
      re.search(r"neon-orbit[^{]*\{[^}]*animation:\s*orbitSpin", HTML) is not None)
check("② ring 真正启用（neonRing 被引用）",
      re.search(r"neon-ring[^{]*\{[^}]*animation:\s*neonRing", HTML) is not None)
check("② NEON_MODES 含 neon-drop", "neon-drop" in re.search(r"const NEON_MODES\s*=\s*\[([^\]]*)\]", HTML).group(1))

# 3) 外链按钮
check("③ 有外链按钮且在标题栏", 'id="open-panel"' in HTML)
check("③ 用 i-external 图标且图标已定义", 'href="#i-external"' in HTML and 'id="i-external"' in HTML)
check("③ 新标签页打开（window.open + noopener）",
      "window.open(" in HTML and "noopener" in HTML)
check("③ 走 /plugin-page/ 路径", "/plugin-page/" in HTML)
check("③ 有持续特效（常驻动画）", re.search(r"\.icon-btn[^{]*\{[^}]*animation:", HTML) is not None)
check("③ 有 title 无障碍提示", 'open-panel' in HTML and 'title=' in HTML[HTML.find("open-panel"):HTML.find("open-panel")+400])

# 4/5) 工具轮 / 复读
check("④ 工具轮已发内容会被标记（避免重复发送/复读）",
      "_accel_tool_turn_early" in PY_ALL.get("stream_engine.py", ""))
check("④ 有工具标签转义的诊断日志",
      "xml_tag_fixer" in PY_ALL.get("stream_engine.py", ""))
check("④ 本插件确实从不 escape（只 unescape）",
      "escape" not in PY_ALL.get("early_sent.py", "").replace("unescape", "")
      and "unescape" in PY_ALL.get("early_sent.py", ""))
check("⑤ 版本号已前进", json.loads((ROOT/"manifest.json").read_text(encoding='utf-8'))["version"].endswith(".2"))

print()
print(("🎉 全部通过（%d 项）" % len(OK)) if not BAD else ("❌ %d 项未通过: %s" % (len(BAD), BAD[:5])))
sys.exit(1 if BAD else 0)
