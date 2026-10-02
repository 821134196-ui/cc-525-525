#!/usr/bin/env bash
# 一条本地启动命令：./run.sh
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m app.main
