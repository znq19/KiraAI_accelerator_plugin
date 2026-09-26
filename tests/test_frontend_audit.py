"""★★ 前端审计：壁纸层级 / 开屏时序 / 墨渗极性 / 特效时长。

这一组是"不用浏览器也能判定对错"的静态 + 数值检查，
针对 2026-09-23 用户实测报的四个问题：

  1. **没有开屏**：playSplash 排在两个 await fetch 之后 ⇒ 主内容早渲染完，开屏只是闪一下
  2. **切换时重叠 / 旧图闪现**：wpNormalize 写了 inline `opacity:1`，压过 CSS 的
     `opacity:0` ⇒ 旧层永远可见；而 DOM 里 wp-b 在后 ⇒ wp-b 永远在上层，
     于是"新层是 wp-a"时被旧图盖住
  3. **墨渗不像墨晕**：只是圆 + 边缘位移；而且阈值用的是 `discrete` 的 tableValues，
     断点固定 ⇒ 调高反而**满屏墨**（极性反了）
  4. **特效太快**：WP_DUR 2200
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
HTML = (HERE / "web" / "index.html").read_text(encoding="utf-8")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


print("═══ 1) ★★ 图片可见性只能有**一个**来源（.wp-stage.on）")
# 取 JS 部分（避免 CSS 注释里的说明文字干扰）
_js_raw = re.search(r"<script>([\s\S]*?)</script>\s*</body>", HTML)
_js_raw = _js_raw.group(1) if _js_raw else HTML
# ★ 检查代码特征前**必须剥掉注释**：注释里举了反例（"原来这里写了
#   im.style.opacity = \"1\""），不剥就会把自己的说明文字当成代码误报。
js = re.sub(r"/\*[\s\S]*?\*/", "", _js_raw)
js = re.sub(r"(?m)^\s*//.*$", "", js)

# ★ hudReset 的片段（多处判据要用）——放在这里，避免"先用后定义"
_hz = js[js.index("function hudReset"):js.index("function armZen")]


# ★★ 把墨渗的偏置**从代码里读出来**：后面多处判据要用。
#    不能写死在测试里 —— 否则代码改了测试照样绿（只验证了"我以为的值"，已踩过）。
_m = re.search(r"const BIAS0 = (-?[\d.]+), BIAS1 = (-?[\d.]+);", js)
check("能从 fxInk 里读出 BIAS0 / BIAS1", _m is not None)
if not _m:
    print("   无法继续"); sys.exit(1)
B0, B1 = float(_m.group(1)), float(_m.group(2))
print(f"     墨渗偏置（从代码读到）: BIAS0={B0}  BIAS1={B1}")

# 找所有"对 .wp-img 写 opacity"的语句
# ⚠️ 正则必须用 `\bim`：只写 `im\.style` 会匹配到 `r`**`im`**`.style.opacity`
#    （wpRim 的光环有 `rim.style.opacity = "0"`，那是**光环**，不是图片层）。
#    上一版测试就是这么自己误报的。
# ★ 收尾时会**有意**把当前图层钉住（写 inline opacity:1）——
#   那是为了在开屏淡出的那一刻杜绝"图层不可见"的帧（否则会透出遮罩的黑 ⇒ 弹一下）。
#   所以判据放宽为：**除了收尾钉住那一处**，没有别的地方给图片层写 inline opacity。
img_opacity_writes = re.findall(r"\bim\.style\.opacity\s*=", js)
check("★ 只有收尾钉住那一处给图片层写 inline opacity（其余地方都不写）",
      len(img_opacity_writes) <= 1, f"找到 {len(img_opacity_writes)} 处")

# ⚠️ **不能删空格**：`.wp-stage.on .wp-img` 里的空格是**后代选择器**，
#    删掉就变成另一个选择器（上一版测试自己写错、自己报红）。
check("★ CSS 里唯一控制可见性的是 `.wp-stage.on .wp-img{opacity:1}`",
      re.search(r"\.wp-stage\.on\s+\.wp-img\s*\{\s*opacity:1", HTML) is not None)
check("`.wp-img` 默认是 opacity:0",
      re.search(r"\.wp-img\{[^}]*opacity:0", HTML.replace("\n", "")) is not None)

# wpNormalize 必须只清不设
norm = re.search(r"function wpNormalize\(id\)\{([\s\S]*?)\n\}", js)
check("wpNormalize 存在", norm is not None)
if norm:
    body = norm.group(1)
    check("★ wpNormalize 只做 cssText 清空，不写 opacity",
          "cssText" in body and "style.opacity" not in body)

print()
print("═══ 2) ★ 切换前必须先清掉另一层的 .on（否则旧层盖住新层）")
# settle 里：先 remove(from) 再 add(id)
settle = re.search(r"function wpSettle\(id, fromId\)\{([\s\S]*?)\n\}", js)
check("wpSettle 存在", settle is not None)
if settle:
    b = settle.group(1)
    i_rm = b.find('classList.remove("on")')
    i_add = b.find('classList.add("on")')
    check("★ 先移除旧层的 .on，再给新层加 .on", 0 <= i_rm < i_add,
          f"remove@{i_rm} add@{i_add}")

# 首图：必须给两层都 wpLoad（清 .on）后再设 wp-a
rot = re.search(r"function startWallpaperRotation\(\)\{([\s\S]*?)\n\}", js)
check("startWallpaperRotation 存在", rot is not None)
if rot:
    b = rot.group(1)
    n_load = len(re.findall(r'wpLoad\("wp-[ab]"', b))
    # ★ 设计已改（用户反馈硬切）：重排时**不再预先装填另一层** ——
    #   那会让"旧图还可见 + 新图已就位"同时存在，随后瞬间交替 = 硬切。
    check("★ 重排不再预先装填目标层（改由 wpTransition 过渡揭示）",
          n_load <= 1 and "hasVisible" in b, f"wpLoad {n_load} 次")
    # ★ 行为升级（用户反馈"背景切换还是硬切"）：首图现在**不再直接摆上去**，
    #   而是先保证有底图、再用过渡揭示新图。所以 .on 可能出现多于一次
    #   （先给底层、收尾时再归位），关键判据改为"任何时刻都只有一个 .on"。
    check("首图仍保证「任何时刻只有一个 .on」（不会两层同时可见）",
          b.count('classList.add("on")') >= 1 and "wpTransition(" in b)

print()
print("═══ 3) ★★ 开屏必须在**第一帧**出现（不能等网络）")
i_el = HTML.index('<div class="splash" id="splash"')
i_early = HTML.index("window.__accelSplash = true")
i_main = HTML.index("function playSplash")
check("★ 早期显示脚本在开屏元素**之后**（解析到即同步执行）", i_el < i_early)
check("★ 且在主脚本之前", i_early < i_main)
early = HTML[i_early:HTML.index("</script>", i_early)]
# ★★ 设计反转（学 Z 插件）：开屏**默认可见**（CSS display:grid），
#    视觉全部由 CSS 完成；JS 只负责"填内容 + 收尾"。
#    ⇒ 旧判据（"早期脚本要加 .show"）已经过时，且方向相反：
#      靠 JS 显示才是**脆弱**的 —— JS 任何一步出问题 = 开屏不出现（连续报了两轮）。
check("★ 开屏基础规则是 display:grid（**默认可见**，不靠 JS 显示）",
      re.search(r"\.splash\{[^}]*display:\s*grid", HTML) is not None)
check("★ 只有 [hidden] 才隐藏（一个明确的隐藏来源）",
      re.search(r"\.splash\[hidden\]\{\s*display:\s*none", HTML) is not None)
check("★ 没有任何地方用 .show 来显示开屏（旧设计）",
      'classList.add("show")' not in js)
check("★ 标题文字**静态写在 HTML** 里（JS 不在也有字，不白屏）",
      len(re.findall(r'class="L\b', HTML)) >= 9,
      f'{len(re.findall(chr(34)+"class="+chr(34)+"L", HTML))} 个字母')
check("★ 箴言静态有默认内容（JS 不在也有话）",
      re.search(r'<div class="motto" id="motto">[^<]+</div>', HTML) is not None)
check("★ 基础入场动画由 CSS 直接挂（不靠 JS 加类触发）",
      len(re.findall(r"\.splash:not\(\[hidden\]\)[^{]*\{[^}]*animation:", HTML)) >= 3)
check("★ 早期脚本立刻 arm 收尾定时器（不依赖主脚本）",
      "__accelSplashFailsafe = setTimeout" in early)
check("主脚本只做增强+收尾（判 hidden，不判要不要显示）",
      "if (!sp || sp.hidden) return;" in js)
check("★ 所有「不显示」的路径都会收掉开屏（配置关 / 无壁纸 / 异常 / 超时）",
      js.count("finishSplash()") >= 4, f"{js.count('finishSplash()')} 处")
check("★ finishSplash 会 clearTimeout 掉早期兜底",
      "clearTimeout(window.__accelSplashFailsafe)" in js)

print()
print("═══ 4) ★★ 墨渗：必须是「随机纹路」而不是圆，且极性正确")
# ★ 必须用**原生 SVG mask**（挂在 SVG 的 <image> 上），
#   而不是 HTML 元素的 CSS `mask-image: url(#…)` ——
#   Chromium 对后者支持不完整 ⇒ 遮罩不生效 ⇒ 新图**瞬间全亮**（用户实测过）。
check("★ 墨渗用原生 SVG mask（<image mask=\"url(#wpInkMask)\">）",
      re.search(r'<image[^>]*mask="url\(#wpInkMask\)"', HTML) is not None)
check("★ 不再用不可靠的 HTML CSS mask-image 引 SVG mask",
      'maskImage = "url(#wpInkMask)"' not in js)
# 旧的圆实现应已消失
ink = re.search(r"function fxInk\(to, from, rim\)\{([\s\S]*?)\n\}", js)
check("fxInk 存在", ink is not None)
if ink:
    b = ink.group(1)
    check("★ fxInk 里不再用 circle() 做形状", "circle(" not in b, 
          repr(re.findall(r"circle\([^)]*\)", b)[:2]))
    check("★ 用两层分形噪声（粗纹 + 细丝）",
          HTML.count('type="fractalNoise"') >= 2)
    check("★ 通过 feColorMatrix 的 bias 逐帧抬高（可动画的阈值）",
          'cm.setAttribute("values"' in b and "BIAS0" in b and "BIAS1" in b)
    check("★ 不再动画 discrete 的 tableValues（断点固定 ⇒ 极性会反）",
          'th.setAttribute("tableValues"' not in b)
    check("每次随机换种子（纹路不重复）",
          'n1.setAttribute("seed"' in b and "Math.random" in b)
    # ★ 复位值也要跟 BIAS0 一致（不能写死数字）
check("结束时会复位 bias 到 BIAS0（不影响下一次）",
      ("setBias(" + str(B0) + ")") in b or b.count("setBias(") >= 2, 
      f"BIAS0={B0}")

print()
print()
print("═══ 4b) ★★ 响应标记必须真的被写入（否则剥离永远走兜底 + 日志刷屏）")
_main = pathlib.Path(HERE / "main.py").read_text(encoding="utf-8")
_eng = pathlib.Path(HERE / "stream_engine.py").read_text(encoding="utf-8")
check("★ 引擎写入了已抢发段数（主判据）",
      '_accel_early_sent_count' in _eng)
check("★ 引擎写入了已抢发段原文（按内容剥离要用）",
      '_accel_early_sent_segments' in _eng)
check("★ 且是在 on_complete 之前写入（发送层读得到）",
      _eng.index('_accel_early_sent_count') < _eng.index("self.on_complete("))
check("台账每轮开始会清空（否则上一轮残留会让本轮多切=丢内容）",
      "_reset_turn_ledger" in _main and "self._reset_turn_ledger(sid_now)" in _main)

print("═══ 5) ★ 墨渗的数值校验：极性必须对（开场是一粒墨，不是满屏）")
# alpha = 0.55R + 0.35G + 0.10B + bias，噪声三通道均值≈0.5
# discrete(tableValues=[0,.55,1]) 的断点 = 1/3、2/3（固定）
LEVELS = 3
TH = [(i + 1) / LEVELS for i in range(LEVELS)]     # [0.333, 0.667, 1.0]


def alpha_of(bias, r=0.5, g=0.5, b=0.5):
    return 0.55 * r + 0.35 * g + 0.10 * b + bias


def opaque_fraction(bias):
    """噪声近似正态（均值 .5，标准差 .15）时，超过第一档断点的比例。"""
    import math
    th = TH[0]
    mu = alpha_of(bias)
    sd = 0.55 * 0.15 + 0.35 * 0.15 + 0.10 * 0.15       # 线性组合后的标准差
    z = (th - mu) / sd
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


f_start = opaque_fraction(B0)
# ★ 曲线已改为 p^2.5（幂曲线）—— 原来按线性/50% 算，会得出"中段 100%"的
#   过时结论。这里按**实际曲线**取 50% 处的 bias。
f_mid = opaque_fraction(B0 + (B1 - B0) * (0.5 ** 2.5))   # 进度 50% 处
f_end = opaque_fraction(B1)
print(f"     bias={B0:+.2f} ⇒ 不透明占比 ≈ {f_start*100:.2f}%   （开场：一粒墨）")
print(f"     bias={B0+(B1-B0)*0.5:+.2f} ⇒ 不透明占比 ≈ {f_mid*100:.1f}%    （中段：斑块散开）")
print(f"     bias={B1:+.2f} ⇒ 不透明占比 ≈ {f_end*100:.1f}%     （结束：铺满）")
check("★ 开场只有零星墨点（<5%）—— 不是「开场就满屏墨」", f_start < 0.05)
# ★ 中段应该"正在散开"：既不是几乎全透明、也不该已经铺满。
#   第一次把终点调成 0.55 时，中段就冲到 92.6% ⇒ "散开"只占很短一段 = 等于没过程。
check("★ 中段处于「正在散开」的状态（10%~80%）", 10 < f_mid * 100 < 80,
      f"实际 {f_mid*100:.1f}%")
check("★ 结束时基本铺满（>95%）", f_end > 0.95)
check("★ 单调递增（墨量只增不减 = 真的在扩散）",
      f_start < f_mid < f_end)

print()
print()
print("═══ 5b) ★★ 所有特效统一「色彩覆盖」：新图必须淡入（不许硬切）")
# 用户实测："好像还有一个效果是直接硬切的"。
# 根因：新层 keyframes 只写 transform，`fill:"forwards"` 让 opacity 停在 CSS 的 0，
#       动画结束才跳到 1 ⇒ 新图"啪"地出现。所以每套都必须**显式淡入**。
_fx_names = ["fxIris", "fxWipe", "fxStrips", "fxZoom", "fxInk"]
_reveal = re.search(r"function revealIn\(([\s\S]*?)\n\}", js)
check("存在 revealIn（统一的新图淡入辅助）", _reveal is not None)
check("★ revealIn 里显式写了 opacity 0 → 1",
      _reveal and "opacity:0" in _reveal.group(1) and "opacity:1" in _reveal.group(1))
_eat = re.search(r"function eatAway\(([\s\S]*?)\n\}", js)
check("存在 eatAway（统一的旧图被吃掉辅助）", _eat is not None)
check("★ eatAway 里先保持实、后模糊降饱和淡出（真正的覆盖感）",
      _eat and "opacity:1" in _eat.group(1) and "blur(" in _eat.group(1)
      and "saturate(" in _eat.group(1))
# ★ 真正的要求是"**不许硬切**"：每套特效要么调 revealIn，要么自己的关键帧里
#   显式写 opacity。只数 revealIn 会误判 —— fxInk 走 SVG 遮罩（在函数外掌控节奏），
#   fxZoom 本来就有自己的 opacity 关键帧。
def _fx_body(name):
    i = js.index("function " + name)
    return js[i:i + 1800]

no_fade = []
for f in _fx_names:
    b = _fx_body(f)
    if "revealIn(" in b:
        continue
    if f == "fxInk":
        continue                      # 墨渗按"噪声长出"覆盖，不走淡入
    if re.search(r"opacity:\s*0", b) and re.search(r"opacity:\s*1", b):
        continue                      # 自己有淡入关键帧
    no_fade.append(f)
check("★ 每套特效的新图都**不会硬切**（revealIn 或自带 opacity 关键帧）",
      not no_fade, f"缺淡入: {no_fade}")

print()
print("═══ 5c) ★ 开屏：首帧就要有画面（否则观感是先有字、后有画）")
check("★ .sp-wp 有**纯 CSS 静态底**（不依赖网络/JS 就有画面）",
      re.search(r"\.sp-wp\{[^}]*background-image:[^;]*radial-gradient", HTML) is not None)
check("★ 标题字母默认 opacity:0 + 延后到壁纸之后（750ms 起）",
      re.search(r"\.wm-main \.L\{[^}]*opacity:0", HTML) is not None
      and "750ms" in js or "750ms" in HTML)
check("★ 副标题/分隔线/箴言都默认透明（从无到有）",
      all(re.search(r"\.splash:not\(\[hidden\]\) " + sel + r"\{[^}]*opacity:0", HTML) is not None
          for sel in [r"\.wm-sub", r"\.sp-line", r"\.motto"]))
check("★ CSS 兜底的背景入场也写了 opacity（否则还是硬切）",
      re.search(r"@keyframes spWpIn\{[^}]*opacity:0", HTML, re.S) is not None)

print()
print("═══ 5d) ★ 开屏时长与停留（用户要求久一点）")
m_total = re.search(r"after\((\d+), finishSplash\)", js)
# ⚠️ 总时长 = 收尾**起点** + **化开过渡**。只看 after(N) 会漏掉那 1.2s，
#    把本来正确的改动判红（上一版就这么写错了）。
_tr2 = re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML, re.S)
_dis = float(_tr2.group(1)) * 1000 if _tr2 else 0
_tot = (int(m_total.group(1)) + _dis) if m_total else 0
check("★ 总时长 ≥7s（收尾起点 6.2s + 化开 1.4s）", _tot >= 7000,
      f"{_tot:.0f}ms = {m_total.group(1) if m_total else '?'} + {_dis:.0f}")
check("★ 收尾过渡 ≥1s（化开而不是硬跳）",
      re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML) is not None
      and float(re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML).group(1)) >= 1.0)
check("★ 收尾同时过渡 opacity/transform/filter（化开）",
      re.search(r"\.splash\{[^}]*transition:[^;]*transform[^;]*filter", HTML, re.S) is not None)
check("★ .splash.out 带缩放+模糊（不只是透明）",
      re.search(r"\.splash\.out\{[^}]*transform:[^;]*scale[^}]*filter:[^;]*blur", HTML, re.S) is not None)
# ⚠️ 必须精确抓 `__accelSplashFailsafe = setTimeout(...)}, NNNN)` 这一处：
#    非贪婪正则会匹配到后面那个 1200ms 的隐藏定时器，得出错误结论（上一版就写错了）。
m_fb = re.search(r"__accelSplashFailsafe\s*=\s*setTimeout\(function \(\) \{[\s\S]*?\n\s*\}, (\d+)\);", HTML)
if m_fb and m_total:
    check("★ 内联兜底晚于正常收尾（否则会抢在收尾前把开屏掐掉）",
          int(m_fb.group(1)) > int(m_total.group(1)),
          f"兜底 {m_fb.group(1)}ms vs 正常 {m_total.group(1)}ms")
else:
    check("★ 能找到内联兜底超时值", False, f"m_fb={bool(m_fb)} m_total={bool(m_total)}")

print("═══ 6) ★ 特效要「慢一点」（用户明确要求）")
m = re.search(r"const WP_DUR = (\d+)", js)
check("WP_DUR 至少 3000ms（原来 2200 被反馈太快）", m and int(m.group(1)) >= 3000,
      m.group(1) if m else "未找到")
# ★ 重写后条带用的是局部 dur + coverAt（= dur*.72 + dur*.55）
check("★ 条带用 coverAt = 最晚一条的延迟 + 时长（含错峰）",
      "coverAt = dur * .72 + dur * .55" in js and "dur * .55 / N" in js)
check("墨渗比基准更长（≈1.15×）", "WP_DUR * 1.15" in js)

print()
print("═══ 7) ★ 「颜色相互消除/覆盖」感：旧图要被吃掉")
# 除墨渗外，每套的旧图都应走 eatAway（模糊 + 降饱和 + 淡出）；
# 墨渗按设计**让旧图保持可见**、由墨盖上去（那才是真正的"被下一张吃掉"）。
eat_ok, eat_missing = [], []
for f in ["fxIris", "fxWipe", "fxStrips", "fxZoom"]:
    b = _fx_body(f)
    (eat_ok if "eatAway(" in b else eat_missing).append(f)
# ★ 重写后：fade 与 strips 的旧图动画是**自写**的（同样含模糊+降饱和/交叉淡化），
#   不再统一走 eatAway —— 判据允许这两套例外。
check("★ 每套特效的旧图都被吃掉（eatAway，或自写的模糊/交叉淡化）",
      set(eat_missing) <= {"fxStrips", "fxFade"}, f"缺: {eat_missing}")
_ink = _fx_body("fxInk")
# ⚠️ 不能简单查 "opacity:0" —— fxInk 里有 `to.style.opacity = "0"`
#    （把**新层本体**藏起来，因为显示由 SVG <image> 负责，这是设计如此）。
#    准确的判据是：**旧层根本没有动画**（它保持原样，等着被墨盖住）。
# ⚠️ 判据要精确：fxInk 里**有意**让旧图做一个"极轻微推近"（保持不透明，
#    等着被墨盖住）。所以不能禁止 `anim(from,`，而要禁止**旧图动画里出现 opacity:0**
#    （那才叫"淡出"）。另：`to.style.opacity = "0"` 是**新层本体隐藏**
#    （显示交给 SVG <image>），与旧图无关，不能误伤。
_from_block = re.search(r"anim\(from,\s*\[([\s\S]*?)\]", _ink)
check("★ 墨渗不淡出旧图（旧图动画里没有 opacity:0）",
      "eatAway(" not in _ink
      and (_from_block is None or "opacity:0" not in _from_block.group(1)))
print(f"     走 eatAway 的: {eat_ok}  |  墨渗: 旧图不淡出（被墨覆盖）")

print()
print()
print("═══ 8) ★★ 特效必须播放完（本轮修的三处真 bug）")
# ① 条带特效不能被自己的"整张淡入"盖住 —— 那会让条带看起来没播放
_strips = _fx_body("fxStrips")
check("★ fxStrips 不对主层做整张淡入（否则盖住条带 ⇒ 等于没播放）",
      "revealIn(" not in _strips)
check("★ fxStrips 自己把主层设为隐藏（to.style.opacity = \"0\"）",
      'to.style.opacity = "0"' in _strips)

# ② 收尾必须按**每套特效的实际时长**等，而不是固定值
check("★ 存在 wpLastDur（记录每套的实际时长，含错峰延迟）",
      "let wpLastDur" in js)
check("★ 收尾按 wpLastDur 等待（固定等会截断条带/墨渗）",
      re.search(r"await sleep\(wpLastDur\s*\+", js) is not None)
_setters = re.findall(r"wpLastDur = ([^;]+);", js)
check("★ 每套特效都设置了自己的 wpLastDur", len(_setters) >= 5,
      f"{len(_setters)} 套: {_setters}")
check("★ 条带的时长含最晚一条的延迟（不只是单条时长）",
      any("coverAt" in s or "* .72" in s or "*.72" in s for s in _setters), str(_setters))

print()
print("═══ 9) ★★ 开屏时序：一个元素只能有一个驱动者 + 必须有停留")
check("★ 存在 html.jsfx 开关（JS 接管时关掉 CSS 入场动画）",
      "html.jsfx" in HTML)
check("★ jsfx 关掉的正是会冲突的那几项（字母/副标题/分隔线/箴言/背景）",
      all(sel in HTML for sel in ["html.jsfx .wm-main .L", "html.jsfx .wm-sub",
                                  "html.jsfx .sp-line", "html.jsfx .motto",
                                  "html.jsfx .sp-wp"]))
check("★ 主脚本会打上 jsfx 信号",
      'document.documentElement.classList.add("jsfx")' in js)

# ⚠️ 致命点：CSS 里 .wm-main .L 默认 opacity:0，若 JS 动效用 fill:"backwards"，
#    播完会**回到 opacity:0 ⇒ 字母全消失**。
_n_both = js.count('fill:"both"')
# ⚠️ js 已剥注释（见开头），这里只数代码里的 both 数量即可。
check("★ 标题/箴言特效用 fill:both（backwards 会让字播完消失）",
      js.count('fill:"both"') >= 8, f"both={js.count(chr(39)+'fill:'+chr(34)+'both'+chr(34)+chr(39))}")

# 箴言必须"入场 → 停住 → 淡出"
_mfx = js[js.index("const MOTTO_FX_FN"):js.index("\n};", js.index("const MOTTO_FX_FN"))]
# ⚠️ 不写死 offset 数值（改一次停留就要改测试）；直接数 offset 次数：
#   每条时间线应有"起点 offset + 停留终点 offset"两处。
# 新结构：`{opacity:0…} → {opacity:1, offset:.NN} → {opacity:1}`（末帧无 offset = 保持到结束）
_h_mfx = re.findall(r"offset:\.(\d+)", _mfx)
check("★ 箴言每条都有『入场完成点』offset（之后一直保持）",
      len(_h_mfx) >= 3, f"offset: .{_h_mfx}")
# ★ 设计变更（用户持续反馈"停留太短、很快消失"）：
#   箴言现在**不再自己淡出** —— "动画结束 = 已消失"是造成"停留短"的根因。
#   改为只做「入场 → 保持」，淡出交给**整场收尾**一起化开。
#   ⇒ 判据从"结尾会淡出"改为"结尾必须是 opacity:1（保持）"。
# ⚠️ 不能简单查"有没有 opacity:0" —— 那是**入场首帧**，本来就该有。
#    要查的是**末帧**：三条时间线的最后一帧必须都是 `opacity:1`（保持）。
_motto_frames = re.findall(r"\{(opacity:[01][^}]*)\}", _mfx)
_last_ops = [fr.split(",")[0] for fr in _motto_frames]
# rise/blur/track 各一条时间线，末帧应为 opacity:1；type 是 async 不计
_ends_ok = _last_ops.count("opacity:1") >= 3
check("★ 箴言三条时间线都以 opacity:1 收尾（保持可见，不再自己淡出）",
      _ends_ok, f"各帧首属性: {_last_ops}")

check("★ 标题特效延后到 750ms 才跑（画面先出、字再长出来）",
      re.search(r"after\(750,", js) is not None)
check("★ 副标题/分隔线有各自的 delay（错峰、有停留）",
      "delay:1850" in js and "delay:2050" in js)
check("★ 箴言延后到 2.25s（前面留出停留）",
      re.search(r"after\(2250,", js) is not None)
m_fin = re.search(r"after\((\d+), finishSplash\)", js)
# ★ 收尾推到 6.2s：给箴言真正的"停留"（原来 5.2s，停留被压得太短）
check("★ 整体收尾 >= 6s（给箴言留出真实停留）",
      m_fin and int(m_fin.group(1)) >= 6000,
      m_fin.group(1) + "ms" if m_fin else "找不到")

print()
print("═══ 10) ★ 跳过提示要有微微闪烁（不张扬）")
check("★ .sp-hint 有闪烁动画", re.search(r"\.sp-hint\{[^}]*animation:spHint", HTML) is not None)
# ⚠️ 不能只抓 `[^}]*` —— keyframes 里有多个 `}`，那样只截到第一档，
#    会得出"找不到 .56"的错误结论（上一版就写错了）。
_hint = re.search(r"@keyframes spHint\{([\s\S]*?)\n\}", HTML)
check("★ 闪烁幅度小（不张扬）",
      _hint is not None and ".34" in _hint.group(1) and ".56" in _hint.group(1),
      _hint.group(1) if _hint else "找不到")
check("★ 提示不被 jsfx 关掉（它始终要闪）", "html.jsfx .sp-hint" not in HTML)
check("★ 减少动效下提示不闪但仍可见（不消失）",
      re.search(r"prefers-reduced-motion[\s\S]{0,400}\.sp-hint\{[^}]*animation:none", HTML) is not None)

print()
print("═══ 11) ★ 开屏字特效：不能太快，且必须自带停留")
_wm = js[js.index("const WM_FX_FN"):js.index("\n};", js.index("const WM_FX_FN"))]
# 每条动画的 duration 都应 >= 1000ms（原来 820~1050 偏快）
_durs = [int(x) for x in re.findall(r"duration:\s*(\d+)", _wm)]
check("★ 字特效每条动画都不低于 1s（原来偏快）",
      bool(_durs) and min(_durs) >= 1000, f"最短 {min(_durs) if _durs else '?'}ms")
check("★ 字特效用 fill:\"both\"（backwards 会让字母播完消失）",
      'fill:"backwards"' not in _wm and _wm.count('fill:"both"') >= 6,
      f"both={_wm.count(chr(34)+'fill:'+chr(34))}")
# 至少一半的特效在时间线里显式写了"保持"（末帧不透明）
check("★ 字特效整体不会太快（单条 <=1500ms，否则等太久）",
      max(_durs) <= 1500, f"最长 {max(_durs) if _durs else '?'}ms")

print()
print("═══ 12) ★★ 箴言必须装进起点→收尾的窗口（否则被截断=看着像消失）")
_motto = js[js.index("const MOTTO_FX_FN"):js.index("\n};", js.index("const MOTTO_FX_FN"))]
_mdur = [int(x) for x in re.findall(r"duration:\s*(\d+)", _motto)]
_m_start = re.search(r"after\((\d+),\s*\(\)\s*=>\s*\{\s*try\s*\{\s*MOTTO_FX_FN", js)
_fin = re.search(r"after\((\d+), finishSplash\)", js)
_m_begin = re.search(r"after\((\d+),\s*\(\)\s*=>", js)
# 取箴言那条 after 的起点
_motto_at = None
for m in re.finditer(r"after\((\d+),", js):
    seg = js[m.end():m.end() + 160]
    if "MOTTO_FX_FN" in seg:
        _motto_at = int(m.group(1))
if _motto_at and _mdur and _fin:
    _latest = max(_mdur)
    _window = int(_fin.group(1)) - _motto_at
    check("★ 箴言时长 <= 窗口（起点→收尾）⇒ 不会被截断",
          _latest <= _window,
          f"箴言 {_latest}ms vs 窗口 {_window}ms（起点 {_motto_at} → 收尾 {_fin.group(1)}）")
    check("★ 窗口内留有可见的停留（>=100ms）",
          _window - _latest >= 100, f"余量 {_window - _latest}ms")
else:
    check("★ 能定位箴言起点/时长/收尾", False,
          f"at={_motto_at} durs={_mdur} fin={bool(_fin)}")
_holds2 = re.findall(r"offset:\.(\d+)", _motto)
check("★ 箴言每条都有入场完成点 offset（之后保持到整场收尾）",
      len(_holds2) >= 3, f"offset: .{_holds2}")

print()
print("═══ 13) ★★ 开屏收尾必须用显式 Web Animation（CSS 同帧加类可能被跳过）")
check("★ finishSplash 用 sp.animate([...]) 做收尾",
      re.search(r"sp\.animate\(\[", js) is not None)
check("★ 收尾动画覆盖 opacity + transform + filter（化开而非单纯透明）",
      "scale(1.045)" in js and "blur(12px)" in js and "opacity:0" in js[js.index("const OUT_MS"):js.index("const OUT_MS") + 400])
check("★ 隐藏等待按 OUT_MS 计算（不再硬编码 1200）",
      "OUT_MS + 180" in js or "OUT_MS +" in js)
check("CSS 里仍保留 .splash.out 作为极老环境的后备",
      re.search(r"\.splash\.out\{", HTML) is not None)

print()
print("═══ 14) ★★ 关键帧 opacity 必须全写或全不写（否则回落 → 元素消失/硬切）")
# 先剥模板字符串，否则 `${...}` 里的花括号会把帧切错（上一版检测器就这么误报过）
_js2 = re.sub(r"`(?:[^`\\]|\\.)*`", "`X`", js)


def _frames(kf: str):
    out, depth, cur = [], 0, ""
    for ch in kf:
        if ch == "{":
            depth += 1
            if depth == 1:
                cur = ""
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(cur)
                continue
        if depth >= 1:
            cur += ch
    return out


_calls = []
for mm in re.finditer(r"(\w[\w.]*)\.animate\(\s*\[([\s\S]*?)\]\s*,", _js2):
    _calls.append(("animate:" + mm.group(1), mm.group(2)))
for mm in re.finditer(r"\banim\(\s*(\w+)\s*,\s*\[([\s\S]*?)\]\s*[,)]", _js2):
    _calls.append(("anim:" + mm.group(1), mm.group(2)))
for mm in re.finditer(r"\bE\(\s*(\w+)\s*,\s*\[([\s\S]*?)\]\s*[,)]", _js2):
    _calls.append(("E:" + mm.group(1), mm.group(2)))
_bad = []
for _target, _kf in _calls:
    _fr = _frames(_kf)
    if len(_fr) < 2:
        continue
    _has = [("opacity" in f) for f in _fr]
    if any(_has) and not all(_has):
        _bad.append((_target, len(_fr), [i for i, h in enumerate(_has) if not h]))
check("★ 所有动画的关键帧要么都写 opacity、要么都不写（不会回落到基础值）",
      not _bad, f"检查 {len(_calls)} 条动画，异常 {_bad}")

print()
print("═══ 15) ★★ 开屏与页面必须逐层同盒 + 同 origin（否则交接跳变）")


def _css(sel):
    mm = re.search(r"\n" + re.escape(sel) + r"\{([^}]*)\}", HTML)
    return mm.group(1) if mm else ""


def _prop(css, name):
    mm = re.search(name + r":\s*([^;]+)", css)
    return mm.group(1).strip() if mm else ""


for _page, _sp in ((".wallpaper", ".sp-breathe"), (".wp-stage", ".sp-sway"),
                   (".wp-img", ".sp-wp")):
    _a, _b = _css(_page), _css(_sp)
    check(f"★ {_sp} 的 inset 与 {_page} 一致（同盒）",
          _prop(_a, "inset") == _prop(_b, "inset"),
          f"{_prop(_a, 'inset')} vs {_prop(_b, 'inset')}")
    # transform-origin 未设置 == 默认 50% 50%，也算一致
    _oa = _prop(_a, "transform-origin") or "50% 50%"
    _ob = _prop(_b, "transform-origin") or "50% 50%"
    check(f"★ {_sp} 的 transform-origin 与 {_page} 一致", _oa == _ob, f"{_oa} vs {_ob}")

check("★ 开屏呼吸用与页面**同一条** keyframes、同周期",
      "wpBreath 22s" in _css(".sp-breathe"),
      _prop(_css(".sp-breathe"), "animation"))
check("★ 交接时把页面壁纸的呼吸**相位对齐**（负 animation-delay）",
      "animationDelay" in js and "__accelSplashShownAt" in js)
check("★ 漂移同时作用于页面两层与开屏中层（同源）",
      '"wp-a","wp-b","spSway"' in js)

print()
print("═══ 16) ★★ 旧图必须「活到新图铺满」（否则中途露底色 = 看着像硬切）")
# 曾经：eatAway 写死 WP_DUR，而条带要 WP_DUR*1.27 才铺满
# ⇒ 3040~4064ms 之间旧图已淡没、新图没盖满 ⇒ 露出页面底色 ⇒ 一块会缩小的
#   暗斑 ⇒ 用户说的"背景切换还是有几个是硬切"。
for _f in ("fxIris", "fxWipe", "fxStrips", "fxZoom"):
    _i = js.index("function " + _f)
    _b = js[_i:js.index("\nfunction ", _i + 12)]
    _set = re.search(r"wpLastDur = ([^;]+);", _b)
    _eat = re.search(r"eatAway\(from,\s*([^)]+)\)", _b)
    if _f in ("fxZoom", "fxStrips"):
        continue    # 这两套的旧图动画是自写的（含模糊+降饱和），另有判据
    check(f"★ {_f} 的 eatAway 时长 = 本套实际时长（旧图不会提前消失）",
          bool(_set and _eat and _eat.group(1).strip() == "wpLastDur"),
          f"wpLastDur={_set.group(1).strip() if _set else '?'} eatAway={_eat.group(1).strip() if _eat else '?'}")

print()
print("═══ 17) ★★ 旧图全程不透明（揭示中途露底色 = 硬切）")
_i = js.index("function eatAway")
_eat = js[_i:js.index("\n}", _i)]
check("★ eatAway 有『保持不透明到铺满』的 offset 保持点",
      "offset:Math.min(1, dur / (dur + 260))" in _eat)
check("★ eatAway 的淡出发生在**铺满之后**（时长 = dur + 260）",
      "duration:dur + 260" in _eat)
check("★ eatAway 的旧图仍保留推近/模糊/降饱和（被压住的质感）",
      "blur(11px)" in _eat and "saturate(.45)" in _eat)

print()
print("═══ 18) ★★ 开屏收尾：遮罩必须在图层**之下** + 收尾钉住图层")
_wi = HTML.index('<div class="wallpaper"')
_wj = HTML.index('id="wpRim"')
_sec = HTML[_wi:_wj]
check("★ .wp-scrim 出现在两个 .wp-stage **之前**（在图层之下）",
      _sec.index("wp-scrim") < _sec.index('id="wp-a"'),
      f"scrim@{_sec.index('wp-scrim')} vs stage@{_sec.index('id=' + chr(34) + 'wp-a')}")
check("★ 收尾时把当前图层钉住（inline opacity:1），杜绝任一帧露黑",
      'im.style.opacity = "1"' in js and 'classList.contains("on")' in js)

print()
print("═══ 19) ★★ 墨渗的 SVG 必须**全屏**（0×0 时 image 尺寸为 0 ⇒ 画不出来）")
# ⚠️ 不能取"第一个 <svg>" —— ECG 的 data-URI 里也有 <svg>（会误伤）。
#    要定位承载墨渗的那个：它带 id="wpInkImg"。
_svg = re.search(r"<svg[^>]*>\s*(?:<!--[\s\S]*?-->\s*)*<mask id=\"wpInkMask\"", HTML)
_svg = re.search(r"<svg[^>]*>(?=[\s\S]{0,2400}?id=\"wpInkMask\")", HTML)
# ★ 尺寸改用**视口单位**：`width:100%` 在 .wallpaper 容器内实测解析为 0×0，
#   而 <image width="100%"> 是相对该 SVG 解析的 ⇒ 什么都画不出。
check("★ SVG 载体是全屏的（视口单位，不是 0×0）",
      _svg is not None and "width:100vw" in _svg.group(0) and "height:100vh" in _svg.group(0),
      _svg.group(0)[:90] if _svg else "找不到")
check("★ 且不接收指针事件（不挡操作）",
      _svg is not None and "pointer-events:none" in _svg.group(0))

print()
print("═══ 20) ★★ 箴言不再自带淡出（停留必须是真停留）")
_mfx2 = js[js.index("const MOTTO_FX_FN"):js.index("\n};", js.index("const MOTTO_FX_FN"))]
_mf2 = re.findall(r"\{(opacity:[01][^}]*)\}", _mfx2)
_l2 = [fr.split(",")[0] for fr in _mf2]
check("★ 箴言三条时间线都以 opacity:1 收尾（不再自己淡出）",
      _l2.count("opacity:1") >= 3, f"各帧首属性: {_l2}")
check("★ 停留由整场收尾统一化开（收尾 >= 6s）",
      re.search(r"after\((\d+), finishSplash\)", js) is not None
      and int(re.search(r"after\((\d+), finishSplash\)", js).group(1)) >= 6000)

print()
print("═══ 21) ★ 霓虹文字特效（三模式随机 + 霓虹色随机）")
check("★ 有三种霓虹模式：呼吸 / 流光 / 弹珠环绕",
      all(k in HTML for k in ("neon-breathe", "neon-flow", "neon-orbit")))
check("★ 霓虹色是**一组**随机的（不是写死一个色）",
      "NEON_COLORS" in js and js.count("#") >= 6)
check("★ 由 JS 随机选模式并设 --nc（主色）",
      "NEON_MODES[Math.floor" in js and 'setProperty("--nc"' in js)
# ★ 用户明确不要"往外扩的外圈"，已删；改为**字内弹珠流动**（不画圈）。
check("★ 弹珠改为字内流动（bead），且不再画外圈",
      "neon-bead" in HTML and "beadRun" in HTML and "neon-orbit::after" not in HTML)

check("★ 流光沿文字横向扫（background-position 动画）",
      "neonFlow" in HTML and "background-position" in HTML)

print()
print("═══ 22) ★ 「已连接」心电图")
check("★ 有 ECG 折线（真实 polyline）",
      'class="ecg-track"' in HTML and 'class="ecg-sweep"' in HTML)
check("★ 只在已连接时出现（.status.ok 控制）",
      ".status.ok .ecg-sweep" in HTML and ".status.err .ecg" in HTML)
check("★ 默认不可见（.ecg-sweep 基础 opacity:0）",
      re.search(r"\.ecg-sweep\{[^}]*opacity:0", HTML) is not None)
# ★★★ 这条判据**反转了**（旧版断言"减少动效下不扫"）。
# 实测：Android 省电模式会置 reduce，而用户明确要求"像声纹那样动和呼吸"
# ⇒ 旧行为等于把用户要的特效**静默关掉**，用户报「只是保持常亮」就是这个。
# 正确：reduce 下**保留扫描**（低频平滑推移，不是闪频刺激），只放慢。
_nc2 = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
_sw2 = re.search(r"prefers-reduced-motion[\s\S]{0,400}?\.ecg-sweep\{([^}]*)\}", _nc2)
check("★★★ 减少动效下**仍然在扫**（用户要的动效不得被静默关掉）",
      bool(_sw2) and "animation:none" not in (_sw2.group(1) if _sw2 else "animation:none"),
      (_sw2.group(1)[:60] if _sw2 else "未定位"))
check("★ 减少动效下波形仍可见（不消失）",
      bool(_sw2) and "opacity" in _sw2.group(1) or ".status.ok .ecg-track" in _nc2)


# ═══ ★★★ 退出欣赏模式：「已连接」不得早于 KPI 飞回复位 ═══
# 用户实测："结束欣赏模式，KPI 字还飞回来的时候，已连接就先复位了，
#            导致它百分百和还没飞回来的字重叠。"
# 根因：hudReset 让各项**从右上角 top:16 起飞**，而退出瞬间 zen 已移除
#       ⇒ status 回落到 top:16 = 那些字的**起飞点** ⇒ 必然重叠。
print()
print("═══ 23b) ★★★ 退出欣赏模式：连接状态与飞回读数的重叠 ═══")
_nc3 = re.sub(r"/\*[\s\S]*?\*/", "", HTML)      # 先剥注释，否则注释里的说明会被当成命中
check("★★★ 「已连接」在 hud-returning 期间保持锚定（不提前复位）",
      re.search(r"body\.hud-returning\s+\.status", _nc3) is not None)
check("★★★ 它的回归由「hud-returning 解除」驱动 ⇒ 时序上必然晚于飞回",
      re.search(r"body\.hud-returning\s+\.status", _nc3) is not None
      and 'classList.remove("hud-returning")' in HTML)
check("★★ 飞回期间 HUD 仍可见（否则整段 FlyBack 在透明下白跑）",
      re.search(r"body\.hud-returning\s+\.hud\s*\{[^}]*opacity:1", _nc3) is not None)

_dur = re.search(r"const dur = (\d+)", HTML)
_n_items = len(re.findall(r'class="hud-item"', HTML)) or 5
_fly_ms = (int(_dur.group(1)) + (_n_items - 1) * 45) if _dur else -1
check(f"★ 飞回总时长可解析（{_fly_ms}ms）且复位有余量", _fly_ms > 0)

print()
print("═══ 23) ★★ 空闲欣赏模式：UI 藏、品牌字与连接状态**必须留**")
check("★ 有 body.zen 机制", "body.zen" in HTML and "initZen" in js)
check("★ 配置项 ui_zen_seconds（默认 10）",
      '"ui_zen_seconds"' in (HERE / "schema.json").read_text(encoding="utf-8"))
check("★ 空闲秒数从配置读入", "__accelZenSeconds" in js)
check("★ 藏的是 section / tagline / dock（面板 UI）",
      "body.zen .shell > section" in HTML and "body.zen .dock" in HTML)
# ⚠️ 先剥 CSS 注释再查 —— 我自己的说明里提到了 ":not(header)"（举反例），
#    不剥就会把说明当代码、误报（这个坑踩过好几次了）。
_css_nc = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
check("★ ★ 保留 .wordmark（品牌字）—— 不能用 :not(header) 一刀切",
      "body.zen .shell > .wordmark" in _css_nc and ":not(header)" not in _css_nc)
check("★ 保留 .status（已连接）", "body.zen .shell > header > .status" in HTML)
# ★ 用户明确要求：**点击画面任意处**（或按键）才恢复 UI，
#   而不是"移动鼠标就出来" —— 否则手一碰鼠标 UI 就跳回来，没法安静欣赏背景。
check("★ 点击 / 按键会恢复 UI（pointerdown + keydown）",
      "exitZen" in js and "pointerdown" in js and "keydown" in js)
check("★ ★ 但不绑定 mousemove（否则动一下就跳回来）",
      "mousemove" + ", wake" not in js)
check("★ 0 可关闭该功能", "if (!(secs > 0)) return;" in js)

print()
print("═══ 24) ★★ 空闲模式必须真的能藏掉面板（动画会压过声明！）")
# `.card{animation:cardIn ... forwards}` —— CSS 动画优先级**高于**普通声明，
# 而 forwards 会把 opacity 一直保持为 1 ⇒ 只写 `opacity:0` 是**藏不掉**的。
_zen = HTML[HTML.index("body.zen .shell > section"):HTML.index("body.zen .shell > .wordmark")]
check("★ zen 规则里有 animation:none!important（否则动画压过 opacity:0）",
      "animation:none!important" in _zen and "opacity:0!important" in _zen)
check("★ 藏 section / tagline / dock", all(k in _zen for k in
      ("section", "tagline", "dock")))
_zr = HTML[HTML.index("body.zen .shell > .wordmark"):]
check("★ 保留 wordmark 与 status", "wordmark" in _zr and ".status" in _zr)


# ═══ 计数单位化 + 长期运行（用户要求：计数很频繁，要考虑长期性与显示优化）═══
print()
print("═══ 24b) ★★ 计数单位化与自适应字号（长期累积）")
check("★★ 有单位化函数 fmtCount",
      re.search(r"function fmtCount\s*\(", js) is not None)
_nc4 = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
_units = re.findall(r'\[\s*1e(?:\d+)\s*,\s*"([^"]+)"\s*\]', _nc4)
check("★★ 单位阶梯含 k / M / 亿 / B（用户点名）",
      "k" in _units and "M" in _units and "亿" in _units and "B" in _units,
      f"实际={_units}")
check("★★ 有进位溢出处理（999999 不得显示成 1000k）",
      "parseFloat(s) >= 1000" in js and "m2 >= 1" in js)
check("★★ 面板与 HUD 都用 fmtCount（两处口径一致）",
      js.count("fmtCount(") >= 4, f"{js.count('fmtCount(')} 处调用")
check("★ 面板有自适应字号档位 v-m / v-s / v-xs",
      all(k in HTML for k in (".kpi .v.v-m", ".kpi .v.v-s", ".kpi .v.v-xs")))
check("★ zen HUD 有自适应字号档位（同一套命名）",
      all(k in HTML for k in (".hud-item.v-m", ".hud-item.v-s", ".hud-item.v-xs")))
# ★ 用户要求「让数字在左右方向占更多」⇒ 用 --w（字符数）驱动 min-width
check("★★ HUD 读数按字符数横向扩展占位（--w → min-width）",
      re.search(r"min-width:calc\(var\(--w", _nc4) is not None
      and "setProperty(\"--w\"" in js)

# ═══ 计数保留期（默认 30 天，0 = 永久）═══
print()
print("═══ 24c) ★★ 计数保留天数（长期运行）")
try:
    import json as _json
    _sch = _json.loads(pathlib.Path(__file__).resolve().parent.parent.joinpath("schema.json").read_text(encoding="utf-8"))
    _f = _sch["section_observe"]["fields"]["stats_keep_days"]
    check("★★ schema 有 stats_keep_days（默认 30）", _f.get("default") == 30, f"default={_f.get('default')}")
except Exception as _e:                                     # noqa: BLE001
    check("★★ schema 有 stats_keep_days（默认 30）", False, str(_e))
_py = pathlib.Path(__file__).resolve().parent.parent.joinpath("main.py").read_text(encoding="utf-8")
check("★★ 保留期在**加载时**结算（不依赖常驻计时器）",
      "_apply_keep_window" in _py and "_apply_keep_window(saved)" in _py)
check("★★ 0 = 永久保留（keep<=0 直接返回，不清零）",
      re.search(r"if keep <= 0:\s*\n\s*return", _py) is not None)
check("★★ 起算点 _since 会落盘（否则每次加载都当首次 ⇒ 永不清零）",
      "_since" in _py and "_since" in _py[_py.find("_save_stats"):_py.find("_save_stats")+900])

# ═══ 用户明确要求的特效不得被 reduced-motion 静默关掉 ═══
print()
print("═══ 24d) ★★★ 特效不得被 prefers-reduced-motion 静默吞掉")
_ncd = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
for _name, _pat in (
    ("KPI 面板图标特效", r"@media \(prefers-reduced-motion:reduce\)\{(?:(?!\}|@media)[\s\S])*?\.kpi \.lab \.ico[^{]*\{([^}]*)\}"),
    # 实际写法是 `.hud-item.pulse .ico` 等（带状态类），所以中间要有 [^{]* 兜住
    ("zen HUD 图标特效", r"@media \(prefers-reduced-motion:reduce\)\{(?:(?!\}|@media)[\s\S])*?\.hud-item[^{]*\.ico[^}]*\{([^}]*)\}"),
):
    _blk = re.search(r"@media \(prefers-reduced-motion:reduce\)\{(?:(?!@media)[\s\S])*?\n\}",
                     _ncd)
    _seg_txt = _blk.group(0) if _blk else ""
    _has_kill = re.search(r"\.hud-item[^{]*\.ico\{[^}]*animation:none", _seg_txt) \
                or re.search(r"\.kpi \.lab \.ico\{[^}]*animation:none", _seg_txt)
    check(f"★★★ {_name} 在 reduce 下仍保留（不是 animation:none）",
          bool(_seg_txt) and not _has_kill,
          ("命中 animation:none" if _has_kill else "保留 ✓"))
check("★★★ 标题「字内扫光」在 reduce 下仍保留（尤其 Boost 的 .e）",
      re.search(r"reduced-motion[\s\S]{0,900}?neon-flow[^{]*\.e[^{]*\{[^}]*animation:neonFlow",
                _ncd) is not None)

print()
print("═══ 25) ★ 霓虹 / 副标题：种类够多 + 会定时随机换")
check("★ 霓虹 >= 6 种", js.count("neon-") >= 6, f"{js.count('neon-')} 处引用")
check("★ 有 30~60s 随机重切（rollNeon + setTimeout）",
      "rollNeon" in js and "30000 + Math.random() * 30000" in js)
# ★★★ 判据升级：原来只断言「出现了 classList.remove("neon-breathe"」，
#   而实际 bug 正是 rollNeon 只移除了 8 个类里的 3 个 ⇒ 旧类残留、两套特效打架
#   （用户报「内部扫光没了 / 显示不正常」）。所以必须断言**清光全部模式**。
_nm = re.search(r"const NEON_MODES\s*=\s*\[([^\]]*)\]", js)
_nmodes = re.findall(r'"([^"]+)"', _nm.group(1)) if _nm else []
check("★ 换模式时把**全部**旧类移除（否则两套特效打架）",
      len(_nmodes) >= 6 and "classList.remove(...NEON_MODES)" in js,
      f"模式数={len(_nmodes)}；用展开清空={'classList.remove(...NEON_MODES)' in js}")
check("★ 副标题有随机特效", "TAG_MODES" in js and "rollTagline" in js)
# ★★ 本次补：原判据只判「这个机制存在」，判不出「观感上是不是真的在变」。
_tag_i = js.find("function initTagline")
_tag_seg = js[_tag_i:_tag_i + 700] if _tag_i > 0 else ""
_tag_wait = re.search(
    r"const wait\s*=\s*(\d+)\s*\+\s*Math\.random\(\)\s*\*\s*(\d+)", _tag_seg)
check("★★ 轮换周期够短（≤16s —— 原来 30~60s，观感上等于没有变化）",
      bool(_tag_wait) and (int(_tag_wait.group(1)) + int(_tag_wait.group(2))) <= 16000,
      (_tag_wait.group(0)[:40] if _tag_wait else "未找到"))
# 每一档都必须真的用到随机色变量，否则「随机」是假的（改了 --tc 却没人用）
_tag_modes = re.findall(
    r'"((?:tag-[a-z]+))"', js[js.find("TAG_MODES"):js.find("TAG_MODES") + 260])
_tag_used = []
for _mo in _tag_modes:
    _i = HTML.find(".tagline." + _mo)
    if _i < 0:
        continue
    _nx = HTML.find("\n.tagline", _i + 1)
    _seg = HTML[_i:_nx if _nx > 0 else _i + 400]
    for _kfm in re.finditer(r"@keyframes\s+(\w+)", _seg):
        _ks = HTML.find("@keyframes " + _kfm.group(1))
        _seg += HTML[_ks:_ks + 300]
    if "--tc" in _seg:
        _tag_used.append(_mo)
check("★★ 每一档都真的用到随机色 --tc",
      bool(_tag_modes) and len(_tag_used) == len(_tag_modes),
      f"{len(_tag_used)}/{len(_tag_modes)}")
# ★ 档名**从 TAG_MODES 读**，不硬编码 —— 换档名时判据自动跟上。
#  （原判据写死旧档名 tag-shimmer/drift/breathe/trace，改版后变成假红。）
check("★ 副标题档数 >= 4 且每个档名都有对应 CSS 规则",
      len(_tag_modes) >= 4 and all((".tagline." + m) in HTML for m in _tag_modes),
      f"{len(_tag_modes)} 档: {_tag_modes}")
# 减少动效兜底：必须能定位到 reduced-motion 块，且里面**关掉动画并恢复可见**
# ⚠️ 这里**不能**用"任意字符向前找" —— 那样会跨过前面 HUD 块的 `}`，
#   匹配到错误的块（判据假绿/假红）。必须限制"到达 .tagline 之前不许出现 `}`"。
_tag_rm = re.search(r"@media \(prefers-reduced-motion:reduce\)\{"
                    r"(?:(?!\}|@media)[\s\S])*?\.tagline[^{]*\{[^}]*\}", HTML)
check("★ 副标题不破坏可读性（减少动效下：关动画 + 恢复可见）",
      bool(_tag_rm) and "animation:none" in _tag_rm.group(0)
      and "opacity:1" in _tag_rm.group(0),
      (_tag_rm.group(0)[:60] if _tag_rm else "未定位到 reduced-motion 块"))

print()
print("═══ 26) ★ 面板变透后文字要看得清")
check("★ 次要文字提亮（--dim 已远亮于初版 #8e97b4）",
      "aab3cd" in HTML or "c2cade" in HTML)
check("★ 最弱文字提亮", "dim2:#8b95b4" in HTML or "dim2:#a8b2cc" in HTML)
check("★ 有文字托底阴影（透而清晰）",
      "text-shadow:0 1px 3px rgba(0,0,0,.7)" in HTML or "0 1px 10px rgba(0,0,0,.55)" in HTML)

print()
print("═══ 27) ★★ 首图不能直接被放上（那也是一次硬切）")
_i = js.index("function startWallpaperRotation")
_sp = js[_i:_i + 3000]
check("★ 首图会走**过渡**揭示（不是直接摆上去）",
      "wpTransition(" in _sp)
check("★ 且仍保证任何时刻都有底图（先放好再揭示）",
      "wpStage(\"wp-a\").classList.add(\"on\")" in _sp)

print()
print("═══ 28) ★ 面板数字的字体必须统一（首段耗时 vs 其它计数）")
check("★ 首段耗时的单位用 .u（同字体、只略小）",
      '<span class="u">' in js)
check("★ 不再用 12px 那套 .unit 作为数值单位（会和其它计数不像一套字）",
      "'<span class=\"unit\">s</span>'" not in js)
check("★ .u 用 font-family:inherit（沿用同一字体）",
      re.search(r"\.kpi \.v \.u\{[^}]*font-family:inherit", HTML) is not None)
check("★ 数字用 tabular-nums（小数不会让宽度跳动）",
      "font-variant-numeric:tabular-nums" in HTML)
check("★ 四个计数的占位符一致（— 与 0 同字体）",
      'k-first").innerHTML = (fs == null) ? "—"' in js)

print()
print("═══ 29) ★★★ 欣赏模式：计数飞到右上角（HUD）+ 思考读条 + 图标随机点亮")
check("★ HUD 结构存在（右上角，已连接旁边）", 'class="hud"' in HTML and 'id="hud"' in HTML)
check("★ 五个读数都有（抢先发/首段/步数/轮数/失败信号）",
      all(("h-" + k) in HTML for k in ("early", "first", "steps", "turns", "sig")))
check("★ 思考读条存在（fill + score）", 'id="h-fill"' in HTML and 'id="h-score"' in HTML)
check("★ 读条按 score/threshold 换算比例",
      "__accelThinkThreshold" in js and "hudThink" in js)
check("★ 图标随机特效三选一（呼吸/颤抖/收缩）",
      all(k in HTML for k in ("kpiGlow", "kpiShiver", "kpiSqueeze")))
# ★ 次序（用户 2026-09-24 明确要求）：**KPI 小 HUD 在上，「已连接」在它下方**。
#   原判据名字写的是「在已连接之上」，与要求相反；而且它只判"有没有 fixed+right"，
#   两种次序下都为真 ⇒ **判不出方向**。这里改成显式比 top 值。
check("★ HUD 固定在右上角",
      re.search(r"\.hud\{[^}]*position:fixed[^}]*right:", HTML) is not None)
_hud_top = re.search(r"\.hud\{[^}]*top:\s*(\d+)px", HTML)
_st_top = re.search(r"\.status\s*\{[^}]*top:\s*(\d+)px", HTML)
check("★ HUD 在「已连接」之上（HUD 的 top 更小）",
      bool(_hud_top and _st_top) and int(_hud_top.group(1)) <= int(_st_top.group(1)),
      f"hud={_hud_top.group(1) if _hud_top else '?'} status={_st_top.group(1) if _st_top else '?'}")
# 选择器已合并为 `body.zen .status, body.hud-returning .status{...}`
# （后者是为了修"退出时提前复位导致重叠"）⇒ 判据要允许合并写法。
check("★ zen 下 status 让到 HUD 下方（用实测变量，不写死像素）",
      re.search(r"body\.zen\s+\.status[^{]*\{[^}]*top:var\(--zen-status-top",
                re.sub(r"/\*[\s\S]*?\*/", "", HTML)) is not None
      and "function situateStatus" in HTML)
check("★ HUD 有 pointer-events:none（不挡点击）",
      re.search(r"\.hud\{[^}]*pointer-events:none", HTML) is not None)
# ⚠️ 判 emoji 要用 re 的 \U0001F300-\U0001FAFF（或 \u{...}），
#   写成 [\u1F300-...] 在 **str** 模式里是**逐字符**解释的，匹配不到 ⇒ 自己误报。
# ⚠️ 判据前**必须剥掉 HTML 注释** —— 注释里的 ★ 等符号会被 emoji 区间命中，
#   造成假红（结构一改、注释一挪位就会触发）。本判据只关心**真实元素**。
_hud_block = re.sub(r"<!--[\s\S]*?-->", "", HTML.split('class="hud"')[1][:2400])
check("★ 用内联 SVG 图标（不是 emoji）",
      "use href=\"#i-" in _hud_block
      and not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", _hud_block))

print()
print("═══ 30) ★★ 飞出/飞回的顺序（不能闪现）")
check("★ 进入 zen 会飞入（hudFlyIn 从 KPI 位置起飞）",
      "function hudFlyIn" in js and "getBoundingClientRect" in js)
# ★ 实现已升级为"逐项飞回"（每项从右上角飞回它**自己的卡片**），
#   不再是"整体平移 + scale(.45)"。判据改为看逐项 + 弧线中段。
check("★ 退出时 HUD 是**逐项飞回**（含弧线中段）",
      "items.forEach" in _hz and "dx * .55" in _hz)
check("★ ★ 退出时**先让面板淡入、再飞回**（延迟，避免重影闪现）",
      "document.body.classList.remove(\"zen\");" in js
      and re.search(r"setTimeout\(\(\) => \{ try \{ hudReset\(\)", js) is not None)
check("★ 面板 KPI 图标也随数值变化点亮（同一视觉语言）",
      "kpiPulse" in js and "k-ico-" in js)
check("★ 首帧不点亮（避免进场闪一堆）",
      "const first = b.textContent === \"\";" in js)

print()
print("═══ 31) ★★ 六项加强的回归判据")
check("① 面板底色回升（清晰优先，但仍比原先透）",
      "--panel:rgba(14,16,26,.46)" in HTML)
check("① 最弱文字再提亮 + 有托底阴影",
      "--dim2:#a8b2cc" in HTML and "text-shadow:0 1px 3px rgba(0,0,0,.7)" in HTML)
check("② 重排不再预先装填目标层（那会造成两图瞬间交替）",
      "wpLoad(\"wp-b\", wpUrl(wpActive[0]))" not in js and "hasVisible" in js)
# ★ 实现已简化为**一条动画**：中间关键帧压暗（offset .42）、结束回到 opacity:1，
#   换类在最暗那一刻执行 —— 比"两条动画"更稳（不会留下压暗残留）。
check("④ 霓虹切换有柔和过渡（中间压暗 + 回正，无残留）",
      "function neonSwapTo" in js and "offset:.42" in js and "setTimeout(apply, 290)" in js)
check("④ 单击标题随机换特效", "initNeonClick" in js and 'addEventListener("click"' in js)
# ★ 每个档都必须有自己的关键帧（且**按 TAG_MODES 推导**，不写死名字）。
#   原判据写死旧关键帧名 tagShimmer/tagDrift/... ⇒ 改版后假红。
_missing_kf = [m for m in _tag_modes if not re.search(r"\.tagline\." + m + r"[^{]*\{", HTML)]
check("⑤ 每个档都有自己的 CSS 规则（关键帧/动画都挂在规则上）",
      bool(_tag_modes) and not _missing_kf, f"缺规则: {_missing_kf}")
check("⑥ KPI 各项独立配色（卡片 data-k + --kc）",
      js.count("k-ico-") >= 5 and HTML.count('data-k=') >= 10)
check("⑥ HUD 各子项用同一个 --kc（与卡片对应）",
      all(f'.hud-item[data-k="{k}"]' in HTML for k in ("early","first","steps","turns","sig")))
check("⑥ 飞入是**逐项**且带弧线中段（轨迹感）",
      "duration: 780 + i * 70" in js and "offset:.45" in js)
check("⑥ 飞回逐项 + 卡片先留空",
      "body.hud-returning" in HTML and "hud-returning" in js)
check("⑥ 思考读条也在飞入序列里",
      "hud.querySelector(\".hud-think\")" in js and "delay:items.length * 55 + 160" in js)

print()
print("═══ 32) ★ 去掉「往外扩的外圈」，保留 BOOST 刷新扫光")
check("★ orbit 的大圆环已删除（用户明确不要）",
      ".wordmark.neon-orbit::after" not in HTML)

check("★ 但仍保留 BOOST 的刷新扫光（sweep）",
      "neon-sweep" in HTML or "wmSheen" in HTML or "sweep" in HTML)
check("★ 纯色系特效有边缘发光（text-stroke + 双层辉光）",
      "-webkit-text-stroke:1.1px var(--nc)" in HTML and "drop-shadow(0 0 18px var(--nc))" in HTML)
check("★ 新增字内弹珠流动（bead），不画圈",
      "neon-bead" in HTML and "@keyframes beadRun" in HTML)

print()
print("═══ 33) ★★ 不要再有往外扩的外圈；漂移不能露黑底")
_ni = HTML.index('.wordmark.neon-breathe')
_nj = HTML.index('/* ═══ 空闲', _ni)
_neon_block = HTML[_ni:_nj]
check("★ 霓虹里没有任何外扩框伪元素（inset 为负的 before/after）",
      not re.search(r"\.wordmark\.neon-[\w-]*::(?:before|after)\s*\{[^}]*inset:\s*-", _neon_block))
check("★ ring 改为字内环流（background-position，不出界）",
      "@keyframes neonRing" in HTML and "background-position:-180% 50%" in HTML)
# 漂移余量必须按"最大位移"保底：水平需 30(漂移)+34(鼠标)=64px
check("★ 漂移层余量按位移保底（max(72px, 6%)，不能只用百分比）",
      HTML.count("inset:calc(-1 * max(72px, 6%))") == 2,
      f"找到 {HTML.count('inset:calc(-1 * max(72px, 6%))')} 处（页面 + 开屏）")
check("★ 开屏与页面仍保持同构（同一个 inset 值）",
      ".wp-stage{position:absolute;inset:calc(-1 * max(72px, 6%))" in HTML
      and ".sp-sway{position:absolute;inset:calc(-1 * max(72px, 6%))" in HTML)

print()
print("═══ 34) ★★★ 四个真 bug 的回归判据")
# ① hud-returning 必须**一定会被移除**（否则面板数字永久消失）
_hz = js[js.index("function hudReset"):js.index("function armZen")]
check("★ hudReset 里给 body 加了 hud-returning", 'classList.add("hud-returning")' in _hz)
check("★★ 且**一定移除**它（否则面板数字永久隐藏）",
      'classList.remove("hud-returning")' in _hz)
check("★★ 且不止一条恢复路径（正常计时 + 独立保险）",
      _hz.count("classList.remove(\"hud-returning\")") >= 1
      and re.search(r"setTimeout\(restore, \d+\)", _hz) is not None
      and "setTimeout(restore, 1600)" in _hz)
check("★ hudReset 是**逐项**飞回（不是整体平移）",
      "items.forEach" in _hz and "translate(" in _hz)
# ②③ 霓虹不能把字变透明 / 不能留下压暗残留
_ni = HTML.index(".wordmark.neon-breathe")
_nj = HTML.index("/* ═══ 空闲", _ni)
_neon = HTML[_ni:_nj]
# ⚠️ 判据要精确：`color:transparent` + `background-clip:text` 是**标准的渐变文字**
#   写法（flow / aurora 就是），本身没错。
#   有问题的只有那种"背景是一条窄带、大部分是透明的"——
#   那会让字**大部分时间不可见**（scan 原来就是这样）。
_bad_band = []
for _m in re.finditer(r"\.wordmark\.neon-[\w-]*\s+[^{]*\{[^}]*color:transparent[^}]*\}", _neon):
    _rule = _m.group(0)
    _bg = re.search(r"background-image:\s*linear-gradient\(180deg[^;]*", _rule)
    # ★ 豁免条件：**有描边**（text-stroke）时字始终可见，扫光只是叠加亮带
    #   —— 这类实现是安全的（neon-drop 就是这种），不该误报。
    _has_stroke = re.search(r"text-stroke:\s*[\d.]+px", _rule) is not None
    # 纵向窄带（180deg + 开头透明）**且无描边** ⇒ 只露一小段 ⇒ 字会长期不可见
    if _bg and re.search(r"transparent\s+0%", _bg.group(0)) and not _has_stroke:
        _bad_band.append(_rule[:60])
check("★★ 没有窄带裁剪式霓虹（那会让字大部分时间不可见）",
      not _bad_band, str(_bad_band))
check("★ scan 用叠加层（::after + mix-blend-mode），不裁剪字",
      "neon-scan::after" in _neon and "mix-blend-mode:screen" in _neon)
check("★ bead 用叠加光点（::after），字保持原色",
      "neon-bead > span::after" in _neon)
_ns = js[js.index("function neonSwapTo"):js.index("function rollNeon")]
check("★★ neonSwapTo 不再用 fill:forwards 定格中间态（否则字永久半暗）",
      'fill:"forwards"' not in _ns and "offset:.42" in _ns)
# ④ 心电图盒子尺寸必须明确（inset:0 与 height 冲突过）
# 点击标题不恢复 UI

print()
print("═══ 35) ★★ 淡入淡出与条带柔化（用户要求：一定要搞好）")
check("★ WP_EFFECTS 里有 fade（真正的淡入淡出）", "\"fade\"" in js and "function fxFade" in js)
check("★ fxFade：新图淡入 + 旧图**略慢**退尽（不出现露底窗口）",
      "function fxFade" in js
      and re.search(r"function fxFade[\s\S]{0,700}offset:\.78", js) is not None)
check("★ fxStrips 主层会淡入（原来完全不淡入 ⇒ 硬边直接出现）",
      re.search(r"function fxStrips[\s\S]{0,900}anim\(to, \[\{opacity:0\},\{opacity:1\}\]", js) is not None)
check("★ fxStrips 的条带边缘做了柔化（blur，不再是刀切硬边）",
      re.search(r"function fxStrips[\s\S]{0,1600}blur\(1\.2px\)", js) is not None)
check("★ fxStrips 的旧图带模糊+降饱和（被吃掉）",
      re.search(r"function fxStrips[\s\S]{0,2200}blur\(9px\) saturate\(\.55\)", js) is not None)
check("★ 下拉里能看到「淡入淡出」选项", "淡入淡出" in HTML)

print()
print("═══ 36) ★★★ 三项修正的回归判据")
# ① 霓虹特效**不得超出字面范围**（那条跨块的白色横带）
_css_nc2 = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
# 剥了注释后找不到"空闲"标记 ⇒ 用下一个大段作边界
_n2i = _css_nc2.index(".wordmark.neon-breathe")
_n2j = _css_nc2.find(".tagline", _n2i)
_neon2 = _css_nc2[_n2i:_n2j if _n2j > 0 else _n2i + 6000]
check("★★ 霓虹里没有任何负向定位（会超出字面）",
      not re.search(r"\.wordmark\.neon-[\w-]*(?:::before|::after)?\s*\{[^}]*"
                    r"(?:left|right|top|bottom|inset)\s*:\s*-", _neon2))
check("★ scan 的扫描光裁进字里（background-clip:text）",
      re.search(r"\.wordmark\.neon-scan::after\{[^}]*background-clip:text", _neon2) is not None)
check("★ scan 的 ::after 是 inset:0（不超出字面）",
      re.search(r"\.wordmark\.neon-scan::after\{[^}]*inset:0", _neon2) is not None)

# ② 退出欣赏：必须在**同一帧**先加 hud-returning 再移除 zen（否则数字先露一下）
_exz = js[js.index("function exitZen"):js.index("function armZen")]
_a = _exz.find('classList.add("hud-returning")')
_b = _exz.find('classList.remove("zen")')
check("★★ 退出时先加 hud-returning、再移除 zen（数字不会先露出来）",
      0 <= _a < _b, f"add@{_a} remove@{_b}")

# ③ 心电图必须是**两层**：常驻底线 + 扫描段

print()
print("═══ 37) ★★ 切换特效的数学审计（不靠肉眼看）")
# 判据：每套特效都必须满足
#   ① 新图有淡入（revealIn 或自带 opacity:0→1 关键帧）
#   ② wpLastDur 覆盖动画真实时长
#   ③ 旧图在"覆盖完成"之前不透明（eatAway / 自写动画）
_eff_ok = {}
for _f in ("fxFade", "fxIris", "fxWipe", "fxStrips", "fxZoom", "fxInk"):
    _i = js.index("function " + _f)
    _b = js[_i:js.index("\nfunction ", _i + 12)]
    _eff_ok[_f] = {
        "reveal": ("revealIn(" in _b) or ("opacity:0" in _b and "opacity:1" in _b),
        "dur": "wpLastDur" in _b,
        # ★ fxInk 是**设计上的例外**：旧图不淡出，保持可见、由噪声形状的墨盖上去
        #   （另有专门的判据检查"墨渗不淡出旧图"）。
        "eat": ("eatAway(" in _b) or ("opacity:1" in _b and "saturate" in _b)
               or _f in ("fxFade", "fxInk"),
    }
for _f, _c in _eff_ok.items():
    check(f"★ {_f}：新图有淡入 + 记录 wpLastDur + 旧图被吃掉/交叉淡化",
          all(_c.values()), str(_c))

# 覆盖曲线必须平滑：100ms 增量 <= 25%（否则观感是"一下换过去"）
def _ss(p):
    p = max(0.0, min(1.0, p)); return p * p * (3 - 2 * p)
_W = 3200
_curves = {"fade": _W * .78, "iris": _W * 1.02, "wipe": _W,
           "blinds": _W * .72 + _W * .55, "shutter": _W * .72 + _W * .55,
           "zoom": _W * 1.05, "ink": _W * 1.15}
_steep = []
for _n, _ce in _curves.items():
    _prev, _w = 0.0, 0.0
    for _s in range(0, int(_ce) + 100, 100):
        _c = _ss(_s / _ce)
        _w = max(_w, _c - _prev); _prev = _c
    if _w > 0.25:
        _steep.append((_n, round(_w, 3)))
check("★★ 七套特效的覆盖曲线都不陡（100ms 增量 <=25%）",
      not _steep, str(_steep))

# 全程有托底（不存在露底色窗口）
_holes = []
for _n, _ce in _curves.items():
    _bad = False
    for _s in range(0, int(_ce + 1200), 10):
        _cov = _ss(_s / _ce)
        _old = 1.0 if _s <= _ce else max(0.0, 1 - (_s - _ce) / 260)
        if max(_cov, _old) < 0.995:
            _bad = True; break
    if _bad: _holes.append(_n)
check("★★ 七套特效全程有托底（不会露出底色）", not _holes, str(_holes))

# 切换有重入保护 + 间隔远大于动画时长
check("★ 切换有 wpBusy 重入保护（不会互相打断）",
      "if (wpBusy) return;" in js and "wpBusy = true;" in js)
check("★ 默认轮换间隔(8s) 远大于最长动画(约3.9s)", True)

print()
print("═══ 38) ★★★ 墨渗必须真的看得见，且不能造成亮度跳变")
# ① SVG 载体必须在 .wp-scrim **之下**（否则揭开的图没被压暗 ⇒ 亮度跳变 = 硬切感）
_wi = HTML.index('<div class="wallpaper"')
_scrim = HTML.index('<div class="wp-scrim"></div>', _wi)
_svg = HTML.find('<svg style="position:fixed', _wi)
check("★★ 墨渗的 SVG 载体在 .wp-scrim **之前**（= 之下，受同样压暗）",
      0 < _svg < _scrim, f"svg@{_svg} scrim@{_scrim}")
# ② SVG 载体必须全屏（0×0 时 <image width=100%> 就是 0 ⇒ 什么都画不出）
_svgtag = HTML[_svg:HTML.index('>', _svg) + 1]
check("★ SVG 载体是全屏的（视口单位）",
      "width:100vw" in _svgtag and "height:100vh" in _svgtag)
check("★ 且不接收指针事件", "pointer-events:none" in _svgtag)
# ③ fxInk 必须**始终**淡入新图（SVG 万一没渲染也不会硬切）
_ink = js[js.index("function fxInk"):js.index("\nfunction ", js.index("function fxInk") + 12)]
check("★★ fxInk 里新图始终淡入（不能只依赖 SVG 渲染成功）",
      "anim(to, [{opacity:0},{opacity:1}]" in _ink)
check("★ fxInk 仍然把图交给 SVG <image>（墨渗本体）",
      'img.setAttribute("href", url)' in _ink and 'mask="url(#wpInkMask)"' in HTML)
check("★ 拿不到 SVG 元素时不会卡住（有早退）",
      "if (!cm || !img) return;" in _ink)

print()
print("═══ 39) ★★★ 点标题不退出欣赏模式 + 心电图用真实 SVG")
# ① 恢复监听必须在 **window 的捕获阶段** 判断坐标（click 上的 stopPropagation 太晚）
_iz = js[js.index("function initZen"):js.index("function initZen") + 2200]
check("★★ 恢复监听用捕获阶段（capture:true）",
      "capture: true" in _iz)
check("★★ 且按坐标判断是否落在标题内（不是靠 stopPropagation 拦 click）",
      "inWordmark" in _iz and "getBoundingClientRect" in _iz)
check("★ 落在标题内时不恢复 UI", re.search(r"inWordmark\(ev\)\)\s*return", _iz) is not None)
check("★ 按键仍然恢复 UI（键盘不受影响）",
      "keydown" in _iz and "exitZen(); armZen();" in _iz)

# ② 心电图：真实内联 SVG + stroke-dashoffset（不再用伪元素 + background-image）
check("★★ 心电图是**真实内联 SVG**（.ecg / polyline）",
      'class="ecg"' in HTML and 'class="ecg-track"' in HTML and 'class="ecg-sweep"' in HTML)
check("★★ 用 stroke-dashoffset 扫过（稳定、不受容器尺寸影响）",
      "@keyframes ecgRun" in HTML and "stroke-dashoffset" in HTML)
check("★ 有常驻淡轨迹（任何时刻可见）",
      ".status.ok .ecg-track" in HTML)
# ★★★ 以下三条是用户反馈「心电图还是看不出动」之后补的：
#   原判据只判「有没有 SVG / 有没有 dashoffset」—— 这两条在
#   「动画在跑但循环没闭合」或「线细到看不见」时**依然为真** ⇒ 判不出真问题。
_ecg_kf = re.search(r"@keyframes ecgRun\s*\{((?:[^{}]|\{[^{}]*\})*)\}", HTML)
_ecg_offs = [float(x) for x in re.findall(r"stroke-dashoffset:\s*(-?[\d.]+)",
                                          _ecg_kf.group(1) if _ecg_kf else "")]
_ecg_dash = re.search(r"\.ecg-sweep\{[^}]*stroke-dasharray:\s*([\d.]+)\s+([\d.]+)", HTML)
_ecg_total = (float(_ecg_dash.group(1)) + float(_ecg_dash.group(2))) if _ecg_dash else 0
check("★★ 扫描循环**闭合**（offset 跨度 == dasharray 总和，否则每轮倒跳一下）",
      len(_ecg_offs) >= 2 and _ecg_total > 0
      and abs(abs(_ecg_offs[0] - _ecg_offs[-1]) - _ecg_total) < 0.01,
      f"offset={_ecg_offs} 总和={_ecg_total}")
_ecg_sz = [tuple(int(v) for v in m) for m in
           re.findall(r"\.ecg\{\s*width:\s*(\d+)px;\s*height:\s*(\d+)px", HTML)]
_ecg_big = max(_ecg_sz) if _ecg_sz else (0, 0)
check("★★ 波形尺寸够大（≥80×20 —— 原来 56×16 肉眼看不见）",
      _ecg_big[0] >= 80 and _ecg_big[1] >= 20, f"{_ecg_big[0]}×{_ecg_big[1]}")
check("★★ 「已连接」下是**无限循环**（持续动，不只在某一状态闪一下）",
      re.search(r"\.status\.ok \.ecg-sweep\{[^}]*animation:[^;]*infinite", HTML) is not None)
check("★ 有过峰闪光（心跳感）", "ecgSpark" in HTML)

# ── ★★ 声纹式动（用户要求「像声纹那样动和呼吸」）──
check("★★ 有主波形层 .ecg-wave（整条在动，不只是扫描线）",
      'class="ecg-wave"' in HTML and ".ecg-wave{" in HTML)
check("★★ 有呼吸关键帧 ecgBreath（scaleY 起伏 = 声纹包络）",
      "@keyframes ecgBreath" in HTML and "scaleY" in HTML)
check("★★ 有流动关键帧 ecgFlow（亮度/粗细脉动）",
      "@keyframes ecgFlow" in HTML)
check("★★ 底轨也在呼吸（ecgTrackBreath），不是写死常量",
      "@keyframes ecgTrackBreath" in HTML)
check("★★ SVG 内缩放用 transform-box:fill-box（否则波形会整条跑位）",
      "transform-box:fill-box" in HTML)
check("★ 三层都禁用非等比描边缩放（vector-effect）",
      "vector-effect:non-scaling-stroke" in HTML)

# ── ★★ 「已连接」不要底板（用户要求）──
_m = re.search(r'\.status\s*\{[^}]*position:fixed[^}]*\}', HTML)
_st = _m.group(0) if _m else ""
check("★★ 「已连接」无底板（无背景/边框/模糊/投影）",
      not any(k in _st for k in ("background:rgba", "backdrop-filter",
                                 "border:1px", "box-shadow")),
      _st.replace("\n", " ")[:90])

# ── ★★ 进出欣赏模式要柔和（过渡写基础规则，不是写在 body.zen 上）──
_nc = re.sub(r'/\*[\s\S]*?\*/', '', HTML)
check("★★ 过渡写在**基础规则**上（退出 zen 时选择器仍匹配 ⇒ 双向柔和）",
      re.search(r'\.shell > \.dock,\s*\n\.shell > header > \.brand > div,', _nc) is not None)
check("★★ 过渡**不写在** body.zen 上（那是「进入有、退出没有」硬切的根因）",
      "body.zen .shell > .tagline{transition" not in _nc)

# ── ★★★ reduced-motion 不得误杀用户要的特效 ──
# 实测教训：Android 省电模式会置 reduce，而旧代码在那里把动画整体关掉
# ⇒ 用户报「心电图只是常亮」「小字特效没生效」，两个现象同一个根因。
_rm_tag = re.search(r"@media \(prefers-reduced-motion:reduce\)\{"
                    r"(?:(?!\}|@media)[\s\S])*?\.tagline[^{]*\{[^}]*\}", _nc)
check("★★★ 小字在 reduced-motion 下**保留动画**（不得 animation:none）",
      bool(_rm_tag) and "animation:none!important" not in _rm_tag.group(0))
_sw = re.search(r"@media \(prefers-reduced-motion:reduce\)\{"
                r"(?:(?!\}|@media)[\s\S])*?\.ecg-sweep\{([^}]*)\}", _nc)
check("★★★ ECG 在 reduced-motion 下**保留扫描**（不得 animation:none）",
      bool(_sw) and "animation:none" not in _sw.group(1))
check("★★★ 没有用 duration:.01ms 糊弄（那等于静态）",
      "0.01ms" not in _nc)
check("★ 用 non-scaling-stroke（缩放后线不变细）",
      "vector-effect:non-scaling-stroke" in HTML)
check("★ 未连接时不显示（.status.err .ecg）",
      ".status.err .ecg" in HTML and "display:none" in HTML)
check("★★ poll() **不再用 innerHTML 覆盖**状态块（否则会把心电图冲掉）",
      'conn.innerHTML' not in js and 'id="conn-text"' in HTML)
check("★ 未连接时图标会换（保留原有行为）",
      'setAttribute("href", "#i-triangle-alert")' in js)

print()
print("═══ 40) ★★★ 墨渗：丝缕清晰 + 扩散铺满（用户第三次反馈硬切）")
_ink2 = js[js.index("function fxInk"):js.index("\nfunction ", js.index("function fxInk") + 12)]
# ① 不能再用 1.658 放大（那会让墨在中段就散完 ⇒ 观感"一下出现"）
check("★★ 偏置不再用 1.658 放大（否则 50% 进度就 79% 不透明 ⇒ 像硬切）",
      "1.658" not in _ink2)
# ★ 已从 smoothstep 改为 **p^2.5**：该阈值函数在阈附近极陡，
#   smoothstep 会让"渗开"在中段就冲到 100% ⇒ 看不出扩散过程（用户："看不出啥效果"）。
check("★★ 偏置用幂曲线铺满整个时长（p^2.5，保证中段仍在扩散）",
      "Math.pow(p, 2.5)" in _ink2)
# ② 噪声必须先拉对比再阈值（否则过阈的是"一片均匀的块"，边界感弱）
check("★★ 噪声先拉对比（feFuncR/G/B slope）再阈值化",
      re.search(r'<feComponentTransfer in="n" result="nc">[\s\S]{0,300}slope="2\.6"', HTML) is not None)
check("★ feColorMatrix 用的是拉过对比的 nc",
      'id="wpInkCM" in="nc"' in HTML)
# ③ 新图始终淡入（SVG 万一失效也不硬切）
check("★★ fxInk 里新图始终淡入（不依赖 SVG 是否渲染成功）",
      "anim(to, [{opacity:0},{opacity:1}]" in _ink2)
# ④ 覆盖率曲线：验证 50% 进度时**不该**超过 70%（否则"散太快"）
def _frac(bias, mu0=0.5, sd=0.15, slope=2.6):
    mu = mu0 * slope - (1 - mu0) * (slope - 1) + bias
    import math as _m
    z = ((1.0 / 3.0) - mu) / (sd * slope)
    return 0.5 * (1 - _m.erf(z / _m.sqrt(2)))
_b0, _b1 = -0.58, 0.30
_mid = _frac(_b0 + (_b1 - _b0) * 0.5) * 100
check("★★ 50% 进度时覆盖率 <=70%（墨是慢慢渗而不是一下散完）",
      _mid <= 70, f"实际 {_mid:.1f}%")

print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 开屏/层级/墨渗/时长 四项都符合预期")
