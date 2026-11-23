#!/usr/bin/env bash
# ============================================================================
# gpu_monitor.sh —— 压测期间 GPU / 进程指标采集（替代 ServerAgent）
#
# 采集口径（按运行模式区分统计对象）：
#   gpu  (默认) : 只统计“占卡进程” —— 容器 PID ∩ nvidia-smi 占用 GPU 的 PID
#                 sm%(GPU 利用率) 与 fb(显存 MiB) 按卡累加；CPU/RSS 只累加这些进程
#   cpu         : 只统计“服务主进程树” —— 容器 init 进程及其全部后代
#                 CPU/RSS 按整棵树累加；GPU 列记 0
#   auto        : 交集非空则按 gpu，否则按 cpu
#
# 用法：
#   bash gpu_monitor.sh <container> <output_dir> [duration] [interval] [mode]
#
#   container   目标服务容器名
#   output_dir  输出目录（不存在会自动创建）
#   duration    采集时长(秒)，0 或不填 = 一直采到 SIGTERM / 停止文件
#   interval    采样间隔(秒)，默认 1（与原 jmx 的 interval_grouping=1000 一致）
#   mode        gpu | cpu | auto，默认 gpu
#
# 可选环境变量：
#   MONITOR_MODE         同 mode 参数（参数优先）
#   MONITOR_PIDS         直接指定 PID 列表(逗号分隔)，跳过容器解析（离线验证用）
#   MONITOR_ROOT_PID     cpu 模式下进程树的根 PID，默认取容器 init 进程
#   MONITOR_PID_REFRESH  刷新 PID 的周期(采样次数)，默认 1（每秒刷新）
#   MONITOR_TS           输出文件名的时间戳后缀（run_test.sh 会传入以对齐批次）
#   MONITOR_GPU          只采集指定 GPU 序号（逗号分隔），默认全部
#
# 输出文件（<ts> = MONITOR_TS）：
#   gpu_<ts>.csv         每秒一行：模式 + GPU 累加指标 + 统计对象 CPU/内存累加
#   gpu_proc_<ts>.csv    每秒每进程一行：是否占卡、是否计入统计、卡号、sm%、fb、CPU、RSS
#   gpu_perfmon_<ts>.jtl PerfMon 兼容格式（值与旧 perfmon_*.jtl 同列结构）
#   gpu_<ts>_summary.txt 汇总（模式、均值/峰值）
# ============================================================================

set -uo pipefail

CONTAINER=${1:-}
OUT_DIR=${2:-}
DURATION=${3:-0}
INTERVAL=${4:-1}
MODE_REQ=${5:-${MONITOR_MODE:-gpu}}

usage() {
  cat <<'EOF'
用法: bash gpu_monitor.sh <container> <output_dir> [duration] [interval] [mode]

  container   目标服务容器名
  output_dir  输出目录（不存在会自动创建）
  duration    采集时长(秒)，0 或不填 = 一直采到 SIGTERM / 停止文件
  interval    采样间隔(秒)，默认 1
  mode        gpu | cpu | auto，默认 gpu
              gpu  = 只统计占卡进程（容器 PID ∩ nvidia-smi 占用 PID）
              cpu  = 只统计服务主进程树（容器 init 及全部后代）
              auto = 交集非空按 gpu，否则按 cpu

可选环境变量:
  MONITOR_MODE         同 mode 参数（参数优先）
  MONITOR_PIDS         直接指定 PID 列表(逗号分隔)
  MONITOR_ROOT_PID     cpu 模式进程树根 PID，默认容器 init
  MONITOR_PID_REFRESH  刷新 PID 周期(采样次数)，默认 1
  MONITOR_TS           输出文件名的时间戳后缀
  MONITOR_GPU          只采集指定 GPU 序号(逗号分隔)

示例:
  bash gpu_monitor.sh hexocr ./result 60 1            # GPU 模式
  bash gpu_monitor.sh hexocr ./result 60 1 cpu        # CPU 模式
EOF
  exit 1
}

[ -z "$CONTAINER" ] || [ -z "$OUT_DIR" ] && usage

case "$MODE_REQ" in
  gpu|cpu|auto) ;;
  *) echo "❌ mode 非法: $MODE_REQ（可选 gpu / cpu / auto）" >&2; exit 1 ;;
esac

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "❌ 未找到 nvidia-smi" >&2
  exit 1
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || [ "$(awk -v v="$INTERVAL" 'BEGIN{print (v>0)?1:0}')" != "1" ]; then
  echo "⚠️  interval 非法($INTERVAL)，回退为 1s" >&2
  INTERVAL=1
fi

case "${DURATION:-0}" in
  ''|*[!0-9]*) DURATION=0 ;;
esac

mkdir -p "$OUT_DIR" || exit 1

TS=${MONITOR_TS:-$(date +%Y%m%d_%H%M%S)}
SUMMARY_CSV="$OUT_DIR/gpu_${TS}.csv"
PROC_CSV="$OUT_DIR/gpu_proc_${TS}.csv"
PERFMON_JTL="$OUT_DIR/gpu_perfmon_${TS}.jtl"
SUMMARY_TXT="$OUT_DIR/gpu_${TS}_summary.txt"
STOP_FLAG="$OUT_DIR/.gpu_monitor_$$.stop"

STOP=0
on_stop() { STOP=1; }
trap on_stop INT TERM
trap 'rm -f "$STOP_FLAG"' EXIT

# ---- 容器进程：pid + ppid 映射 --------------------------------------------
container_ppid_map() {
  if [ -n "${MONITOR_PIDS:-}" ]; then
    # 离线指定 PID 时没有父子关系信息，用 ps 补 ppid
    local p
    for p in ${MONITOR_PIDS//,/ }; do
      [ -r "/proc/$p/stat" ] || continue
      ppid=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null)
      [ -n "${ppid:-}" ] && echo "$p $ppid"
    done
    return 0
  fi
  docker top "$CONTAINER" -eo pid,ppid 2>/dev/null \
    | awk 'NR > 1 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {print $1, $2}'
}

container_root_pid() {
  if [ -n "${MONITOR_ROOT_PID:-}" ]; then
    echo "$MONITOR_ROOT_PID"
    return
  fi
  local p
  p=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null)
  case "$p" in ''|*[!0-9]*) p="" ;; esac
  if [ -z "$p" ]; then
    # 兜底：容器内没有 init（如 --pid=host）时取第一个进程
    p=$(echo "${1:-}" | awk 'NR == 1 {print $1}')
  fi
  echo "$p"
}

# ---- 服务主进程树：root 及其全部后代 --------------------------------------
tree_pids() {
  local root="$1" map="$2"
  [ -z "$root" ] && return 0
  echo "$map" | awk -v root="$root" '
    { parent[$1] = $2; pids[$1] = 1 }
    END {
      t[root] = 1
      do {
        changed = 0
        for (p in pids) {
          if (!(p in t) && (parent[p] in t)) { t[p] = 1; changed = 1 }
        }
      } while (changed)
      for (p in t) print p
    }'
}

# ---- GPU 设备级指标 --------------------------------------------------------
gpu_devices() {
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
    --format=csv,noheader,nounits 2>/dev/null \
    | awk -F',' -v want="${MONITOR_GPU:-}" '
      BEGIN { if (want != "") { n = split(want, w, ","); for (i = 1; i <= n; i++) keep[w[i] + 0] = 1 } }
      {
        gsub(/[ \t]/, "", $0)
        split($0, f, ",")
        idx = f[1] + 0
        if (want != "" && !(idx in keep)) next
        printf "%d %s %s %s\n", idx, f[2] + 0, f[3] + 0, f[4] + 0
      }'
}

# ---- 进程级 GPU 指标（pmon，按表头取列）-----------------------------------
# 输出：pid gpu_index sm_pct fb_mib
gpu_processes() {
  nvidia-smi pmon -c 1 -s um -o T 2>/dev/null \
    | awk '
      /^#Time/ {
        for (i = 1; i <= NF; i++) {
          if ($i == "gpu") gi = i
          else if ($i == "pid") pi = i
          else if ($i == "sm")  si = i
          else if ($i == "fb")  fi = i
        }
        next
      }
      /^#/ { next }
      NF > 0 && pi {
        sm = $(si); fb = $(fi)
        if (sm == "-" || sm == "") sm = 0
        if (fb == "-" || fb == "") fb = 0
        printf "%s %s %s %s\n", $(pi), $(gi), sm, fb
      }'
}

# ---- 进程 RSS / 命令 ------------------------------------------------------
# 不取 ps 的 %cpu（生命周期均值，压测中不敏感）
ps_info() {
  local list="$1"
  [ -z "$list" ] && return 0
  ps -o pid= -o rss= -o comm= -p "$(echo "$list" | tr ' ' ',')" 2>/dev/null \
    | awk 'NF >= 3 {
        pid = $1; rss = $2
        cmd = $3
        for (i = 4; i <= NF; i++) cmd = cmd " " $i
        gsub(/,/, "_", cmd)
        if (length(cmd) > 40) cmd = substr(cmd, 1, 40)
        printf "%s %s %s\n", pid, rss, cmd
      }'
}

# ---- /proc/<pid>/stat 累计 CPU 时间（jiffies）-----------------------------
read_cpu_ticks() {
  local p files=()
  for p in $1; do
    [ -r "/proc/$p/stat" ] && files+=("/proc/$p/stat")
  done
  [ ${#files[@]} -eq 0 ] && return 0
  awk '{
    line = $0
    sub(/^.*\) /, "", line)
    n = split(line, f, " ")
    fn = FILENAME
    sub(/^\/proc\//, "", fn)
    sub(/\/stat$/, "", fn)
    print fn, f[12] + f[13]
  }' "${files[@]}"
}

# ---- PerfMon 兼容行 --------------------------------------------------------
emit_perfmon() {
  local ts="$1" label="$2" value="$3"
  printf '%s,0,"%s",,,,,true,,%d,0,0,0,null,0,0,0\n' "$ts" "$label" "${value%.*}"
}

# ---- 初始化 ----------------------------------------------------------------
PPID_MAP="$(container_ppid_map)"
PID_STR="$(echo "$PPID_MAP" | awk 'NF {print $1}' | tr '\n' ' ')"
PID_STR="$(echo $PID_STR)"
ROOT_PID="$(container_root_pid "$PPID_MAP")"
TREE_STR=""

echo "time,iso_time,mode,gpu_device_count,gpu_device_util_avg_pct,gpu_device_util_max_pct,gpu_mem_used_mib,gpu_mem_total_mib,proc_count,gpu_proc_sm_pct_sum,gpu_proc_mem_mib,container_proc_count,cpu_pct_sum,rss_mib_sum" > "$SUMMARY_CSV"
echo "time,iso_time,pid,command,counted,on_gpu,gpu_index,gpu_sm_pct,gpu_mem_mib,cpu_pct,rss_mib" > "$PROC_CSV"
: > "$PERFMON_JTL"

echo "📈 监控启动"
echo "   容器        : $CONTAINER"
echo "   请求模式    : $MODE_REQ"
echo "   进程树根    : ${ROOT_PID:-<未知>}"
echo "   容器进程 PID: ${PID_STR:-<未解析到，等待刷新>}"
echo "   间隔 / 时长 : ${INTERVAL}s / ${DURATION:-0}s"
echo "   输出        : $SUMMARY_CSV"
echo "                $PROC_CSV"

START_MS=$(date +%s%3N)
NEXT_MS=$START_MS
CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
PREV_TS_MS=$START_MS
declare -A PREV_TICKS=()
INTERVAL_MS=$(awk -v i="$INTERVAL" 'BEGIN { printf "%d", i * 1000 }')
CYCLE=0
REFRESH=${MONITOR_PID_REFRESH:-1}

COUNT=0
MODE_LAST=$MODE_REQ
GPU_MODE_SAMPLES=0
UTIL_SUM=0
UTIL_MAX=0
SM_SUM_TOTAL=0
GPUMEM_MAX=0
CPU_SUM_TOTAL=0
CPU_MAX=0
RSS_MAX=0
PROC_MAX=0

while [ "$STOP" -eq 0 ]; do
  CYCLE=$((CYCLE + 1))
  NOW_MS=$(date +%s%3N)
  ISO=$(date '+%F %T')

  # ---- PID 集合 / 进程树（默认每秒刷新）----
  if [ -z "$PID_STR" ] || [ $((CYCLE % REFRESH)) -eq 0 ]; then
    PPID_MAP="$(container_ppid_map)"
    PID_STR="$(echo "$PPID_MAP" | awk 'NF {print $1}' | tr '\n' ' ')"
    PID_STR="$(echo $PID_STR)"
    TREE_STR="$(tree_pids "$ROOT_PID" "$PPID_MAP" | tr '\n' ' ')"
    TREE_STR="$(echo $TREE_STR)"
  fi

  GPU_OUT=$(gpu_devices)
  PMON_OUT=$(gpu_processes)
  PS_OUT=$(ps_info "$PID_STR")

  # ---- 进程表（RSS / 命令）----
  declare -A PS_RSS=() PS_CMD=() IS_PID=()
  while read -r p rss cmd; do
    [ -z "${p:-}" ] && continue
    PS_RSS[$p]=$rss
    PS_CMD[$p]=$cmd
    IS_PID[$p]=1
  done <<< "$PS_OUT"

  # ---- 容器 CPU：/proc 增量 => 瞬时占用 ----
  declare -A PS_CPU=()
  DT_MS=$(( NOW_MS - PREV_TS_MS ))
  [ "$DT_MS" -le 0 ] && DT_MS=1000
  TICKS_OUT=$(read_cpu_ticks "$PID_STR")
  while read -r tp ticks; do
    [ -z "${tp:-}" ] && continue
    prev=${PREV_TICKS[$tp]:-}
    if [ -n "$prev" ] && [ "$ticks" -ge "$prev" ] 2>/dev/null; then
      PS_CPU[$tp]=$(awk -v d=$((ticks - prev)) -v dt="$DT_MS" -v hz="$CLK_TCK" \
        'BEGIN { printf "%.2f", (dt > 0 ? d * 1000 / dt / hz * 100 : 0) }')
    else
      PS_CPU[$tp]=0          # 首轮 / 进程刚重建，本轮记 0
    fi
    PREV_TICKS[$tp]=$ticks
  done <<< "$TICKS_OUT"
  PREV_TS_MS=$NOW_MS

  # ---- 占卡进程：容器 PID ∩ nvidia-smi PID ----
  declare -A GPU_SM=() GPU_FB=() PID_GPU=() PID_SM=() PID_FB=() IS_GPU_PID=()
  while read -r p g sm fb; do
    [ -z "${p:-}" ] && continue
    [ -z "${IS_PID[$p]:-}" ] && continue
    pid_key="$p:$g"
    PID_GPU[$p]="${PID_GPU[$p]:-} $g"
    PID_SM[$pid_key]=$sm
    PID_FB[$pid_key]=$fb
    GPU_SM[$g]=$(( ${GPU_SM[$g]:-0} + sm ))
    GPU_FB[$g]=$(( ${GPU_FB[$g]:-0} + fb ))
    IS_GPU_PID[$p]=1
  done <<< "$PMON_OUT"

  # ---- 模式判定与统计对象 ----
  MODE_EFF=$MODE_REQ
  if [ "$MODE_REQ" = "auto" ]; then
    if [ ${#IS_GPU_PID[@]} -gt 0 ]; then MODE_EFF=gpu; else MODE_EFF=cpu; fi
  fi
  MODE_LAST=$MODE_EFF

  declare -A IS_COUNTED=()
  if [ "$MODE_EFF" = "gpu" ]; then
    # 只算占卡进程
    for p in "${!IS_GPU_PID[@]}"; do IS_COUNTED[$p]=1; done
  else
    # 只算服务主进程树
    for p in $TREE_STR; do IS_COUNTED[$p]=1; done
  fi
  PROC_COUNT=${#IS_COUNTED[@]}

  # ---- GPU 累加（非 gpu 模式记 0）----
  SM_SUM=0
  FB_SUM=0
  if [ "$MODE_EFF" = "gpu" ]; then
    for g in "${!GPU_SM[@]}"; do
      SM_SUM=$((SM_SUM + ${GPU_SM[$g]}))
      FB_SUM=$((FB_SUM + ${GPU_FB[$g]}))
    done
    GPU_MODE_SAMPLES=$((GPU_MODE_SAMPLES + 1))
  fi

  # ---- 设备级 GPU（整卡，交叉核对用）----
  if [ -n "$GPU_OUT" ]; then
    read -r DEV_CNT DEV_UTIL_AVG DEV_UTIL_MAX DEV_MEM_USED DEV_MEM_TOTAL <<<"$(echo "$GPU_OUT" | awk '
      { n++; u += $2; if ($2 > m) m = $2; used += $3; total += $4 }
      END { printf "%d %.2f %.2f %d %d\n", n, (n ? u / n : 0), m, used, total }')"
  else
    DEV_CNT=0 DEV_UTIL_AVG=0 DEV_UTIL_MAX=0 DEV_MEM_USED=0 DEV_MEM_TOTAL=0
  fi

  # ---- 统计对象 CPU / 内存累加 ----
  # CPU / 内存统一按容器主进程树统计（cpu/gpu 模式一致）：master、entrypoint、
  # sidecar 这些不占卡的进程同样计入；GPU 两列仍只算占卡进程。
  CPU_SUM=0
  for p in ${TREE_STR:-}; do
    [ -z "${IS_PID[$p]:-}" ] && continue
    CPU_SUM=$(awk -v a="$CPU_SUM" -v b="${PS_CPU[$p]:-0}" 'BEGIN { printf "%.2f", a + b }')
  done
  # 内存同样按主进程树累加，与 CPU 口径一致
  RSS_SUM_KB=0
  for p in ${TREE_STR:-}; do
    [ -z "${IS_PID[$p]:-}" ] && continue
    RSS_SUM_KB=$(( RSS_SUM_KB + ${PS_RSS[$p]:-0} ))
  done
  RSS_SUM_MIB=$(awk -v k="$RSS_SUM_KB" 'BEGIN { printf "%.1f", k / 1024 }')

  echo "$NOW_MS,$ISO,$MODE_EFF,$DEV_CNT,$DEV_UTIL_AVG,$DEV_UTIL_MAX,$DEV_MEM_USED,$DEV_MEM_TOTAL,$PROC_COUNT,$SM_SUM,$FB_SUM,${#IS_PID[@]},$CPU_SUM,$RSS_SUM_MIB" >> "$SUMMARY_CSV"

  # ---- 进程明细（全部容器进程，counted 标记是否计入统计）----
  for p in "${!IS_PID[@]}"; do
    counted=0
    [ -n "${IS_COUNTED[$p]:-}" ] && counted=1
    cpu="${PS_CPU[$p]:-0}"
    rss_mib=$(awk -v k="${PS_RSS[$p]:-0}" 'BEGIN { printf "%.1f", k / 1024 }')
    gpu_list="${PID_GPU[$p]:-}"
    if [ -n "$gpu_list" ]; then
      for g in $gpu_list; do
        echo "$NOW_MS,$ISO,$p,${PS_CMD[$p]:-},$counted,1,$g,${PID_SM[$p:$g]:-0},${PID_FB[$p:$g]:-0},$cpu,$rss_mib" >> "$PROC_CSV"
      done
    else
      echo "$NOW_MS,$ISO,$p,${PS_CMD[$p]:-},$counted,0,,0,0,$cpu,$rss_mib" >> "$PROC_CSV"
    fi
  done

  # ---- PerfMon 兼容输出（只输出计入统计的对象）----
  while read -r idx util used total; do
    emit_perfmon "$NOW_MS" "127.0.0.1 GPU${idx} device_util" "$util" >> "$PERFMON_JTL"
    emit_perfmon "$NOW_MS" "127.0.0.1 GPU${idx} device_mem" "$used" >> "$PERFMON_JTL"
  done <<< "$GPU_OUT"
  for g in "${!GPU_SM[@]}"; do
    emit_perfmon "$NOW_MS" "127.0.0.1 GPU${g} proc_sm_sum" "${GPU_SM[$g]}" >> "$PERFMON_JTL"
    emit_perfmon "$NOW_MS" "127.0.0.1 GPU${g} proc_mem_sum" "${GPU_FB[$g]}" >> "$PERFMON_JTL"
  done
  emit_perfmon "$NOW_MS" "127.0.0.1 total proc_sm_sum" "$SM_SUM" >> "$PERFMON_JTL"
  emit_perfmon "$NOW_MS" "127.0.0.1 total proc_mem_sum" "$FB_SUM" >> "$PERFMON_JTL"
  for p in "${!IS_COUNTED[@]}"; do
    [ -z "${IS_PID[$p]:-}" ] && continue
    emit_perfmon "$NOW_MS" "127.0.0.1 CPU pid=$p" "${PS_CPU[$p]:-0}" >> "$PERFMON_JTL"
    emit_perfmon "$NOW_MS" "127.0.0.1 Memory pid=$p" "$(awk -v k="${PS_RSS[$p]:-0}" 'BEGIN { printf "%d", k / 1024 }')" >> "$PERFMON_JTL"
  done
  emit_perfmon "$NOW_MS" "127.0.0.1 CPU total" "$CPU_SUM" >> "$PERFMON_JTL"
  emit_perfmon "$NOW_MS" "127.0.0.1 Memory total" "$RSS_SUM_MIB" >> "$PERFMON_JTL"

  # ---- 累加统计 ----
  COUNT=$((COUNT + 1))
  UTIL_SUM=$(awk -v a="$UTIL_SUM" -v b="$DEV_UTIL_AVG" 'BEGIN { printf "%.2f", a + b }')
  UTIL_MAX=$(awk -v a="$UTIL_MAX" -v b="$DEV_UTIL_MAX" 'BEGIN { printf "%.2f", (b > a ? b : a) }')
  SM_SUM_TOTAL=$(awk -v a="$SM_SUM_TOTAL" -v b="$SM_SUM" 'BEGIN { printf "%.2f", a + b }')
  [ "$FB_SUM" -gt "$GPUMEM_MAX" ] && GPUMEM_MAX=$FB_SUM
  [ "$PROC_COUNT" -gt "$PROC_MAX" ] && PROC_MAX=$PROC_COUNT
  CPU_SUM_TOTAL=$(awk -v a="$CPU_SUM_TOTAL" -v b="$CPU_SUM" 'BEGIN { printf "%.2f", a + b }')
  CPU_MAX=$(awk -v a="$CPU_MAX" -v b="$CPU_SUM" 'BEGIN { printf "%.2f", (b > a ? b : a) }')
  RSS_MAX=$(awk -v a="$RSS_MAX" -v b="$RSS_SUM_MIB" 'BEGIN { printf "%.1f", (b > a ? b : a) }')

  # ---- 退出条件 ----
  [ -f "$STOP_FLAG" ] && STOP=1
  if [ "$DURATION" != "0" ]; then
    ELAPSED_MS=$(( $(date +%s%3N) - START_MS ))
    [ "$ELAPSED_MS" -ge $((DURATION * 1000)) ] && STOP=1
  fi
  [ "$STOP" -eq 1 ] && break

  NEXT_MS=$(( NEXT_MS + INTERVAL_MS ))
  NOW_MS=$(date +%s%3N)
  WAIT_MS=$(( NEXT_MS - NOW_MS ))
  if [ "$WAIT_MS" -gt 0 ]; then
    sleep "$(awk -v w="$WAIT_MS" 'BEGIN { printf "%.3f", w / 1000 }')"
  else
    NEXT_MS=$NOW_MS
  fi
done

{
  echo "监控汇总"
  echo "容器              : $CONTAINER"
  echo "请求模式 / 实际   : $MODE_REQ / $MODE_LAST"
  if [ "$MODE_LAST" = "gpu" ]; then
    echo "统计对象          : 占卡进程（容器 PID ∩ nvidia-smi 占用 PID）"
  else
    echo "统计对象          : 服务主进程树（根 PID ${ROOT_PID:-未知} 及全部后代）"
  fi
  echo "采样次数          : $COUNT"
  echo "采样间隔          : ${INTERVAL}s"
  if [ "$MODE_REQ" = "auto" ]; then
    echo "GPU 模式采样占比  : $(awk -v a="$GPU_MODE_SAMPLES" -v b="$COUNT" 'BEGIN { printf "%.1f", (b ? a * 100 / b : 0) }') %"
  fi
  echo "计入进程数峰值    : $PROC_MAX"
  echo "GPU 利用率累加均值(各进程 sm 之和) : $(awk -v s="$SM_SUM_TOTAL" -v c="$COUNT" 'BEGIN { printf "%.2f", (c ? s / c : 0) }') %"
  echo "设备利用率均值    : $(awk -v s="$UTIL_SUM" -v c="$COUNT" 'BEGIN { printf "%.2f", (c ? s / c : 0) }') %（单卡峰值 $(printf '%.2f' "$UTIL_MAX")）"
  echo "进程显存累加峰值  : $GPUMEM_MAX MiB"
  echo "统计对象 CPU 均值 : $(awk -v s="$CPU_SUM_TOTAL" -v c="$COUNT" 'BEGIN { printf "%.2f", (c ? s / c : 0) }') %（峰值 $(printf '%.2f' "$CPU_MAX")）"
  echo "统计对象内存峰值  : $RSS_MAX MiB"
  if [ "$MODE_LAST" = "gpu" ] && [ "$PROC_MAX" -eq 0 ]; then
    echo "⚠️ 未检测到占卡进程：若服务实际以 CPU 模式运行，请用 mode=cpu 重新采集"
  fi
  echo "输出文件          :"
  echo "  $SUMMARY_CSV"
  echo "  $PROC_CSV"
  echo "  $PERFMON_JTL"
} | tee "$SUMMARY_TXT"

echo "✅ 监控结束"
