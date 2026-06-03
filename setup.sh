#!/bin/bash
# ============================================================
# QDII-fund-scout 一键安装脚本
# 面向零基础用户，全程中文指引
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
step()  { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.fund-scout"
CONFIG_FILE="$CONFIG_DIR/config.json"

echo ""
echo "============================================"
echo "   QDII-fund-scout 一键安装"
echo "   QDII 基金申购限额查询工具"
echo "============================================"
echo ""

# ── 第1步：检查 Python ──────────────────────────
step "第1步：检查运行环境"

if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    error "未检测到 Python！请先安装 Python 3.9+"
    error "下载地址：https://www.python.org/downloads/"
    error "安装后重新运行本脚本即可"
    exit 1
fi

PYVER=$($PYTHON --version 2>&1)
info "检测到 $PYVER"

# ── 第2步：安装依赖 ──────────────────────────
step "第2步：安装依赖包"

info "正在安装 requests（网络请求）和 pdfplumber（PDF解析）..."
$PYTHON -m pip install requests pdfplumber yfinance pandas numpy -q 2>&1 | tail -1
info "依赖安装完成"

# ── 第3步：配置推送渠道 ──────────────────────────
step "第3步：配置推送渠道（可选，可跳过）"

echo "  QDII-fund-scout 支持将基金数据推送到"
echo "  ① 飞书机器人 ② 企业微信机器人"
echo ""
echo "  ⚠ 如果没有推送需求，直接按回车跳过即可"
echo "  ⚠ 后续也可以随时修改配置文件 ~/.fund-scout/config.json"
echo ""

read -p "  是否配置推送渠道？(y/n，默认 n): " SETUP_PUSH
echo ""

FEISHU_URL=""
WECHAT_URL=""

if [[ "$SETUP_PUSH" == "y" || "$SETUP_PUSH" == "Y" ]]; then

    # ── 飞书 ──
    echo "  ── 飞书机器人配置 ──"
    echo "  获取方式：飞书 → 群设置 → 群机器人 → 添加 Webhook 机器人"
    echo "  复制 Webhook 地址（以 https://open.feishu.cn/open-apis/bot/v2/hook/ 开头）"
    read -p "  请输入飞书 Webhook 地址（直接回车跳过）: " FEISHU_URL
    echo ""

    # ── 企业微信 ──
    echo "  ── 企业微信机器人配置 ──"
    echo "  获取方式：企业微信 → 群设置 → 群机器人 → 添加机器人"
    echo "  复制 Webhook 地址（以 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key= 开头）"
    read -p "  请输入企业微信 Webhook 地址（直接回车跳过）: " WECHAT_URL
    echo ""
fi

# ── 创建配置文件 ──────────────────────────
mkdir -p "$CONFIG_DIR"

cat > "$CONFIG_FILE" <<CONFEOF
{
  "my_funds": [
    {"code": "012870", "name": "易方达纳指100C", "main_code": "012869"},
    {"code": "006479", "name": "广发纳指100C", "main_code": "006479"},
    {"code": "008971", "name": "大成纳指100C", "main_code": "008970"}
  ],
  "push": {
    "feishu_webhook": "${FEISHU_URL}",
    "wechat_webhook": "${WECHAT_URL}"
  },
  "defaults": {
    "format": "md",
    "style": "card",
    "profile": "compare"
  }
}
CONFEOF

info "配置文件已生成 → $CONFIG_FILE"

# ── 第4步：创建启动快捷方式 ──────────────────────────
step "第4步：验证安装"

cd "$SCRIPT_DIR/scripts"
$PYTHON -c "
import sys
sys.path.insert(0, '.')
from core.sources.eastmoney import EastMoneySource
from core.fetcher import FundFetcher
print('EastMoneySource: OK')
print('FundFetcher: OK')
" 2>&1 | grep -v urllib3

info "安装验证通过！"

# ── 完成 ──────────────────────────
step "安装完成 🎉"

echo ""
echo "  下一步："
echo ""
echo "  📋 查看持仓基金申购限额和收益率："
echo "      cd $SCRIPT_DIR && bash run.sh"
echo ""
echo "  📋 或直接命令行运行："
echo "      cd $SCRIPT_DIR/scripts"
echo "      python3 cli.py compare 012870,006479,008971"
echo ""
echo "  📋 推送到飞书/企业微信："
echo "      cd $SCRIPT_DIR/scripts"
echo "      python3 cli.py compare 012870,006479 --push feishu"
echo ""
echo "  📝 修改我的基金列表："
echo "      vim $CONFIG_FILE"
echo ""
