#!/bin/bash
# ============================================================
# QDII-fund-scout 一键安装（无需 git）
# 复制以下命令到终端运行即可：
#   bash <(curl -fsSL https://raw.githubusercontent.com/hangliuc/QDII-fund-scout-skill/main/install.sh)
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

INSTALL_DIR="$HOME/QDII-fund-scout"
REPO_URL="https://github.com/hangliuc/QDII-fund-scout-skill.git"
ZIP_URL="https://github.com/hangliuc/QDII-fund-scout-skill/archive/refs/heads/main.zip"

echo ""
echo "============================================"
echo "   QDII-fund-scout 一键安装"
echo "   QDII 基金申购限额查询工具"
echo "============================================"
echo ""

# ── 第1步：检查 Python ──────────────────────────
step "第1步/4步：检查运行环境"

if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    error "未检测到 Python！请先安装 Python 3.9+"
    echo ""
    echo "  macOS 安装方式："
    echo "    方式1：App Store 搜索 Python 安装"
    echo "    方式2：brew install python3"
    echo "    方式3：https://www.python.org/downloads/"
    echo ""
    echo "  安装 Python 后重新运行本脚本即可"
    exit 1
fi

PYVER=$($PYTHON --version 2>&1)
info "检测到 $PYVER"

# ── 第2步：下载项目 ──────────────────────────
step "第2步/4步：下载项目文件"

_download_zip() {
    info "使用 ZIP 下载（无需 git）..."
    ZIP_FILE="/tmp/qdii-fund-scout.zip"
    if command -v curl &>/dev/null; then
        curl -fsSL "$ZIP_URL" -o "$ZIP_FILE"
    elif command -v wget &>/dev/null; then
        wget -q "$ZIP_URL" -O "$ZIP_FILE"
    else
        error "需要 curl 或 wget 来下载文件"
        echo "  请安装 curl：brew install curl"
        exit 1
    fi
    info "下载完成，正在解压..."
    cd /tmp && unzip -qo "$ZIP_FILE" && rm -f "$ZIP_FILE"
    mv "/tmp/QDII-fund-scout-skill-main" "$INSTALL_DIR" 2>/dev/null || true
    info "解压完成"
}

if [ -d "$INSTALL_DIR" ]; then
    warn "检测到已有安装目录：$INSTALL_DIR"
    read -p "  是否重新下载覆盖？(y/n，默认 n): " OVERWRITE
    if [[ "$OVERWRITE" == "y" || "$OVERWRITE" == "Y" ]]; then
        rm -rf "$INSTALL_DIR"
    else
        info "保留现有安装，跳过下载"
    fi
fi

if [ ! -d "$INSTALL_DIR" ]; then
    if command -v git &>/dev/null; then
        info "使用 git 下载..."
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null && info "下载完成" || {
            warn "git clone 失败，尝试 ZIP 下载..."
            _download_zip
        }
    else
        _download_zip
    fi
fi

# ── 第3步：安装依赖 ──────────────────────────
step "第3步/4步：安装依赖包"

info "正在安装 requests（网络请求）和 pdfplumber（PDF解析）..."
$PYTHON -m pip install requests pdfplumber yfinance pandas numpy -q 2>&1 | tail -1
info "依赖安装完成"

# ── 第4步：验证安装 ──────────────────────────
step "第4步/4步：验证安装"

cd "$INSTALL_DIR/scripts"
$PYTHON -c "
import sys
sys.path.insert(0, '.')
from core.sources.eastmoney import EastMoneySource
from core.fetcher import FundFetcher
print('OK')
" 2>&1 | grep -v urllib3 | grep -q "OK" && info "安装验证通过！" || warn "验证未完全通过，但通常不影响使用"

# ── 创建快捷命令 ──────────────────────────
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "fund-scout" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# QDII-fund-scout 快捷命令" >> "$SHELL_RC"
        echo "alias fund-scout='bash $INSTALL_DIR/run.sh'" >> "$SHELL_RC"
        info "已添加快捷命令 fund-scout 到 $SHELL_RC"
        info "新开终端后，输入 fund-scout 即可运行"
    fi
fi

# ── 完成 ──────────────────────────
step "安装完成 🎉"

echo ""
echo "  启动方式："
echo ""
echo "  方式1：快捷命令（新开终端后生效）"
echo "      fund-scout"
echo ""
echo "  方式2：直接运行"
echo "      bash $INSTALL_DIR/run.sh"
echo ""
echo "  方式3：可视化界面"
echo "      bash $INSTALL_DIR/run.sh  → 选 1) 打开可视化界面"
echo ""
echo "  更新方式："
if command -v git &>/dev/null; then
echo "      cd $INSTALL_DIR && git pull"
else
echo "      重新运行本安装脚本即可覆盖更新"
fi
echo ""
