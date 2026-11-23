#!/usr/bin/env bash
# ============================================================================
# install.sh —— 把 gputest 装到 PATH 上，之后任意目录直接 `gputest ...`
#
# 用法:
#   bash install.sh                  # 自动选目录（优先 ~/.local/bin）
#   bash install.sh --prefix DIR     # 指定目录
#   bash install.sh --copy           # 复制整个工具目录（原目录可以删/移动）
#   bash install.sh --update-path    # 目录不在 PATH 上时写进 ~/.bashrc
#   bash install.sh --uninstall      # 卸载
#
# 默认是**软链**：工具目录更新后，命令立刻是新的，不需要重装。
# 同时装 gputest 与 gpu-test 两个名字（后者只是别名）。
# ============================================================================
set -euo pipefail

BASE_DIR=$(cd "$(dirname "$0")" && pwd)
NAMES=(gputest gpu-test)

usage() {
  cat <<'EOF'
用法:
  bash install.sh [--prefix DIR] [--copy] [--update-path] [--uninstall] [--name NAME]

  --prefix DIR     安装目录，默认自动选：~/.local/bin（若不可写则 ~/bin）
  --copy           复制工具目录到 DIR/gputest.d，再软链过去（不依赖原目录）
  --update-path    若安装目录不在 PATH 上，追加到 ~/.bashrc（幂等）
  --uninstall      移除已安装的命令（不删源目录）
  --name NAME      额外/仅安装这个名字（默认 gputest 与 gpu-test）
EOF
}

prefix=""
do_copy=0
do_uninstall=0
update_path=0
names=("${NAMES[@]}")

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)      usage; exit 0 ;;
    --prefix)       [ "$#" -ge 2 ] || { echo "❌ --prefix 缺少参数" >&2; exit 1; }
                    prefix=$2; shift 2 ;;
    --prefix=*)     prefix=${1#*=}; shift ;;
    --copy)         do_copy=1; shift ;;
    --uninstall)    do_uninstall=1; shift ;;
    --update-path)  update_path=1; shift ;;
    --name)         [ "$#" -ge 2 ] || { echo "❌ --name 缺少参数" >&2; exit 1; }
                    names=("$2"); shift 2 ;;
    *) echo "❌ 未知选项: $1" >&2; usage >&2; exit 1 ;;
  esac
done

on_path() {
  case ":${PATH:-}:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

pick_prefix() {
  if [ -n "$prefix" ]; then
    printf '%s\n' "$prefix"
    return 0
  fi
  local home=${HOME:-/tmp}
  if [ -d "$home/.local/bin" ] && [ -w "$home/.local/bin" ]; then
    printf '%s\n' "$home/.local/bin"
  elif mkdir -p "$home/.local/bin" 2>/dev/null && [ -w "$home/.local/bin" ]; then
    printf '%s\n' "$home/.local/bin"
  elif mkdir -p "$home/bin" 2>/dev/null && [ -w "$home/bin" ]; then
    printf '%s\n' "$home/bin"
  else
    echo "❌ 找不到可写目录；用 --prefix DIR 指定" >&2
    exit 1
  fi
}

install_one() { # install_one <命令名> <目标脚本>
  local name=$1 target=$2
  local dest="$INSTALL_DIR/$name"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    if ! head -n 3 "$dest" 2>/dev/null | grep -q 'gputest'; then
      echo "❌ $dest 已存在且不是 gputest 安装的文件，拒绝覆盖" >&2
      exit 1
    fi
  fi
  ln -sfn "$target" "$dest"
  echo "🔗 $dest -> $target"
}

INSTALL_DIR=$(pick_prefix)
if ! mkdir -p "$INSTALL_DIR" 2>/dev/null || [ ! -w "$INSTALL_DIR" ]; then
  echo "❌ 安装目录不可写: $INSTALL_DIR" >&2
  exit 1
fi
INSTALL_DIR=$(cd -P "$INSTALL_DIR" && pwd)

# ---------------------------------------------------------------- 卸载 ------
if [ "$do_uninstall" = 1 ]; then
  removed=0
  for name in "${names[@]}"; do
    dest="$INSTALL_DIR/$name"
    if [ -L "$dest" ] || [ -f "$dest" ]; then
      rm -f "$dest" && echo "🗑 已移除 $dest" && removed=1
    fi
  done
  copy_dir="$INSTALL_DIR/gputest.d"
  if [ -d "$copy_dir" ]; then
    rm -rf "$copy_dir" && echo "🗑 已移除 $copy_dir"
    removed=1
  fi
  [ "$removed" = 1 ] || echo "ℹ️ $INSTALL_DIR 下没有找到已安装的 gputest"
  exit 0
fi

# ---------------------------------------------------------------- 源目录 ----
if [ ! -x "$BASE_DIR/bin/gputest" ]; then
  chmod +x "$BASE_DIR/bin/gputest" 2>/dev/null || true
fi
if [ ! -f "$BASE_DIR/bin/gputest" ]; then
  echo "❌ 找不到 $BASE_DIR/bin/gputest" >&2
  exit 1
fi
chmod +x "$BASE_DIR/bin/gputest" 2>/dev/null || true

# ---------------------------------------------------------------- 复制模式 ---
target="$BASE_DIR/bin/gputest"
if [ "$do_copy" = 1 ]; then
  copy_dir="$INSTALL_DIR/gputest.d"
  echo "📦 复制工具目录到 $copy_dir ..."
  rm -rf "${copy_dir:?}"
  mkdir -p "$copy_dir"
  # 工具本体（无 .git / 无 __pycache__ / 无历史备份）
  local_tmp=$(mktemp -d)
  trap 'rm -rf "$local_tmp"' EXIT
  (cd "$BASE_DIR" && tar -cf - \
      --exclude='__pycache__' --exclude='*.bak.*' --exclude='.git' \
      bin run_test.sh prepare_run.py update_jmx_pid.py update_server_config.sh \
      apply_server_config.sh ports.json summary_monitor tests README.md install.sh \
      2>/dev/null || true) | (cd "$copy_dir" && tar -xf -)
  chmod +x "$copy_dir/bin/gputest" "$copy_dir"/*.sh 2>/dev/null || true
  target="$copy_dir/bin/gputest"
fi

# ---------------------------------------------------------------- 安装 ------
for name in "${names[@]}"; do
  install_one "$name" "$target"
done

# ---------------------------------------------------------------- 自检 ------
echo ""
found=$(command -v "${names[0]}" 2>/dev/null || true)
if [ -n "$found" ] && [ "$found" != "$INSTALL_DIR/${names[0]}" ] && on_path "$INSTALL_DIR"; then
  echo "⚠️ PATH 上更靠前的同名命令会遮住本次安装: $found"
fi

if on_path "$INSTALL_DIR"; then
  echo "✅ $INSTALL_DIR 已在 PATH 上：现在任意目录都能直接敲 ${names[0]}"
else
  echo "⚠️ $INSTALL_DIR 不在当前 PATH 上"

  login_sees=0
  login_found=""
  if command -v bash >/dev/null 2>&1; then
    # 必须是「这个目录里的那份」才算数，别被别的 PATH 目录里的同名命令骗了
    login_found=$(bash -lc "command -v ${names[0]}" 2>/dev/null | head -1 || true)
    if [ "$login_found" = "$INSTALL_DIR/${names[0]}" ]; then
      login_sees=1
    fi
  fi

  rc_file=""
  case "${SHELL:-/bin/bash}" in
    */zsh) rc_file="$HOME/.zshrc" ;;
    *)     rc_file="$HOME/.bashrc" ;;
  esac
  line="export PATH=\"$INSTALL_DIR:\$PATH\""

  if [ "$login_sees" = 1 ]; then
    echo "   登录 shell 里已经能看到它（shell 配置里已有 $INSTALL_DIR），新开终端即可。"
  elif [ "$update_path" = 1 ]; then
    if [ -f "$rc_file" ] && grep -qF "$INSTALL_DIR" "$rc_file"; then
      echo "   $rc_file 里已写过 $INSTALL_DIR，无需重复添加"
    else
      printf '\n# gputest CLI\n%s\n' "$line" >> "$rc_file"
      echo "✍️ 已写入 $rc_file: $line"
      echo "   新开终端生效，或立即执行: source $rc_file"
    fi
  else
    echo "   任选一种让它生效："
    echo "     source $rc_file                       # 若你已写过 PATH"
    echo "     bash $(printf '%q' "$BASE_DIR/install.sh") --update-path   # 帮你写进 $rc_file"
    echo "     $line                                 # 手动加到 shell 配置"
  fi
fi

echo ""
echo "试着运行:"
echo "  $INSTALL_DIR/${names[0]} doctor"
echo "  $INSTALL_DIR/${names[0]} services"
echo "  $INSTALL_DIR/${names[0]} run <jmx> <结果目录> <容器名> <线程数> <秒数> --service <服务名>"
