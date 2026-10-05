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
  # ★ 按扩展名选解释器：前端那条"真引擎跑转场"的测试是 Node 写的
  #   （它要在最小 DOM/WAAPI 桩里**真的执行**壁纸引擎，python 做不到）。
  case "$file" in
    *.js) RUNTIME="node" ;;
    *)    RUNTIME="python3" ;;
  esac
  # ★ 硬超时：几个集成测试会起真服务器，卡住不能让整套挂死
  if ! timeout -k 5 240 $RUNTIME "$@" > /tmp/_accel_out 2>&1; then
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
run "★重复发送：多步循环"     tests/test_dup_send_multistep.py
run "★重复发送：按步抢发"     tests/test_dup_per_step.py
run "★重复发送：跨轮次(优先级)" tests/test_dup_across_turns.py
run "★与xml修复器兼容（剥离）" tests/test_compat_xmlfixer.py
run "★抢发兼容AFTER_XML_PARSE" tests/test_after_xml_parse.py
run "★文本被改写后的剥离"     tests/test_strip_when_text_rewritten.py
run "★前端审计（层级/开屏/墨渗）" tests/test_frontend_audit.py
run "★计数保留期（30天/永久）" tests/test_stats_retention.py
run "★计数清理安全性（不误伤）" tests/test_stats_safety.py
run "★v1.0.2 新功能（多选/扫光/外链）" tests/test_v102_features.py
run "★壁纸切换特效（防硬切/燃纸）" tests/test_wallpaper_fx.py
run "★★★壁纸图层顺序（两方向）" tests/test_wallpaper_layer_order.py
run "★★★壁纸转场行为（真引擎）" tests/test_wallpaper_transition.js
run "★日志行为（不误导/不误报）" tests/test_log_behaviour.py
run "★媒体描述不计入评分"     tests/test_thinking_media.py
run "★台账生命周期（防误告警）" tests/test_ledger_lifecycle.py
run "★★跨轮清理安全（防重叠误清）" tests/test_cross_turn_clear_safety.py
run "★★生产抢发契约（emit必返回）" tests/test_production_emit_contract.py
run "★★不可解析段安静交回"     tests/test_unparsable_handback.py
run "★★跨会话路由（并发/漂移）" tests/test_cross_session_routing.py
run "★★故障转移不重复（failover）" tests/test_failover_no_dup.py
run "★★★插件接管识别（防重复发送）" tests/test_plugin_takeover.py


run "前端URL前缀守卫"         tests/test_frontend_api.py
run "★真框架API集成"          tests/test_real_api.py
run "★代理类型透传"           tests/test_proxy_isinstance.py
run "★真流式端到端"           tests/test_stream_real.py
run "★壁纸轮换真逻辑"         tests/test_rotation_live.py
run "★思考注入覆盖面"         tests/test_thinking_providers.py
run "★★思考控制权（压住/打开）" tests/test_thinking_conflict.py
run "★★默认值迁移（仅一次）"   tests/test_thinking_default_migration.py

# ★ 守卫：tests/ 里每个测试文件都必须被上面跑到。
#   之前就吃过亏 —— 脚本引用了 16 个文件，仓库里只提交了 8 个，
#   结果别人 clone 下来跑不起来。现在漏加会当场报错。
MISSING=""
for f in tests/test_*.py tests/test_*.js; do
  [ -e "$f" ] || continue
  b="$(basename "$f")"
  case "$b" in
    # 显式豁免：_env 是**公共辅助模块**不是测试（它不叫 test_* 也进不来，
    # 这里只是把意图写清楚，防止以后有人误加）
    _env.py) continue ;;
  esac
  case " $RAN " in
    *" $b "*) ;;
    *) MISSING="$MISSING $b" ;;
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
