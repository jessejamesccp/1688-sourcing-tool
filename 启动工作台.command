#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p "$DIR/data"

if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "=================================================="
  echo "[提示] 未检测到 Google Chrome 浏览器！"
  echo "请先安装 Google Chrome 后再使用找同款/抓电话/旺旺功能："
  echo "https://www.google.com/chrome/"
  echo "=================================================="
fi

PY_BIN=""
if [ -x "$DIR/runtime/bin/python3" ]; then
  PY_BIN="$DIR/runtime/bin/python3"
elif [ -x "$DIR/.venv/bin/python" ]; then
  PY_BIN="$DIR/.venv/bin/python"
else
  SYS_PY="$(command -v python3 || true)"
  if [ -z "$SYS_PY" ]; then
    echo "[错误] 未检测到 Python 3，请先安装 Python 3.10+ 后重试。"
    read -r -p "按回车键关闭窗口..."
    exit 1
  fi
  echo "[首次初始化] 正在创建 .venv 虚拟环境并安装依赖（请稍候）..."
  "$SYS_PY" -m venv "$DIR/.venv"
  PY_BIN="$DIR/.venv/bin/python"
  "$PY_BIN" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$DIR/requirements.txt" || "$PY_BIN" -m pip install -r "$DIR/requirements.txt"
fi

META_FILE="$DIR/data/server.json"
if [ -f "$META_FILE" ]; then
  OLD_INFO="$("$PY_BIN" -c '
import json, os, sys
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    pid = int(d.get("pid") or 0)
    url = str(d.get("url") or "")
    if pid > 0:
        os.kill(pid, 0)
        print(f"{pid}|{url}")
except Exception:
    pass
' "$META_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_INFO" ]; then
    OLD_URL="${OLD_INFO#*|}"
    echo "工作台已在运行: $OLD_URL"
    open "$OLD_URL"
    exit 0
  fi
fi

rm -f "$META_FILE"
nohup "$PY_BIN" -u "$DIR/web_ui.py" --no-browser >> "$DIR/data/server.log" 2>&1 &
BG_PID=$!

READY_URL=""
for _ in $(seq 1 30); do
  if ! kill -0 "$BG_PID" 2>/dev/null; then
    break
  fi
  if [ -f "$META_FILE" ]; then
    READY_URL="$("$PY_BIN" -c '
import json, sys
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(d.get("url") or "")
except Exception:
    pass
' "$META_FILE" 2>/dev/null || true)"
    if [ -n "$READY_URL" ]; then
      break
    fi
  fi
  sleep 0.3
done

if [ -z "$READY_URL" ]; then
  echo "[错误] 工作台启动失败，请查看日志: $DIR/data/server.log"
  tail -n 20 "$DIR/data/server.log" 2>/dev/null || true
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

echo "=================================================="
echo "1688 选品工作台已启动: $READY_URL"
echo "如需停止后台服务，请双击「停止工作台.command」或点击页面右上角「停止服务」。"
echo "=================================================="
open "$READY_URL"
