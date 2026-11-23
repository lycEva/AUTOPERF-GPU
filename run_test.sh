#!/bin/bash
# ============================================================================
# run_test.sh —— 一键压测（GPU 版，监控口径与 DCU 工具一致）
#
# 监控不再依赖 ServerAgent(7777)。压测前先做一次监控预检，预检不通过就不施压；
# 压测结束后自动产出 metrics.csv 与四张 PNG 曲线（外加 overview.png）。
#
# 预热（--warmup N）：先施压 N 秒但不记录——请求不进 JTL/聚合/HTML 报告，
# 监控也不做周期采样；正式测量窗口内才开始全面记录。第五个参数始终表示
# 正式测量时长，不是含预热的总时长。
#
# 用法（推荐装 CLI：bash install.sh 之后任意目录直接 gputest run ...）:
#   gputest run <jmx路径> <结果目录> <容器名> <线程数> <持续秒数> \
#     [--service 服务名] [选项...]
#   # 等价旧写法（相对路径按调用目录解析，与工具目录无关）:
#   bash run_test.sh <jmx路径> <结果目录> <容器名> <线程数> <持续秒数> \
#     [--warmup 0] [--monitor-containers a,b] [--service 服务名] [--port 58096] \
#     [--monitor-interval 0.3] [--monitor-timeout 2] \
#     [--server-config 本地server_config.json --config-workers <整数> --config-threads <整数> \
#      [--config-target-path /容器内/绝对路径] [--config-wait 30] [--config-settle 5]]
#
#   监控固定为 direct 直采（JMX 已移除 PerfMon 监听器），没有 --monitor-mode。
#
#   改配置 + 压测一条命令：同时给出 --server-config / --config-workers /
#   --config-threads 时，会先改本地 server_config.json 的 workers/threads，
#   docker cp 进容器并重启，等容器内服务端口就绪（最多 --config-wait 秒）后
#   再开始生成运行副本与施压；等不到就绪直接失败退出，不打到没起来的服务。
#
#   容器名 = 要压测/监控的容器；服务名 = ports.json 的 key，决定 JMX 端口（必传）。
#   --port 直接指定端口，优先级最高（只改运行副本）。
#
#   A 调用 B 的场景：压测目标仍是 A，用 --monitor-containers A,B 把两个容器
#   画进同一批图里；两个无关服务的压测各跑一次 run_test.sh，各用自己的结果目录。
#
# 示例:
#   bash run_test.sh ./yc.jmx ./result hexocr-hexocr-1 3 900
#   bash run_test.sh ./yc.jmx ./result hexocr-hexocr-1 3 600 --warmup 120
#   # 一条命令：改成 workers=2/threads=4 → 重启 → 就绪后压 900 秒
#   bash run_test.sh ./yc.jmx ./result bsocr-container 10 900 --service bsocr \
#     --server-config ./server_config.json --config-workers 2 --config-threads 4
# ============================================================================
set -euo pipefail

# 工具目录：脚本自身位置，用来找 prepare_run.py / summary_monitor / ports.json。
# 它与调用方的当前目录无关，所以从任意目录调用都能跑。
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
# 调用方当前目录：用户给的相对路径（jmx / 结果目录 / server_config.json）按它解析。
INVOCATION_DIR=$PWD

USAGE='用法: gputest run <jmx路径> <结果目录> <容器名> <线程数> <持续秒数> [选项]
     等价旧写法: bash run_test.sh <jmx路径> <结果目录> <容器名> <线程数> <持续秒数> [选项]

位置参数（相对路径按当前目录解析）:
  jmx路径       JMeter 模板，不会被改写（运行副本是结果目录里的 plan.jmx）
  结果目录      结果根目录，每次运行新建 run_<容器>_<时间>_<随机串>/
  容器名        压测 / 监控的容器
  线程数        JMeter 线程数，正整数
  持续秒数      正式测量时长，正整数（不含预热）

选项:
  --service <名>              必传，服务名（ports.json 的 key），决定 JMX 端口
  --port <端口>               1-65535，优先级最高（只改运行副本）
  --ports-config <文件>       换一份端口配置（本地路径）
  --warmup <秒>               预热时长，非负整数，默认 0
  --monitor-containers a,b    采哪些容器，默认等于压测容器
  --monitor-interval <秒>     采样目标间隔，默认 0.3
  --monitor-timeout <秒>      单轮采样 deadline，默认 2
  --server-config <文件>      本地 server_config.json，与下面两项一起给出才改配置
  --config-workers <整数>     写入服务配置的 workers
  --config-threads <整数>     写入服务配置的 threads
  --config-target-path <路径> 容器内目标路径，默认 /hexapp/ai-<服务名>-serving/conf/server_config.json
  --config-wait <秒>          重启后等端口就绪上限，默认 30；0 表示不等
  --config-settle <秒>        就绪后再稳定几秒，默认 5
  -h, --help                  显示这份帮助'

case "${1:-}" in
  -h|--help|help) printf '%s\n' "$USAGE"; exit 0 ;;
esac

if [ "$#" -lt 5 ]; then
  printf '%s\n' "$USAGE" >&2
  exit 1
fi
JMX_FILE=$1
RESULT_DIR=$2
CONTAINER=$3
THREADS=$4
DURATION=$5
shift 5

MONITOR_INTERVAL=${MONITOR_INTERVAL:-0.3}
MONITOR_TIMEOUT=${MONITOR_TIMEOUT:-2}
WARMUP=${WARMUP:-0}
MONITOR_CONTAINERS=${MONITOR_CONTAINERS:-}
PORT=${PORT:-}
SERVICE=${SERVICE:-}
# 改配置 + 压测一条命令：三个参数同时给出才启用改配置步骤
SERVER_CONFIG=${SERVER_CONFIG:-}
CONFIG_WORKERS=${CONFIG_WORKERS:-}
CONFIG_THREADS=${CONFIG_THREADS:-}
CONFIG_TARGET_PATH=${CONFIG_TARGET_PATH:-}
CONFIG_WAIT=${CONFIG_WAIT:-30}
CONFIG_SETTLE=${CONFIG_SETTLE:-5}
PORTS_CONFIG=${PORTS_CONFIG:-}
while [ "$#" -gt 0 ]; do
  if [ "$#" -lt 2 ]; then echo "❌ 选项缺少参数: $1" >&2; exit 1; fi
  case "$1" in
    --monitor-interval) MONITOR_INTERVAL=$2 ;;
    --monitor-timeout) MONITOR_TIMEOUT=$2 ;;
    --warmup) WARMUP=$2 ;;
    --monitor-containers) MONITOR_CONTAINERS=$2 ;;
    --port) PORT=$2 ;;
    --service) SERVICE=$2 ;;
    --ports-config) PORTS_CONFIG=$2 ;;
    --server-config) SERVER_CONFIG=$2 ;;
    --config-workers) CONFIG_WORKERS=$2 ;;
    --config-threads) CONFIG_THREADS=$2 ;;
    --config-target-path) CONFIG_TARGET_PATH=$2 ;;
    --config-wait) CONFIG_WAIT=$2 ;;
    --config-settle) CONFIG_SETTLE=$2 ;;
    *) echo "❌ 未知选项: $1" >&2; exit 1 ;;
  esac
  shift 2
done
# 默认只监控压测目标容器；A 调用 B 的场景用 --monitor-containers a,b 一起看。
[ -n "$MONITOR_CONTAINERS" ] || MONITOR_CONTAINERS=$CONTAINER
if [[ ! "$WARMUP" =~ ^(0|[1-9][0-9]*)$ ]]; then
  echo "❌ 预热秒数必须为非负整数（不含前导零），当前为: ${WARMUP}" >&2; exit 1
fi
if [ -n "$PORT" ]; then
  if [[ ! "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "❌ 端口必须是 1-65535 之间的整数，当前为: ${PORT}" >&2; exit 1
  fi
fi
if [ -z "$SERVICE" ]; then
  echo "❌ --service 必传（服务名，见 ports.json）" >&2; exit 1
fi
if [[ ! "$SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "❌ 服务名格式错误: ${SERVICE}" >&2; exit 1
fi
python3 -c 'import math,sys; values=[float(x) for x in sys.argv[1:]]; sys.exit(0 if all(math.isfinite(x) and x>0 for x in values) else 1)' \
  "$MONITOR_INTERVAL" "$MONITOR_TIMEOUT" || { echo "❌ 采样间隔和超时必须为有限正数（秒）" >&2; exit 1; }
if [[ ! "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "❌ 容器名格式错误" >&2; exit 1
fi
for name in ${MONITOR_CONTAINERS//,/ }; do
  if [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
    echo "❌ 监控容器名格式错误: $name" >&2; exit 1
  fi
done
if [[ ! "$THREADS" =~ ^[1-9][0-9]*$ || ! "$DURATION" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ 线程数和持续秒数必须为正整数" >&2; exit 1
fi

# ===== 改配置 + 压测：三项齐全才启用，半套参数直接拒绝 =====
CONFIG_STEP=0
if [ -n "$SERVER_CONFIG" ] || [ -n "$CONFIG_WORKERS" ] || [ -n "$CONFIG_THREADS" ]; then
  CONFIG_STEP=1
  missing=""
  [ -n "$SERVER_CONFIG" ] || missing="$missing --server-config"
  [ -n "$CONFIG_WORKERS" ] || missing="$missing --config-workers"
  [ -n "$CONFIG_THREADS" ] || missing="$missing --config-threads"
  if [ -n "$missing" ]; then
    echo "❌ 改配置需要同时给出 --server-config / --config-workers / --config-threads（缺少:${missing}）" >&2
    exit 1
  fi
fi
if [ -n "$CONFIG_WORKERS" ] && ! [[ "$CONFIG_WORKERS" =~ ^[0-9]+$ ]]; then
  echo "❌ config-workers 必须是非负整数: $CONFIG_WORKERS" >&2; exit 1
fi
if [ -n "$CONFIG_THREADS" ] && ! [[ "$CONFIG_THREADS" =~ ^[0-9]+$ ]]; then
  echo "❌ config-threads 必须是非负整数: $CONFIG_THREADS" >&2; exit 1
fi
if ! [[ "$CONFIG_WAIT" =~ ^[0-9]+$ ]]; then
  echo "❌ config-wait 必须是非负整数秒: $CONFIG_WAIT" >&2; exit 1
fi
if ! [[ "$CONFIG_SETTLE" =~ ^[0-9]+$ ]]; then
  echo "❌ config-settle 必须是非负整数秒: $CONFIG_SETTLE" >&2; exit 1
fi
if [ -n "$CONFIG_TARGET_PATH" ]; then
  if [[ "$CONFIG_TARGET_PATH" != /* ]]; then
    echo "❌ config-target-path 必须是容器内绝对路径: $CONFIG_TARGET_PATH" >&2; exit 1
  fi
  if [[ "$CONFIG_TARGET_PATH" == *:* ]]; then
    echo "❌ config-target-path 不能包含冒号（docker cp 语法冲突）: $CONFIG_TARGET_PATH" >&2; exit 1
  fi
fi
if [ "$CONFIG_STEP" = 0 ] && { [ -n "$CONFIG_TARGET_PATH" ] || [ "$CONFIG_WAIT" != 30 ]; }; then
  echo "⚠️ 未给 --server-config/--config-workers/--config-threads，忽略配置相关选项（只压测，不改配置）" >&2
fi
# ===== 相对路径语义：用户给的路径按「调用目录」补齐，不按工具目录 =====
# 这样从 /tmp 或任意工程目录调用时，./yc.jmx、./result 都指向调用方自己的文件。
abspath_from_caller() {
  local rel
  case "$1" in
    "") printf '%s\n' "" ;;
    /*) printf '%s\n' "$1" ;;
    *)  rel=$1
        # 顺手去掉 ./ 前缀，报错信息里不出现 /tmp/x/./y.jmx 这种别扭路径
        while [ "${rel#./}" != "$rel" ]; do rel=${rel#./}; done
        printf '%s\n' "$INVOCATION_DIR/$rel" ;;
  esac
}
JMX_FILE=$(abspath_from_caller "$JMX_FILE")
RESULT_DIR=$(abspath_from_caller "$RESULT_DIR")
SERVER_CONFIG=$(abspath_from_caller "$SERVER_CONFIG")
PORTS_CONFIG=$(abspath_from_caller "$PORTS_CONFIG")

if [ ! -f "$JMX_FILE" ]; then echo "❌ JMX文件不存在: $JMX_FILE" >&2; exit 1; fi
if ! command -v nvidia-smi >/dev/null 2>&1; then echo "❌ 找不到 nvidia-smi（GPU 监控必需）" >&2; exit 1; fi
if [ ! -f "$BASE_DIR/prepare_run.py" ]; then echo "❌ 找不到 prepare_run.py" >&2; exit 1; fi

# ===== 定位 JMeter：非登录 shell 的 PATH 里通常没有，做兜底查找 =====
if [ -z "${JMETER:-}" ]; then
  JMETER=$(command -v jmeter 2>/dev/null || true)
fi
if [ -z "${JMETER:-}" ]; then
  for cand in /data/aitest/tools/apache-jmeter-*/bin/jmeter \
              /opt/apache-jmeter-*/bin/jmeter \
              /usr/local/apache-jmeter-*/bin/jmeter; do
    [ -x "$cand" ] && JMETER=$cand && break
  done
fi
if [ -z "${JMETER:-}" ]; then
  echo "❌ 未找到 jmeter，请通过 JMETER 环境变量指定，例如：" >&2
  echo "   JMETER=/data/aitest/tools/apache-jmeter-5.4.1/bin/jmeter bash run_test.sh ..." >&2
  exit 1
fi

if [[ "$RESULT_DIR" != /* ]]; then RESULT_DIR="$INVOCATION_DIR/$RESULT_DIR"; fi
mkdir -p "$RESULT_DIR"
RESULT_DIR=$(cd "$RESULT_DIR" && pwd)

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RUN_DIR=$(mktemp -d "$RESULT_DIR/run_${CONTAINER}_${TIMESTAMP}_XXXXXX")
chmod 755 "$RUN_DIR"
RESULT_PATH="$RUN_DIR/result.jtl"
JMETER_LOG="$RUN_DIR/jmeter.log"
REPORT_DIR="$RUN_DIR/report"
PLAN="$RUN_DIR/plan.jmx"
TOTAL_DURATION=$(( WARMUP + DURATION ))

CONFIG_DESC="未启用（只压测，不改配置）"
if [ "$CONFIG_STEP" = 1 ]; then
  CONFIG_DESC="workers=${CONFIG_WORKERS} threads=${CONFIG_THREADS} → 重启 ${CONTAINER}（等就绪 ${CONFIG_WAIT}s + 稳定 ${CONFIG_SETTLE}s）"
fi

printf '📄 原始模板: %s\n📦 压测容器: %s\n📈 监控容器: %s\n🔌 端口覆盖: %s\n🏷 服务名: %s\n📁 本次运行: %s\n👥 线程数: %s\n⏱️ 正式测量: %s秒\n🔥 预热: %s秒（总施压 %s秒）\n⚙️ 改配置: %s\n🖥 JMeter: %s\n' \
  "$JMX_FILE" "$CONTAINER" "$MONITOR_CONTAINERS" "${PORT:-（未指定，按 ports.json）}" "${SERVICE:-（未指定）}" "$RUN_DIR" "$THREADS" "$DURATION" "$WARMUP" "$TOTAL_DURATION" "$CONFIG_DESC" "$JMETER"

PYTHON_BIN=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON_BIN" ]; then echo "❌ 找不到 python3" >&2; exit 1; fi

# ===== 改服务配置（可选）：改 JSON → docker cp → 重启 → 等端口就绪 =====
# 放在生成运行副本之前：配置没改成功、服务没起来，都不应该进入施压阶段。
if [ "$CONFIG_STEP" = 1 ]; then
  if [ ! -f "$BASE_DIR/apply_server_config.sh" ]; then
    echo "❌ 找不到 apply_server_config.sh（未施压）" >&2; exit 1
  fi
  APPLY_ARGS=("$SERVER_CONFIG" "$CONTAINER" "$SERVICE" "$CONFIG_WORKERS" "$CONFIG_THREADS"
    --run-dir "$RUN_DIR" --wait "$CONFIG_WAIT" --settle "$CONFIG_SETTLE")
  [ -n "$CONFIG_TARGET_PATH" ] && APPLY_ARGS+=(--target-path "$CONFIG_TARGET_PATH")
  [ -n "${PORT:-}" ] && APPLY_ARGS+=(--port "$PORT")
  [ -n "${PORTS_CONFIG:-}" ] && APPLY_ARGS+=(--ports-config "$PORTS_CONFIG")
  if ! bash "$BASE_DIR/apply_server_config.sh" "${APPLY_ARGS[@]}"; then
    echo "❌ 改服务配置 / 等待服务就绪失败（未施压）" >&2; exit 1
  fi
fi

# 原始 JMX 不改写：先在运行目录生成独立副本 plan.jmx
echo "🛠 生成运行副本（预热 ${WARMUP}s / 正式 ${DURATION}s）..."
PREPARE_ARGS=(--template "$JMX_FILE" --run-dir "$RUN_DIR" --container "$CONTAINER"
  --threads "$THREADS" --duration "$DURATION" --warmup "$WARMUP")
if [ -n "$PORT" ]; then PREPARE_ARGS+=(--port "$PORT"); fi
if [ -n "$SERVICE" ]; then PREPARE_ARGS+=(--service "$SERVICE"); fi
[ -n "${PORTS_CONFIG:-}" ] && PREPARE_ARGS+=(--ports-config "$PORTS_CONFIG")
"$PYTHON_BIN" "$BASE_DIR/prepare_run.py" "${PREPARE_ARGS[@]}" || {
  echo "❌ 生成运行配置失败" >&2; exit 1; }

# 运行副本上再做容器 PID 映射（线程数/时长与 prepare_run 保持一致）
UPDATE_PY=""
for cand in "$BASE_DIR/update_jmx_pid.py" "$BASE_DIR/../update_jmx_pid.py"; do
  [ -f "$cand" ] && UPDATE_PY="$cand" && break
done
if [ -n "$UPDATE_PY" ]; then
  echo "🛠 更新JMX PID映射: $UPDATE_PY"
  "$PYTHON_BIN" "$UPDATE_PY" "$PLAN" "$CONTAINER" "$THREADS" "$TOTAL_DURATION" || {
    # PID 映射是 legacy 步骤（GPU 版已无 ServerAgent/PerfMon），失败只告警不中断压测
    echo "⚠️ JMX PID 映射失败，跳过该步骤继续压测" >&2; }
else
  echo "⚠️ 未找到 update_jmx_pid.py，跳过 PID 替换" >&2
fi

JMETER_ARGS=("$JMETER" -n -t "$PLAN" -l "$RESULT_PATH" -j "$JMETER_LOG"
  -e -o "$REPORT_DIR" -Jthreads="$THREADS" -Jduration="$TOTAL_DURATION")

# exec 保留调用方 PID：TERM/INT 直接到达负责收尾的进程
exec "$PYTHON_BIN" "$BASE_DIR/summary_monitor/run_direct.py" \
  --container "$MONITOR_CONTAINERS" --run-dir "$RUN_DIR" \
  --interval "$MONITOR_INTERVAL" --timeout "$MONITOR_TIMEOUT" --warmup "$WARMUP" \
  -- "${JMETER_ARGS[@]}"
