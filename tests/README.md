# 自检套件

插件的行为约束都在这里。跑一条命令即可：

```sh
sh run_tests.sh          # 在插件根目录执行
```

## 两档测试

**纯逻辑套件（20 个）** —— 不需要框架、不需要网络，clone 下来直接跑：
`patches` / `stream_first` / `early_sent` / `stream_engine` / `auto_thinking` /
`parallel` / `parallel_equiv` / `rotation` / `compat` / `load` /
`dup_send_guard` / `v102_features` / `wallpaper_fx` / `frontend_audit` /
`log_behaviour` / `thinking_media` / `thinking_default_migration` / `ledger_lifecycle` 等
（总数会随版本演进，确切清单以 `run_tests.sh` 里不带框架依赖的那些为准）。

**集成套件（22 个）** —— 需要 **KiraAI 框架源码**（少数还需要 `node`），
找不到会**自动跳过**（不算失败）：

| 套件 | 它真的做了什么 |
|---|---|
| `test_frontend_api.py` | 纯静态比对：面板拼的 URL ↔ 后端 `@register.api` 声明 |
| `test_real_api.py` | 真导入框架 → 走框架自己的注册路径建真 FastAPI → **真发 HTTP 请求** |
| `test_proxy_isinstance.py` | 复用框架**真的** `get_default_llm`，验证代理能过全部类型检查 |
| `test_stream_real.py` | 起**真 SSE 服务器**，接真框架客户端，量首字到达时刻 |
| `test_thinking_providers.py` | 用**真客户端**比对三家的请求体，确认思考参数真的注入 |
| `test_rotation_live.py` | 用 **node** 跑从 `index.html` 抽出来的**真脚本** + DOM 垫片 |

要跑集成套件，把框架源码给它（任选一种）：

```sh
KIRA_FW=/path/to/KiraAI sh run_tests.sh      # ① 环境变量
cp -r /path/to/KiraAI kira_fw_v2346/         # ② 放到插件根下
```

> 框架源码只要 `core/` 与 `webui/` 两个目录即可
> （`git archive v2.34.6 core webui`）。

## 为什么这些套件要"真"跑

因为吃过**三次**亏，都是**静态验证正常、运行时其实坏的**：

- 面板只用「注入假数据 + 截图」验证过 ⇒ 截图好看，但 URL 前缀写错、
  切换特效里一个 `TypeError` 把轮换永久卡死，都发现不了。
- 装载自检只测了 install/uninstall ⇒ 从没走过框架的真实调用链，
  于是代理破坏了 `isinstance`、整个消息链路全挂，测试还是全绿。
- **全部 `test_dup_*` 都自己写 emit 回调** ⇒ 从没走过**生产路径** `_make_engine`，
  于是那里"忘了 return"的回调让抢发**只对第一段生效**整整一版，测试还是全绿
  （2026-09-28 发现；补了 `test_production_emit_contract.py` 走生产路径）。

所以现在的规矩是：**打补丁类改动，必须把框架真实调用方跑一遍**；
**前端改动，必须让真脚本真跑一次**；
**改生产回调/工厂，必须从真实的构造入口进（别自己写替代品）**。

## 反向验证

关键守卫都带反向验证 —— 拿"改之前的版本"跑，必须报红。例如：

```sh
PANEL_HTML=/path/to/old/index.html python3 tests/test_rotation_live.py
# ⇒ 5 条报红，其中「6 秒内换过多张 → 1 张」正是当时的线上症状
```

守卫得证明自己有毒，否则它只是个装饰。

## 写新测试时注意

- 用 `tests/_env.py` 定位插件根与框架源码，**不要写死路径**
  （之前几个测试硬编码了开发机路径，结果 import 到的是别处的副本）。
- 顶层 `let` 不会挂到 `globalThis` ⇒ 需要驱动前端脚本时，
  在脚本末尾追加 `globalThis.__hook = { get wpBusy(){return wpBusy}, … }` 暴露状态。
- node 里跑前端脚本**必须 `process.exit(0)`**，否则 `setInterval(poll, 3000)`
  会让事件循环永远活着。
- 起本地 HTTP 服务当测试夹具，**必须用 `ThreadingHTTPServer`** ——
  单线程 + HTTP/1.1 keep-alive 下第二条连接永远排不上队，测试会卡死。
