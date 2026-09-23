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
img_opacity_writes = re.findall(r"\bim\.style\.opacity\s*=", js)
check("★ 没有任何一行给**图片层**（im）写 inline opacity",
      not img_opacity_writes, f"找到 {len(img_opacity_writes)} 处")

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
    check("★ 首图给**两层**都 wpLoad（清掉可能残留的 .on）", n_load >= 2, f"{n_load} 次")
    check("首图只给 wp-a 加 .on（不会两层同时可见）",
          b.count('classList.add("on")') == 1)

print()
print("═══ 3) ★★ 开屏必须在**第一帧**出现（不能等网络）")
i_el = HTML.index('<div class="splash" id="splash"')
i_early = HTML.index("window.__accelSplash = true")
i_main = HTML.index("function playSplash")
check("★ 早期显示脚本在开屏元素**之后**（解析到即同步执行）", i_el < i_early)
check("★ 且在主脚本之前", i_early < i_main)
early = HTML[i_early:HTML.index("</script>", i_early)]
check("★ 早期脚本**同步**加 .show（不依赖任何 await / DOMContentLoaded）",
      'classList.add("show")' in early and "await" not in early)
check("★ 早期脚本自带硬兜底定时器（主脚本没跑起来也不会卡住页面）",
      "__accelSplashFailsafe" in early and "setTimeout" in early)
check("主脚本的 playSplash 不再自己负责显示，只填内容",
      "if (!sp || !window.__accelSplash) return;" in js)
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
f_mid = opaque_fraction(B0 + (B1 - B0) * 0.5)     # 进度 50% 处
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
print("═══ 6) ★ 特效要「慢一点」（用户明确要求）")
m = re.search(r"const WP_DUR = (\d+)", js)
check("WP_DUR 至少 3000ms（原来 2200 被反馈太快）", m and int(m.group(1)) >= 3000,
      m.group(1) if m else "未找到")
check("条带铺满整个时长（不再挤在前 60%）",
      "WP_DUR * .72" in js and "WP_DUR * .55 / N" in js)
check("墨渗比基准更长（≈1.15×）", "WP_DUR * 1.15" in js)

print()
print("═══ 7) ★ 「颜色相互消除/覆盖」感：旧图要被吃掉，不是单纯淡出")
# 每套特效的 from 动画都带 filter（模糊+降饱和）
from_anims = re.findall(r"anim\(from, \[\{([^\]]*?)\}", js)
with_filter = [a for a in from_anims if "filter" in a]
print(f"     带「被吃掉」处理的旧图动画: {len(with_filter)} / {len(from_anims)}")
check("★ 至少 4 套特效的旧图带模糊/降饱和（观感是被覆盖）",
      len(with_filter) >= 4, f"{len(with_filter)} 套")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 开屏/层级/墨渗/时长 四项都符合预期")
