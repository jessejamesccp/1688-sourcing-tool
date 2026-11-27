#!/bin/bash

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ -x "$DIR/runtime/bin/python3" ]; then
  PY_BIN="$DIR/runtime/bin/python3"
elif [ -x "$DIR/.venv/bin/python" ]; then
  PY_BIN="$DIR/.venv/bin/python"
else
  PY_BIN="$(command -v python3 || true)"
fi

META_FILE="$DIR/data/server.json"
PID=""
URL=""

if [ -f "$META_FILE" ] && [ -n "$PY_BIN" ]; then
  INFO="$("$PY_BIN" -c '
import json, sys
from pathlib import Path
try:
    d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(f"{int(d.get(\"pid\") or 0)}|{d.get(\"url\") or \"\"}")
except Exception:
    pass
' "$META_FILE" 2>/dev/null || true)"
  PID="${INFO%%|*}"
  URL="${INFO#*|}"
fi

if [ -n "$URL" ]; then
  curl -fsS --max-time 2 -X POST "$URL/api/shutdown" -H "Content-Type: application/json" -d "{}" >/dev/null 2>&1 || true
fi

if [ -n "$PID" ] && [ "$PID" -gt 0 ] 2>/dev/null; then
  for _ in $(seq 1 15); do
    if ! kill -0 "$PID" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
    sleep 1
  fi
  if kill -0 "$PID" 2>/dev/null; then
    kill -KILL "$PID" 2>/dev/null || true
  fi
fi

pkill -TERM -f "$DIR/web_ui.py" 2>/dev/null || true
pkill -TERM -f "$DIR/step2_find_matches.py" 2>/dev/null || true
pkill -TERM -f "$DIR/step3_get_contacts.py" 2>/dev/null || true
pkill -TERM -f "$DIR/step4_wangwang_chat.py" 2>/dev/null || true

rm -f "$META_FILE"
echo "1688 选品工作台及关联进程已停止。"
