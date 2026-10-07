"""对着真框架跑插件 API —— 这次不再用假数据。

为什么需要它：面板是「前端拼 URL → FastAPI 路由」的耦合，
用假数据渲染面板只能证明 HTML/CSS 没问题，
**证明不了 URL 真的能通**。本次线上 bug（面板未连接 + 壁纸 0 张）
就是这类耦合错误：框架挂在 /api/plugin/（单数），前端写成了 /api/plugins/（复数）。
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自身
import _env  # noqa: E402

FRAMEWORK = _env.framework()
if FRAMEWORK is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
os.environ["KIRA_FW"] = FRAMEWORK
ROOT = _env.ROOT            # 被测插件根（以前这里硬编码了开发机绝对路径）
PLUGIN_ID = "kira_accelerator"

# 框架在导入期就会建日志目录，必须先切到有 data/ 的地方
os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, FRAMEWORK)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


async def main():
    print("=== 0) 导入真框架 + 真插件 ===")
    from fastapi import FastAPI
    # ★ 跨世代：3.0 模块名是 core.plugin.registry（PluginManager 另在 manager.py）
    import importlib as _il2
    pr = _il2.import_module(_env.resolve_module("plugin_registry"))
    import importlib
    mod = _env.load("main")
    check("框架导入成功", True)
    check("插件模块导入成功（装饰器已执行）", True)

    comp = pr._plugin_components.get(PLUGIN_ID)
    check("插件已注册到框架组件表", comp is not None)
    api_paths = sorted({r["path"] for r in (comp.api_routes if comp else [])})
    print(f"     框架记录的插件 API: {api_paths}")
    print(f"     框架记录的插件页面: {getattr(comp, 'pages', None)}")

    # ── 1) 建实例 + 真 FastAPI app ──
    print("\n=== 1) 挂载到真 FastAPI app ===")
    mgr = _env.resolve("PluginManager")[0]()   # ★ 跨世代：3.0 在 core.plugin.manager
    cfg = {}
    cfg_path = ROOT / "schema.json"
    try:
        schema = json.loads(cfg_path.read_text())
        cfg = {k: v.get("default") for k, v in schema.items()
               if isinstance(v, dict) and "default" in v}
    except Exception as e:
        print(f"     (读 schema 失败: {e})")
    inst = mod.AcceleratorPlugin(ctx=None, cfg=cfg)
    mgr.plugin_instances[PLUGIN_ID] = inst
    mgr.plugin_enabled[PLUGIN_ID] = True

    app = FastAPI()
    mgr.set_web_app(app)

    # 认证依赖：用真框架的 require_auth 对象做 override（不是绕过路由，只绕过鉴权）
    try:
        from webui.routes.auth import require_auth
        app.dependency_overrides[require_auth] = lambda: {"user": "itest"}
        check("已覆盖 require_auth 依赖", True)
    except Exception as e:
        check("已覆盖 require_auth 依赖", False, str(e)[:80])

    routes = sorted({getattr(r, "path", "") for r in app.routes})
    plugin_routes = [r for r in routes if "kira_accelerator" in r]
    print(f"     app 上的插件路由: {plugin_routes}")
    check("插件路由已真的挂到 app 上", len(plugin_routes) > 0)

    # ── 2) 真发 HTTP 请求 ──
    print("\n=== 2) 真 HTTP 请求（ASGI 直连，不 mock） ===")
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:

        # 2a 插件自有端点：单数前缀
        for p in ("/health", "/wallpapers"):
            r = await c.get(f"/api/plugin/{PLUGIN_ID}{p}")
            ok = r.status_code == 200
            check(f"GET /api/plugin/{PLUGIN_ID}{p} → 200", ok,
                  f"{r.status_code} {r.text[:70]}")
            if ok and p == "/wallpapers":
                try:
                    lst = r.json()
                    # 后端返回 {"files": [...]}（前端也是这么读的：.files || []）
                    names = (lst.get("files", []) if isinstance(lst, dict)
                             else (lst if isinstance(lst, list) else []))
                    check("壁纸返回体形如 {files:[...]}（与前端读取方式一致）",
                          isinstance(lst, dict) and "files" in lst,
                          str(lst)[:60])
                    check("壁纸列表非空（线上是 0 张）", len(names) > 0,
                          f"{len(names)} 张")
                    if names:
                        nm = names[0] if isinstance(names[0], str) else names[0].get("name")
                        r2 = await c.get(f"/api/plugin/{PLUGIN_ID}/wallpapers/{nm}")
                        ct = r2.headers.get("content-type", "")
                        check(f"壁纸图片可取 ({nm})", r2.status_code == 200
                              and ct.startswith("image/"),
                              f"{r2.status_code} {ct} {len(r2.content)}B")
                except Exception as e:
                    check("壁纸列表可解析", False, str(e)[:80])

        # 2b 框架自带端点（config 等）由 webui 注册，本测试台未挂载 webui，
        #    所以不从 HTTP 判，改成直接查框架源码是否声明了这些复数路径
        fw_routes = set()
        for f in Path(FRAMEWORK).rglob("*.py"):
            if "/.git/" in str(f):
                continue
            try:
                txt = f.read_text(errors="ignore")
            except Exception:
                continue
            fw_routes |= set(re.findall(r'path="(/api/plugins/[^"]+)"', txt))
        print(f"     框架声明的复数路由: {sorted(fw_routes)}")
        check("框架确实声明了 /api/plugins/{plugin_id}/config（复数）",
              any("config" in x for x in fw_routes), str(sorted(fw_routes))[:90])

        # 2c ★ 反向验证：故意用错前缀必须 404
        r = await c.get(f"/api/plugins/{PLUGIN_ID}/health")
        check("★ 反向验证：/api/plugins/<id>/health（复数）确实 404",
              r.status_code == 404, f"{r.status_code} ← 线上就是这个错")

        # ── 3) ★★ 从真前端源码里抽出所有 URL，按各自前缀逐个校验 ──
        print("\n=== 3) ★★ 前端写的每个 URL 都要真的能通 ===")
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        def prefix_of(var):
            # 形如: const PAPI = "/api/plugin/" + encodeURIComponent(PLUGIN_ID);
            m = re.search(r'const\s+' + var + r'\s*=\s*"([^"]*)"\s*\+\s*encodeURIComponent\(PLUGIN_ID\)',
                          html)
            if not m:
                m = re.search(r'const\s+' + var + r'\s*=\s*"([^"]*)"', html)
                return m.group(1) if m else None
            return m.group(1) + PLUGIN_ID

        papi = prefix_of("PAPI")
        api = prefix_of("API")
        print(f"     前端 PAPI（插件端点）= {papi}")
        print(f"     前端 API （框架端点）= {api}")

        check("★ PAPI 前缀 == 框架要求的单数 /api/plugin/<id>",
              (papi or "").rstrip("/") == f"/api/plugin/{PLUGIN_ID}",
              f"前端={papi}")
        check("★ API 前缀 == 框架的复数 /api/plugins/<id>",
              (api or "").rstrip("/") == f"/api/plugins/{PLUGIN_ID}",
              f"前端={api}")

        for var, pref in (("PAPI", papi), ("API", api)):
            used = sorted(set(re.findall(r'(?<![A-Za-z_])' + var + r'\s*\+\s*"([^"]+)"', html)))
            print(f"     {var} 用到的端点: {used}")
            for pth in used:
                if "{" in pth or pth.endswith("/"):
                    continue
                if var == "PAPI":
                    # ★ 按**端点真实的方法**发请求。
                    #   原来一律用 GET，而新加的 upload/delete 是 POST
                    #   ⇒ 405/404 被误判成"端点不通"（前端其实是对的）。
                    #   判据：**只要不是 404 就说明路由存在**（405 = 存在但方法不对）。
                    if pth.endswith("/upload") or pth.endswith("/delete") \
                            or "reset" in pth:
                        r = await c.post(pref + pth, json={})
                    else:
                        r = await c.get(pref + pth)
                    check(f"PAPI {pth} 真的能通", r.status_code != 404, f"{r.status_code}")
                else:
                    check(f"API {pth} 是框架声明的路由",
                          any(pth in x for x in fw_routes), f"声明={sorted(fw_routes)}")

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过")


asyncio.run(main())
