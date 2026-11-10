#!/usr/bin/env bash
# 本地重放所有案例：仅使用解释器进程，无网络、无子进程、无文件读写副作用。
set -u
cd "$(dirname "$0")"
for f in demos/0*.py safe_integration/test_safe_loader.py safe_integration/platform_integration.py; do
  echo "================================================================"
  echo "### $f"
  echo "================================================================"
  python3 "$f" || echo "[[ $f 退出码非零 ]]"
  echo
done
