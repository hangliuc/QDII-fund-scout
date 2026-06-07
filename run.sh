#!/bin/bash
# ============================================================
# QDII-fund-scout 启动器
# 打开浏览器可视化界面（http://localhost:8765）
# 所有交互操作都在浏览器里完成。
#
# 自动化 / 命令行用法见各脚本的 --help：
#   python3 scripts/cli.py --help              # 查询、对比、推送
#   python3 scripts/predict_cli.py --help      # T-1 估值预测
#   python3 scripts/holdings_refresh.py --help # 刷新季报缓存
#   python3 scripts/schedule_setup.py --help   # 定时任务管理
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 检查 Python ──────────────────────────
if command -v python3 &>/dev/null; then
    PYTHON_BIN=python3
elif command -v python &>/dev/null; then
    PYTHON_BIN=python
else
    error "未检测到 Python，请先安装 Python 3.6+"
    exit 1
fi

# ── 检查依赖 ──────────────────────────
if ! $PYTHON_BIN -c "import requests" 2>/dev/null; then
    warn "缺少依赖，正在安装..."
    REQ_FILE="$SCRIPT_DIR/requirements.txt"
    if [ -f "$REQ_FILE" ]; then
        $PYTHON_BIN -m pip install -r "$REQ_FILE" -q
    else
        $PYTHON_BIN -m pip install requests pdfplumber -q
    fi
    info "依赖安装完成"
fi

# ── 后台预热（BulkSnapshot 全市场快照 + CSRC 季报索引）──
# 用户打开页面 / 第一次查询时数据通常已就绪。
(cd "$SCRIPT_DIR/scripts" && \
    $PYTHON_BIN -c "
import threading
from core.fetcher import FundFetcher
from holdings_refresh import refresh_stale_in_background
threading.Thread(target=FundFetcher.warm_up, daemon=True).start()
refresh_stale_in_background()
" \
    >/dev/null 2>&1) &

# ── 启动浏览器界面 ──────────────────────────
echo -e "\n${BLUE}━━━ 启动 QDII-fund-scout ━━━${NC}\n"

cd "$SCRIPT_DIR/ui"

# 关闭已占用的 8765 端口
lsof -ti:8765 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 0.3

$PYTHON_BIN server.py &
SERVER_PID=$!
sleep 1

# 自动打开浏览器
if command -v open &>/dev/null; then
    open "http://localhost:8765" 2>/dev/null || true
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:8765" 2>/dev/null || true
fi

cat <<EOF
  界面已打开 → http://localhost:8765

  在界面中可以：
    · 添加 / 删除基金（含常用 QDII 下拉）
    · 配置飞书 / 企业微信 Webhook 并测试
    · 查询基金数据，支持同时推送到多个渠道
    · 设置 / 取消每日定时推送
    · 季报数据缓存诊断（折叠面板，平时无需操作）

  自动化 / SSH / cron 请用 python 脚本：
    python3 scripts/cli.py --help
    python3 scripts/holdings_refresh.py --help
    python3 scripts/schedule_setup.py --help

  按 Ctrl+C 关闭服务并退出
EOF

trap "kill $SERVER_PID 2>/dev/null; exit 0" INT TERM
wait $SERVER_PID
