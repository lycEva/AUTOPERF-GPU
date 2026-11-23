#!/usr/bin/env bash
# 一键修改服务 server_config.json 的 workers/threads，同步进容器后重启容器。
#
# 用法:
#   ./update_server_config.sh <server_config.json_path> <container_name> <service_name> <workers> <threads> \
#     [--target-path /容器内/绝对路径/server_config.json]
#
# 默认容器内目标路径：/hexapp/ai-<service_name>-serving/conf/server_config.json
# 服务不在这个约定路径时，用 --target-path（或环境变量 TARGET_PATH）指定；
# 目标目录不存在会自动创建。
#
# 示例:
#   ./update_server_config.sh ./server_config.json bsocr-container bsocr 2 4
#   ./update_server_config.sh ./server_config.json bsocr-container bsocr 2 4 \
#     --target-path /opt/app/conf/server_config.json

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./update_server_config.sh <server_config.json_path> <container_name> <service_name> <workers> <threads> \
[--target-path CONTAINER_PATH]

Example:
  ./update_server_config.sh ./server_config.json bsocr-container bsocr 2 4
  ./update_server_config.sh ./server_config.json bsocr-container bsocr 2 4 \
    --target-path /opt/app/conf/server_config.json

This script will:
  1. Update "workers" and "threads" in the local server_config.json
  2. Copy the file into the container at:
     --target-path 给出的路径，或默认 /hexapp/ai-<service_name>-serving/conf/server_config.json
     （目标目录不存在会自动 mkdir -p）
  3. Restart the target Docker container

Environment:
  TARGET_PATH   same as --target-path (the flag wins)
EOF
}

if [[ $# -lt 5 ]]; then
  usage
  exit 1
fi

config_path="$1"
container_name="$2"
service_name="$3"
workers="$4"
threads="$5"
shift 5

target_path="${TARGET_PATH:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-path)
      if [[ $# -lt 2 ]]; then echo "❌ --target-path 缺少参数" >&2; exit 1; fi
      target_path="$2"; shift 2 ;;
    --target-path=*)
      target_path="${1#*=}"; shift ;;
    *)
      echo "❌ 未知选项: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$target_path" ]]; then
  target_path="/hexapp/ai-${service_name}-serving/conf/server_config.json"
fi
if [[ "$target_path" != /* ]]; then
  echo "❌ 目标路径必须是容器内绝对路径: $target_path" >&2
  exit 1
fi
if [[ "$target_path" == *:* ]]; then
  echo "❌ 目标路径不能包含冒号（docker cp 语法冲突）: $target_path" >&2
  exit 1
fi

echo "参数验证通过: config_path=$config_path, container_name=$container_name, service_name=$service_name, workers=$workers, threads=$threads"
echo "容器内目标路径: $target_path"

if [[ ! -f "$config_path" ]]; then
  echo "Config file not found: $config_path" >&2
  exit 1
fi

echo "配置文件存在: $config_path"

if ! [[ "$workers" =~ ^[0-9]+$ ]]; then
  echo "workers must be a non-negative integer: $workers" >&2
  exit 1
fi

if ! [[ "$threads" =~ ^[0-9]+$ ]]; then
  echo "threads must be a non-negative integer: $threads" >&2
  exit 1
fi

echo "workers 和 threads 验证通过: workers=$workers, threads=$threads"

PY_BIN=$(command -v python3 || command -v python || true)
if [[ -z "$PY_BIN" ]]; then
  echo "python3/python not found; cannot edit JSON" >&2
  exit 1
fi

echo "开始更新 JSON 文件"
"$PY_BIN" - "$config_path" "$workers" "$threads" <<'PY'
import json
import sys
from pathlib import Path

config_file = Path(sys.argv[1])
workers = int(sys.argv[2])
threads = int(sys.argv[3])

with config_file.open("r", encoding="utf-8") as f:
    data = json.load(f)

data["workers"] = workers
data["threads"] = threads

with config_file.open("w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=4)
    f.write("\n")
PY

echo "Updated local config: $config_path"

target_dir=$(dirname "$target_path")
if ! docker exec "$container_name" mkdir -p "$target_dir" 2>/dev/null; then
  echo "⚠️ 目标目录创建失败或镜像无 mkdir，直接尝试 docker cp: $target_dir" >&2
fi

echo "开始复制文件到容器"
docker cp "$config_path" "${container_name}:${target_path}"
echo "Copied config to: ${container_name}:${target_path}"
echo "开始重启容器"
docker restart "$container_name"
echo "Restarted container: $container_name"
