#!/usr/bin/env bash
# Ventoy-UI 起動スクリプト (systemd が使えない環境用フォールバック)
set -euo pipefail
cd /opt/ventoy-ui
exec python3 /opt/ventoy-ui/app.py
