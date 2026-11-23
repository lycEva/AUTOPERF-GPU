#!/usr/bin/env bash
# ============================================================================
# apply_server_config.sh —— 改 server_config.json → 同步进容器 → 重启 → 等端口就绪
#
# 这是 run_test.sh「改配置 + 压测一条命令」的配置阶段，单独跑也可以：
#
# 用法:
#   bash apply_server_config.sh <server_config.json路径> <容器名> <服务名> <workers> <threads> \
#     [--target-path /容器内/绝对路径] [--wait 30] [--settle 5] \
#     [--port 58096] [--ports-config ports.json] [--run-dir 归档目录]
#
#   先改本地 JSON 的 workers/threads，再 docker cp 进容器、docker restart（这三步
#   由 update_server_config.sh 完成），然后等 127.0.0.1:<服务端口> 可连：
#     - 就绪后可选再稳定 --settle 秒（让线程池/模型完全拉起来）；
#     - 超过 --wait 秒仍未就绪 → 退出码 1，调用方据此在施压前中止。
#
#   端口口径与压测共用同一套规则：--port 最高优先级，否则查 --ports-config
#   （默认脚本同目录 ports.json）里的 <服务名>。
#
#   --wait 0 表示重启后不做就绪探测（不推荐，只用于确认失败链路的场景）。
#
#   给了 --run-dir 时，会往里写两份归档：
#     server_config.json   本次生效的本地配置快照
#     config_change.json   改了什么、容器内目标路径、就绪探测结果
#
# 示例:
#   bash apply_server_config.sh ./server_config.json bsocr-container bsocr 2 4
#   bash apply_server_config.sh ./server_config.json bsocr-container bsocr 2 4 \
#     --target-path /opt/app/conf/server_config.json --wait 120
# ============================================================================
set -euo pipefail

BASE_DIR=$(cd "$(dirname "$0")" && pwd)

usage() {
  cat <<'EOF'
Usage:
  bash apply_server_config.sh <server_config.json_path> <container_name> <service_name> <workers> <threads> \
    [--target-path CONTAINER_PATH] [--wait SECONDS] [--settle SECONDS] \
    [--port PORT] [--ports-config FILE] [--run-dir DIR]

This script will:
  1. Update "workers" / "threads" in the local server_config.json
  2. Copy it into the container and restart the container (update_server_config.sh)
  3. Wait until 127.0.0.1:<service port> accepts TCP connections (up to --wait seconds)
     then optionally settle for --settle seconds
EOF
}

if [[ $# -lt 5 ]]; then
  usage >&2
  exit 1
fi

config_path="$1"
container_name="$2"
service_name="$3"
workers="$4"
threads="$5"
shift 5

target_path="${TARGET_PATH:-}"
wait_s="${CONFIG_WAIT:-30}"
settle_s="${CONFIG_SETTLE:-5}"
port="${PORT:-}"
ports_config="${PORTS_CONFIG:-}"
run_dir=""

while [[ $# -gt 0 ]]; do
  if [[ $# -lt 2 ]]; then echo "❌ 选项缺少参数: $1" >&2; exit 1; fi
  case "$1" in
    --target-path) target_path="$2"; shift 2 ;;
    --target-path=*) target_path="${1#*=}"; shift ;;
    --wait) wait_s="$2"; shift 2 ;;
    --wait=*) wait_s="${1#*=}"; shift ;;
    --settle) settle_s="$2"; shift 2 ;;
    --settle=*) settle_s="${1#*=}"; shift ;;
    --port) port="$2"; shift 2 ;;
    --port=*) port="${1#*=}"; shift ;;
    --ports-config) ports_config="$2"; shift 2 ;;
    --ports-config=*) ports_config="${1#*=}"; shift ;;
    --run-dir) run_dir="$2"; shift 2 ;;
    --run-dir=*) run_dir="${1#*=}"; shift ;;
    *) echo "❌ 未知选项: $1" >&2; exit 1 ;;
  esac
done

if [[ ! -f "$config_path" ]]; then
  echo "❌ server_config.json 不存在: $config_path" >&2
  exit 1
fi
if [[ -n "$target_path" ]]; then
  if [[ "$target_path" != /* ]]; then
    echo "❌ 目标路径必须是容器内绝对路径: $target_path" >&2
    exit 1
  fi
  if [[ "$target_path" == *:* ]]; then
    echo "❌ 目标路径不能包含冒号（docker cp 语法冲突）: $target_path" >&2
    exit 1
  fi
fi
if ! [[ "$wait_s" =~ ^[0-9]+$ ]]; then
  echo "❌ --wait 必须是非负整数秒: $wait_s" >&2
  exit 1
fi
if ! [[ "$settle_s" =~ ^[0-9]+$ ]]; then
  echo "❌ --settle 必须是非负整数秒: $settle_s" >&2
  exit 1
fi
if [[ -n "$port" ]] && { [[ ! "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; }; then
  echo "❌ --port 必须是 1-65535 之间的整数: $port" >&2
  exit 1
fi

UPDATE_SCRIPT=""
for cand in "$BASE_DIR/update_server_config.sh" "$BASE_DIR/../update_server_config.sh"; do
  [ -f "$cand" ] && UPDATE_SCRIPT="$cand" && break
done
if [[ -z "$UPDATE_SCRIPT" ]]; then
  echo "❌ 找不到 update_server_config.sh" >&2
  exit 1
fi

PYTHON_BIN=$(command -v python3 || command -v python || true)
if [[ -z "$PYTHON_BIN" ]]; then
  echo "❌ 找不到 python3" >&2
  exit 1
fi

echo "⚙️ 改配置: workers=${workers} threads=${threads}（${config_path} → ${container_name} / ${service_name}）"
CONFIG_ARGS=("$config_path" "$container_name" "$service_name" "$workers" "$threads")
if [[ -n "$target_path" ]]; then CONFIG_ARGS+=(--target-path "$target_path"); fi
if ! bash "$UPDATE_SCRIPT" "${CONFIG_ARGS[@]}"; then
  echo "❌ 改服务配置失败" >&2
  exit 1
fi

ready_state="skipped"
ready_port=""
if [[ "$wait_s" -gt 0 ]]; then
  # 端口口径与 JMX 改写一致：--port > ports.json[服务名]
  ready_port=$("$PYTHON_BIN" - "$BASE_DIR" "$service_name" "${port:-}" \
    "${ports_config:-$BASE_DIR/ports.json}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from prepare_run import load_ports_config, resolve_port
except ImportError:
    sys.exit("无法导入 prepare_run.py，不能解析服务端口")
service, port, config = sys.argv[2], sys.argv[3] or None, sys.argv[4]
try:
    value, _source = resolve_port(None, int(port) if port else None,
                                  load_ports_config(config), service)
except ValueError as exc:
    sys.exit(str(exc))
if value is None:
    sys.exit("没有可用的服务端口")
print(value)
PY
  ) || { echo "❌ 解析服务端口失败，未改配置后的等待" >&2; exit 1; }

  echo "⏳ 等待服务就绪：${container_name} 的 127.0.0.1:${ready_port}，最多 ${wait_s}s ..."
  if "$PYTHON_BIN" - "$ready_port" "$wait_s" <<'PY'
import socket
import sys
import time

port, budget = int(sys.argv[1]), float(sys.argv[2])
start = time.monotonic()
deadline = start + budget
attempt = 0
while True:
    attempt += 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        print("… 端口 %d 未就绪（第 %d 次探测: %s）" % (port, attempt, exc), flush=True)
    else:
        sock.close()
        print("✅ 端口 %d 已就绪（第 %d 次探测，%.1fs）"
              % (port, attempt, time.monotonic() - start), flush=True)
        sys.exit(0)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sys.exit(1)
    time.sleep(min(2.0, remaining))
PY
  then
    ready_state="ok"
    if [[ "$settle_s" -gt 0 ]]; then
      echo "🧘 端口已通，再稳定 ${settle_s}s ..."
      sleep "$settle_s"
    fi
  else
    ready_state="timeout"
  fi
else
  echo "⚠️ --wait 0：重启后不做就绪探测，直接返回成功" >&2
fi

if [[ -n "$run_dir" ]]; then
  mkdir -p "$run_dir"
  cp -f "$config_path" "$run_dir/server_config.json"
  "$PYTHON_BIN" - "$run_dir/config_change.json" "$config_path" "$container_name" \
    "$service_name" "$workers" "$threads" "$target_path" "$ready_port" \
    "$wait_s" "$settle_s" "$ready_state" <<'PY'
import datetime
import json
import sys

(out, source, container, service, workers, threads, target,
 port, wait, settle, state) = sys.argv[1:12]
payload = {
    "applied_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "source_config": str(source),
    "container": container,
    "service": service,
    "workers": int(workers),
    "threads": int(threads),
    "target_path": target or "/hexapp/ai-{}-serving/conf/server_config.json".format(service),
    "readiness_port": int(port) if port else None,
    "readiness_wait_s": int(wait),
    "readiness_settle_s": int(settle),
    "readiness": state,
}
with open(out, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
PY
fi

if [[ "$ready_state" = "timeout" ]]; then
  echo "❌ 服务在 ${wait_s}s 内没有在 127.0.0.1:${ready_port} 就绪" >&2
  echo "   排查容器日志: docker logs --tail 100 ${container_name}" >&2
  echo "   起服务确实更慢就加大等待: --wait 180（端口本身对不对看 ports.json / --port）" >&2
  exit 1
fi

echo "✅ 配置已生效: ${container_name}（workers=${workers} threads=${threads}${ready_port:+, 端口 ${ready_port} 就绪}）"
