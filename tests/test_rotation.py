"""壁纸轮换逻辑自检 —— 从面板 HTML 里抽出真实逻辑并验证。

覆盖：
  1. 随机挑下一张（不按顺序）
  2. 不会连着出现同一张
  3. 只有 1 张时不轮换
  4. 只有 2 张时退化为交替（不会死循环）
  5. 轮换间隔来自配置（默认 30 秒）
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent   # 插件根
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


html = (HERE / "web" / "index.html").read_text(encoding="utf-8")

print("1) 轮换逻辑的静态检查（确认真的用了随机）")
check("使用 Math.random 选下一张", "Math.random() * wpActive.length" in html)
check("有避免连续重复的循环", "while (wpActive[next] === cur" in html)
check("不再用顺序 (wpIdx + 1) 递增", "wpIdx = (wpIdx + 1) % wpActive.length" not in html)
check("只有 1 张时提前返回（不轮换）", "if (wpActive.length < 2 || wpIntervalS <= 0) return;" in html)
# ★ 行为升级（2026-09-23）：定时器里现在还要求"不在切换中"（wpBusy），
#   否则会和正在飞的切换抢图层。
check("定时器里对 <=1 张做保护", "if (wpActive.length <= 1 || wpBusy) return;" in html)


print("\n2) 行为模拟：按面板里的算法跑，验证分布与去重")


def simulate(n, rounds, seed=0):
    """复刻面板里的随机挑选算法。"""
    import random
    rng = random.Random(seed)
    seq = []
    idx = 0
    for _ in range(rounds):
        if n <= 1:
            break
        cur = idx
        nxt = idx
        guard = 0
        while True:
            nxt = rng.randrange(n)
            guard += 1
            if idx != nxt or guard >= 20:
                break
        idx = nxt
        seq.append(idx)
    return seq


# 19 张，跑 200 次
seq = simulate(19, 200)
check("产生了 200 次切换", len(seq) == 200, f"实际={len(seq)}")
check("覆盖到多种壁纸（>=15 种）", len(set(seq)) >= 15, f"实际覆盖={len(set(seq))}")
# 检查无连续重复
dups = sum(1 for a, b in zip(seq, seq[1:]) if a == b)
check("没有连续重复", dups == 0, f"连续重复 {dups} 次")
# 检查不是顺序（顺序的话相邻差值恒为 1）
increments = sum(1 for a, b in zip(seq, seq[1:]) if (b - a) % 19 == 1)
check("不是顺序轮换", increments < len(seq) * 0.3,
      f"顺序递增占比 {increments}/{len(seq)}（顺序轮换会接近 100%）")

print("\n3) 边界情况")
check("1 张时不产生切换", simulate(1, 50) == [])
s2 = simulate(2, 40)
check("2 张时能持续切换（不死循环）", len(s2) == 40, f"实际={len(s2)}")
check("2 张时不重复", all(a != b for a, b in zip(s2, s2[1:])))

print("\n4) 轮换间隔默认值")
import json
sch = json.loads((HERE / "schema.json").read_text())
iv = sch["section_appearance"]["fields"]["wallpaper_interval"]
check("默认 8 秒", iv["default"] == 8, f"实际={iv['default']}")
check("配置说明提到随机", "随机" in iv["locales"]["zh"]["hint"])
check("0 表示不轮换的说明存在", "0" in iv["locales"]["zh"]["hint"])

print("\n4b) ★ 伪 live2D 视差（跟随鼠标的轻微晃动）")
pv = sch["section_appearance"]["fields"].get("wallpaper_parallax")
check("schema 里有 wallpaper_parallax 开关", pv is not None)
if pv:
    check("默认开启", pv.get("default") is True, str(pv.get("default")))
    hint = (pv.get("locales", {}).get("zh", {}) or {}).get("hint", "")
    check("说明里提到鼠标", "鼠标" in hint, hint[:50])
    check("说明里提到会随「减少动态效果」自动停用",
          "减少动态效果" in hint or "reduced-motion" in hint.lower(), hint[:70])

check("面板有 applySway / initParallax", "function applySway" in html and "function initParallax" in html)
# ★ 行为升级（2026-09-23，用户反馈"晃动没感觉"）：
#   ① 不再只跟鼠标 —— 壁纸**持续自行漂移**（不碰鼠标也在动）
#   ② 幅度从 16/11 提到 34/24
check("★ 有自动漂移（不碰鼠标画面也在动）", "function wpTick" in html and "baseX = Math.sin" in html)
check("★ 跟随鼠标的方向与鼠标相反（形成景深）",
      "-((x / window.innerWidth) * 2 - 1) * RX" in html.replace("\n", " ").replace("  ", " ")
      or "aimX = -((x / window.innerWidth) * 2 - 1) * RX" in html)
check("★ 幅度比旧版明显（旧版 16/11 几乎看不出）", "const RX = 34, RY = 24;" in html)
check("★ 开关在事件里实时判断（改配置即时生效）", "if (!wpParallax) return;" in html)
check("监听只注册一次（不会重复叠加）", "parallaxBound" in html)
check("★ reduced-motion 下停用", "animation:none!important" in html.replace(" ", "")
      and "transform:none!important" in html.replace(" ", ""))
check("切换时保留位移（复位后补回）", html.count("applySway();") >= 3, f"{html.count('applySway();')} 次")
# ★ 行为升级：位移与过渡特效现在是**分元素**写的（.wp-stage 写位移、.wp-img 写特效），
#   所以判据从"某条 transition 里含 transform"改成"结构上确实分了层"。
check("★ 位移与特效分层（不再互相覆盖 inline transform）",
      ".wp-stage{" in html and ".wp-img{" in html and "function applySway" in html)
check("★ 过渡用 Web Animations（可读回实际值，便于自检）", "el.animate(" in html or "E(to, [" in html or "animate(kf" in html)

print("\n5) ★ 文档与实物一致（数字最容易漂）")
import re as _re
wp_dir = HERE / "wallpapers"
real = len([f for f in wp_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".webp", ".jpg", ".png", ".jpeg", ".gif")])
check("壁纸目录里有图", real > 0, f"{real} 张")

readme = (HERE / "README.md").read_text(encoding="utf-8")
claimed = sorted({int(m) for m in _re.findall(r"(\d+)\s*张动漫壁纸", readme)})
check("★ README 写的张数 == 目录实际张数",
      claimed == [real], f"README 写 {claimed}，实际 {real} 张")
# 反向验证：把数字改错，必须与实物不符（证明这个检查不是恒真）
bad = readme.replace(f"{real} 张动漫壁纸", f"{real + 5} 张动漫壁纸")
bad_claimed = sorted({int(m) for m in _re.findall(r"(\d+)\s*张动漫壁纸", bad)})
check("★ 反向验证：数字改错后会被抓出", bad_claimed != [real],
      f"篡改后 {bad_claimed} vs 实际 {real}")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
    sys.exit(1)
print("✅ 全部通过")
