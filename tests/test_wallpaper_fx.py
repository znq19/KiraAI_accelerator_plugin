"""壁纸切换特效：**不能有硬切**，且每种特效都要真的在动。

为什么单独一个套件：用户连续反馈"切换还是硬切/连淡入淡出都没有"，
而根因每次都不一样（reduced-motion 抹掉 transform、首图直接放上、
条带残留……）。字符串判据证明不了"看起来在过渡"，所以这里查三件事：
  ① 有没有哪条规则把过渡**整体关掉**（历史踩坑点）
  ② 每种特效是否真的驱动了视觉量（opacity / clip-path / transform / filter）
  ③ 每种特效是否上报了真实时长（否则收尾早到 ⇒ 闪一下 = 硬切观感）
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

ROOT = _env.ROOT
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
JS = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", HTML))
NC = re.sub(r"/\*[\s\S]*?\*/", "", HTML)          # 剥注释：注释里提到也会误报

_fail = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        _fail.append(name)


print("═══ 壁纸切换：不得有硬切 ═══")

# ① 不得有任何规则把壁纸层的过渡整体抹掉（历史真 bug：reduced-motion 里
#    `.wp-img{transform:none!important}` ⇒ 缩放/擦除全废 ⇒ 只剩瞬切）
_bad = []
for m in re.finditer(r"@media \(prefers-reduced-motion:reduce\)\{((?:[^{}]|\{[^{}]*\})*)\}", NC):
    b = m.group(1)
    if re.search(r"\.wp-(img|layer)[^{]*\{[^}]*transform:none", b):
        _bad.append("transform:none")
    if re.search(r"\.wp-(img|layer)[^{]*\{[^}]*transition:none", b):
        _bad.append("transition:none")
check("★ 减少动效下**不抹掉**壁纸过渡（那会变成硬切）", not _bad, str(_bad))

# ② 可见性必须能过渡：不能用"类名切换"直接决定 opacity（那是瞬变）
check("★ 新图可见性由动画驱动（revealIn 写 opacity 关键帧）",
      "function revealIn" in JS and "{opacity:0},{opacity:1}" in JS)

# ③ 每种特效都要真的驱动视觉量，并上报时长
FX = {
    "fxFade":   ["opacity"],
    "fxIris":   ["clipPath"],
    "fxWipe":   ["clipPath"],
    "fxZoom":   ["transform"],
    "fxBurn":   ["maskImage", "filter"],     # blob 挖空 + 受热暖调
}
for fn, needs in FX.items():
    i = JS.find("function " + fn)
    # ⚠️ 窗口要够大：fxBurn 已长到 4500+ 字符，6000 会把尾部（含 wpLastDur）截掉
    #   ⇒ 判据假红。取 12000 留足余量。
    seg = JS[i:i + 12000] if i > 0 else ""
    check(f"★ {fn} 存在", i > 0)
    for nd in needs:
        check(f"  {fn} 驱动 {nd}", nd in seg)
    check(f"  {fn} 上报真实时长 wpLastDur", "wpLastDur" in seg)

# ④ 条带类必须有真实铺满时长（否则旧图先退 ⇒ 露底色 ⇒ 观感就是硬切）
check("★ 条带特效上报真实铺满时长（coverAt）",
      "wpLastDur = coverAt" in JS)

# ⑤ 收尾必须先等**这一套自己的**时长，而不是固定值
check("★ 收尾等待本套特效的实际时长（不是写死常量）",
      "await sleep(wpLastDur + " in JS)

# ⑥ 首图不得"直接放上"（那是硬切）；必须有可见底图时才走过渡
check("★ 首图走过渡揭示（不硬贴）", "wpTransition(wpUrl(wpActive[0]))" in JS)

print()
print("═══ 燃纸 burn ═══")
_b = JS[JS.find("function fxBurn"):JS.find("/* ── 斜擦")]
check("★ 已注册在 WP_EFFECTS", '"burn"' in JS)
check("★ 无焦边层（用户明确不要）", "scorch" not in _b)
check("★ 两层可见性都显式写动画（否则烧在透明层上=硬切）",
      "anim(from, [{opacity:1},{opacity:1}]" in _b)
check("★ 无明火（用户明确不要）", not re.search(r"\bfire\b|flame", _b, re.I))
check("  fxBurn 上报真实时长 wpLastDur", "wpLastDur = dur" in _b)
check("★ 有燃烧动感：受热暖调", "sepia(" in _b)
check("★ 有燃烧动感：受热暖调 + 火线发光", "sepia(" in _b and "screen" in _b)
check("★ 边缘不规则（多次谐波扰动）", "Math.sin(lobes * th + ph + wob)" in _b)
# ★ 用户要求改成"多个地方随机点燃" ⇒ 起点不再是单个
check("★ 多处随机点燃（3~6 个火源）",
      re.search(r"3 \+ Math\.floor\(Math\.random\(\) \* 4\)", _b) is not None)
check("★ 每处速率不同（否则是同心圆）", "rate:" in _b)
check("★ 火线为连续曲线（blob，非网格方块）", "blob(" in _b and "Q" in _b)
check("★ 用 mask 挖洞（evenodd 单路径）", "evenodd" in _b and "maskImage" in _b)
check("★ 三层边缘：光晕 + 火芯 + 炭化线", "glow" in _b and "core" in _b and "char" in _b)
check("★ 半径到边缘为止（不会一秒吞屏）", "q.far" in _b)
check("★ 可取消（收尾清理）", "cancelAnimationFrame" in _b)

print()
print("═══ 特效清单一致性 ═══")
import json
_m = re.search(r"const WP_EFFECTS = \[([^\]]*)\]", JS)
_js = [x.strip().strip('"') for x in _m.group(1).split(",")]
_sc = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
_sx = [x for x in _sc["section_appearance"]["fields"]["wallpaper_effect"]["options"] if x != "random"]
check("★ JS 与 schema 的特效清单一致", sorted(_js) == sorted(_sx), f"{_js} vs {_sx}")

print()
print("═══ 燃纸层级（纸必须在**上层**才看得到被烧）═══")
# ⚠️ 踩过：给 .wp-img 设 z-index **没用** —— from/to 分属两个 .wp-stage，
#    跨 stage 的堆叠顺序由 stage 自己决定。判据因此直接查 stage 上的赋值。
check("★ z-index 设在 **stage** 上（不是 .wp-img）",
      "stFrom.style.zIndex" in _b and "from.style.zIndex" not in _b)
check("★ 收尾还原 stage 层级", 'stFrom.style.zIndex = ""' in _b)
check("★ 收尾显式隐藏纸（不只靠动画 fill）", 'from.style.opacity = "0"' in _b)



print()
print("═══ ★★★ 特效函数不得引用未定义的自由变量（本轮真 bug）═══")
# 现场：fxBurn 里用了 `fromId`/`id`（是 _wpTransition 的局部变量）却不在参数里
# ⇒ 运行时 ReferenceError ⇒ 特效完全没效果 + 硬切。
# 这里用**静态近似**：把每个 fx* 的函数体拿出来，找出「既不是参数、
# 也不是体内声明、也不是已知全局」的裸标识符。
_KNOWN = {
    "window","document","Math","Array","Object","Number","String","JSON","Boolean",
    "setTimeout","clearTimeout","setInterval","clearInterval","console","Promise",
    "requestAnimationFrame","cancelAnimationFrame","encodeURIComponent","decodeURIComponent",
    "wpLastDur","wpStage","wpImg","anim","sleep","rand","WP_DUR","WP_EFFECTS",
    "innerWidth","innerHeight","getComputedStyle","performance","Element","undefined",
    "null","true","false","this","isNaN","parseFloat","parseInt","Error","Date","CSS",
    # 模块级 helper
    "wpSettle","wpCleanup","wpCur","wpOther","wpLoad","wpNormalize","wpRimReset",
    "wpBusy","wpActive","wpEffect","wpEnabled","_wpLastFx","wpPendingRestart",
    "startWallpaperRotation","stopWallpaperRotation",
}
_DECL = r"(?:const|let|var|function|class)\s+"
# 每个特效：断言它用到的、形如 id/fromId 这类**明显来自调用方**的名字都在参数里
for _fx in ("fxFade","fxIris","fxWipe","fxStrips","fxZoom","fxInk","fxBurn"):
    _i = HTML.find("function " + _fx)
    if _i < 0:
        continue
    # 函数体：从签名到下一个顶层 function
    _j = HTML.find("\nfunction ", _i + 10)
    _body = HTML[_i:_j if _j > 0 else _i + 9000]
    # 去掉注释，避免注释里的词造成误报
    _body = re.sub(r"/\*[\s\S]*?\*/", "", _body)
    _body = re.sub(r"//[^\n]*", "", _body)
    _sig = re.search(r"function \w+\(([^)]*)\)", _body)
    _params = {x.strip() for x in (_sig.group(1) if _sig else "").split(",") if x.strip()}
    _local = set(re.findall(_DECL + r"([A-Za-z_$]\w*)", _body))
    # ★ 只检查「调用方可能传入」的候选名（id/fromId/to/from/rim 家族），
    #   不做全量分析（那会被字符串与属性名干扰）。
    _suspect = []
    for _name in ("fromId", "id"):
        if re.search(r"(?<![\w.$])" + _name + r"(?![\w$])", _body) and _name not in _params:
            _suspect.append(_name)
    check(f"★★★ {_fx} 不引用未定义的 {_suspect or '调用方变量'}",
          not _suspect,
          f"缺失参数: {_suspect}" if _suspect else "参数齐全")

print()
print("═══ 特效失败必须被单独捕获（不得连累整条切换链）═══")
_i = HTML.find('if (fx === "fade")')
_blk = HTML[max(0, _i - 800):_i + 1200]
check("★★ 调度处对特效调用有 try/catch", "catch (e)" in _blk and "fxFade" in _blk)
check("★★ 失败时退化为淡入（最坏无特效，绝不硬切）",
      "退化为普通淡入" in _blk or "兜底淡入" in _blk)



print()
print("═══ ★★★ 收尾不得让旧图/新图闪现（本轮两个真 bug）═══")
# 现场1：fxBurn 收尾写 `from.style.opacity="0"` 后又 `anim(from,[{opacity:1},...])`
#        ⇒ 动画首帧把 0 拉回 1 ⇒ 旧图重新可见一帧（"燃完出现旧图帧"）
# 现场2：wpSettle 先 wpNormalize(fromId) 后 remove("on")
#        ⇒ 清空 inline opacity 到摘 .on 之间，`.wp-stage.on` 仍让它可见 ⇒ 同症状
# 必须先剥注释：注释里写了 `anim(from,[{opacity:1},...])` 作为反例，
# 不剥掉就会把注释里的反例当成真实代码，判据假红。
_b = re.sub(r"/\*[\s\S]*?\*/", "", JS[JS.find("function fxBurn"):JS.find("function fxInk")])
_b = re.sub(r"//[^\n]*", "", _b)
# 用「收尾」特征定位（第一个 applyMask 是函数定义，不是收尾处）
# 判据：收尾阶段**不得**出现「先设 opacity=0，紧接着又用 opacity:1 起动画」
# 这个组合——那正是"燃完旧图闪现一帧"的成因。用负向断言最稳，不依赖定位。
_no_bounce = re.search(
    r'from\.style\.opacity\s*=\s*"0"[\s\S]{0,120}?anim\(from,\s*\[\{\s*opacity:\s*1',
    _b) is None
check("★★★ 燃纸收尾不得「设 0 后又从 1 起动画」（会闪一帧旧图）", _no_bounce)
check("★ 收尾的 from 动画从 0 开始（保持不变）",
      '{opacity:0},{opacity:0}' in _b)



print()
print("═══ ★★★ 墨染：遮罩必须**真正铺满**（否则收尾靠新图兜底 = 硬切）═══")
# 覆盖率模型：alpha = clamp(2.6*L - 0.8 + bias)，噪声亮度 L ≈ (U+U)/2
_ink = JS[JS.find("function fxInk"):]
_ink = re.sub(r"/\*[\s\S]*?\*/", "", _ink)
_m = re.search(r"BIAS0\s*=\s*(-?[\d.]+)[\s\S]{0,200}?BIAS1\s*=\s*(-?[\d.]+)", _ink)
if _m:
    _b0, _b1 = float(_m.group(1)), float(_m.group(2))
    import random as _r
    _r.seed(7)
    _L = [(_r.random() + _r.random()) / 2 for _ in range(8000)]
    def _cover(bias):
        return sum(1 for Lv in _L
                   if min(1.0, max(0.0, min(1.0, 2.6 * Lv - 0.8) + bias)) > 0.5) / len(_L)
    check(f"★★★ BIAS1={_b1} 必须真正铺满（100%）",
          _cover(_b1) >= 0.995, f"实际 {_cover(_b1)*100:.1f}%")
    check(f"★ BIAS0={_b0} 几乎不覆盖（正确的「一粒墨」起点）",
          _cover(_b0) <= 0.02, f"实际 {_cover(_b0)*100:.1f}%")
else:
    check("★★★ 能找到 BIAS0/BIAS1", False, "未匹配")

check("★★ 墨染 SVG 用视口单位（100% 在容器内解析为 0×0）",
      "width:100vw" in HTML and "height:100vh" in HTML)
check("★★ 墨染 maskUnits=userSpaceOnUse（与内部 100% 单位匹配）",
      'id="wpInkMask" maskUnits="userSpaceOnUse"' in HTML)

print()
if _fail:
    print(f"❌ {len(_fail)} 项未通过: {_fail[:6]}")
    sys.exit(1)
print("🎉 全部通过 —— 壁纸切换不会硬切，燃纸符合要求")
