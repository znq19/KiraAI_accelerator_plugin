#!/bin/sh
# 全量自检 —— 一条命令跑完所有测试
#
# 用法：sh run_tests.sh          （在插件根目录执行）
#
# 需要真框架的几个集成测试（真路由 / 真流式 / 真思考注入 / 代理类型）会在
# 找不到 KiraAI 源码时**自动跳过**；要跑它们就设 KIRA_FW：
#     KIRA_FW=/path/to/KiraAI sh run_tests.sh
set -e
cd "$(dirname "$0")"

echo "════ KiraAI 提速器 · 全量自检 ════"
fail=0
RAN=""
SKIPPED=""

run() {
  name="$1"; shift
  file="$1"
  RAN="$RAN $(basename "$file")"
  printf '\n──── %s ────\n' "$name"
  # ★ 硬超时：几个集成测试会起真服务器，卡住不能让整套挂死
  if ! timeout -k 5 240 python3 "$@" > /tmp/_accel_out 2>&1; then
    fail=$((fail+1)); echo "❌ $name 失败"
    # ★ 只输出"失败项"与逐条 ✗，而不是尾部 40 行 ——
    #   否则偶发失败时看不到究竟是哪条断言（之前吃过这个亏）
    echo "   ── 未通过的断言 ──"
    grep -E "^  ✗" /tmp/_accel_out | head -12 | sed 's/^/   /'
    echo "   ── 末尾输出 ──"
    tail -12 /tmp/_accel_out | sed 's/^/   /' 
  elif grep -qF "[SKIP]" /tmp/_accel_out; then
    # 缺框架而跳过 —— 不能伪装成"通过"，否则"全绿"是假的
    SKIPPED="$SKIPPED $name"; echo "⏭  $name 跳过（见下一行说明）"
  else
    echo "✅ $name 通过"
  fi
}

# 顺序：先纯逻辑（快、无依赖），再需要真框架的集成测试
run "接管护栏 patches"        tests/test_patches.py
run "段切分 stream_first"     tests/test_stream_first.py
run "抢先发送 early_sent"     tests/test_early_sent.py
run "流式引擎 stream_engine"  tests/test_stream_engine.py
run "自动思考 auto_thinking"  tests/test_auto_thinking.py
run "工具并行 parallel"       tests/test_parallel.py
run "并行等价性 parallel_eq"  tests/test_parallel_equiv.py
run "壁纸轮换 rotation"       tests/test_rotation.py
run "插件兼容性 compat"       tests/test_compat.py
run "装载与还原 load"         tests/test_load.py
run "★会话路由不串"           tests/test_session_routing.py
run "★消息间隔"               tests/test_message_pacing.py
run "★客户端复用缓存"         tests/test_client_cache.py
run "★接管点精确还原"         tests/test_patch_restore.py
run "★记忆落盘优化"           tests/test_memory_dump.py
run "★抢发后的消息ID对齐"     tests/test_early_sent_alignment.py
run "★抢发交接处的节奏"       tests/test_pacing_handoff.py
run "★重复发送：回退策略"     tests/test_dup_send_guard.py
run "★重复发送：全链路"       tests/test_dup_send_e2e.py

run "前端URL前缀守卫"         tests/test_frontend_api.py
run "★真框架API集成"          tests/test_real_api.py
run "★代理类型透传"           tests/test_proxy_isinstance.py
run "★真流式端到端"           tests/test_stream_real.py
run "★壁纸轮换真逻辑"         tests/test_rotation_live.py
run "★思考注入覆盖面"         tests/test_thinking_providers.py

# ★ 守卫：tests/ 里每个测试文件都必须被上面跑到。
#   之前就吃过亏 —— 脚本引用了 16 个文件，仓库里只提交了 8 个，
#   结果别人 clone 下来跑不起来。现在漏加会当场报错。
MISSING=""
for f in tests/test_*.py; do
  case " $RAN " in
    *" $(basename "$f") "*) ;;
    *) MISSING="$MISSING $(basename "$f")" ;;
  esac
done
if [ -n "$MISSING" ]; then
  echo "❌ 有测试文件没被 run_tests.sh 引用:$MISSING"
  fail=$((fail+1))
fi

printf '\n════════════════════════════\n'
if [ -n "$SKIPPED" ]; then
  echo "⏭  跳过:$SKIPPED"
  echo "   （需要 KiraAI 框架源码；设 KIRA_FW=/path/to/KiraAI 后重跑）"
fi
if [ "$fail" -eq 0 ]; then echo "🎉 全部通过"; else echo "❌ $fail 个套件失败"; exit 1; fi
