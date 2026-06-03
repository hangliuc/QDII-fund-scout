#!/bin/bash
# ============================================================
# QDII-fund-scout 一键运行脚本
# 交互式菜单，无需记忆任何命令
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }
title() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$HOME/.fund-scout/config.json"

# ── 检查依赖 ──────────────────────────
check_deps() {
    if ! python3 -c "import requests" 2>/dev/null; then
        warn "缺少依赖，正在安装..."
        python3 -m pip install requests pdfplumber -q
        info "依赖安装完成"
    fi
}

# ── 读取配置中的基金列表 ──────────────────────────
read_my_funds() {
    if [ -f "$CONFIG_FILE" ]; then
        python3 -c "
import json
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
funds = cfg.get('my_funds', [])
for f in funds:
    print(f.get('code','') + '|' + f.get('name','') + '|' + f.get('main_code',''))
" 2>/dev/null || true
    fi
}

# ── 读取配置中的推送渠道 ──────────────────────────
read_push_urls() {
    if [ -f "$CONFIG_FILE" ]; then
        python3 -c "
import json
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
push = cfg.get('push', {})
print(push.get('feishu_webhook', ''))
print(push.get('wechat_webhook', ''))
" 2>/dev/null || echo -e "\n\n"
    fi
}

# ── 主菜单 ──────────────────────────
show_menu() {
    clear
    echo ""
    echo "============================================"
    echo "   QDII-fund-scout"
    echo "   QDII 基金申购限额查询工具"
    echo "============================================"
    echo ""
    echo "  当前日期：$(date '+%Y-%m-%d')"
    echo ""

    # 显示预设基金
    FUNDS_JSON=$(read_my_funds)
    FUND_COUNT=$(echo "$FUNDS_JSON" | grep -c '|' 2>/dev/null || echo "0")
    if [ "$FUND_COUNT" -gt 0 ]; then
        echo "  📋 已配置基金：$FUND_COUNT 只"
        echo "$FUNDS_JSON" | while IFS='|' read -r code name main; do
            [ -n "$code" ] && echo "     $code  $name"
        done
    else
        echo "  📋 暂未配置基金（稍后可手动输入）"
    fi
    echo ""
    echo "  请选择操作："
    echo ""
    echo "    1) 打开可视化界面（浏览器配置）"
    echo "    2) 查看我的基金（使用配置文件）"
    echo "    3) 编辑我的基金列表"
    echo "    4) 手动输入基金代码查询"
    echo "    5) 查询我的基金并推送到飞书"
    echo "    6) 查询我的基金并推送到企业微信"
    echo "    7) 查询我的基金并同时推送飞书 + 企业微信"
    echo "    8) 配置推送渠道（飞书/企业微信）"
    echo "    9) 设置每日定时推送（自动运行）"
    echo "    10) 取消定时推送"
    echo "    11) 检查并刷新季报持仓数据（用于 T-1 估值预测）"
    echo "    0) 退出"
    echo ""
    read -p "  请输入数字 (0-11): " CHOICE
    echo ""
}

# ── 运行对比查询 ──────────────────────────
run_compare() {
    local codes="$1"
    local push_target="$2"

    cd "$SCRIPT_DIR/scripts"
    CMD="python3 cli.py compare --format md --style table"

    if [ -n "$codes" ]; then
        CMD="$CMD $codes"
    else
        CMD="$CMD --config $CONFIG_FILE"
    fi

    if [ -n "$push_target" ]; then
        CMD="$CMD --push $push_target"
    fi

    echo ""
    info "正在查询..."
    echo ""
    eval "$CMD"
    local EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        info "查询完成！"
    else
        warn "查询过程中出现部分错误，请检查网络后重试"
    fi

    echo ""
    read -p "按回车键返回主菜单..."
}

# ── 配置基金列表 ──────────────────────────
edit_funds() {
    clear
    title "编辑我的基金列表"

    echo "  每行输入一只基金，格式：基金代码 基金名称"
    echo "  ⚠ 基金代码为 6 位数字，必须正确（名称可以自定义，仅做展示）"
    echo "  示例：012870 易方达纳指100C"
    echo "  输入完毕后输入空行结束"
    echo ""
    echo "  提示：标准基金列表可参考 README.md 或打开可视化界面查看"
    echo ""

    FUNDS_LIST=""
    while true; do
        read -p "  " LINE
        if [ -z "$LINE" ]; then
            break
        fi
        CODE=$(echo "$LINE" | awk '{print $1}')
        NAME=$(echo "$LINE" | cut -d' ' -f2-)
        if [ -n "$CODE" ] && [ -n "$NAME" ]; then
            # 校验：基金代码必须是 6 位数字
            if ! echo "$CODE" | grep -qE '^[0-9]{6}$'; then
                warn "基金代码 '$CODE' 不是有效的 6 位数字，请重新输入"
                continue
            fi
            FUNDS_LIST="${FUNDS_LIST}{\"code\": \"$CODE\", \"name\": \"$NAME\"},"
        else
            warn "格式错误，请重新输入"
        fi
    done

    if [ -n "$FUNDS_LIST" ]; then
        FUNDS_LIST="${FUNDS_LIST%,}"
        mkdir -p "$HOME/.fund-scout"

        # 合并推送配置
        FEISHU=""
        WECHAT=""
        if [ -f "$CONFIG_FILE" ]; then
            FEISHU=$(python3 -c "import json; f=open('$CONFIG_FILE'); d=json.load(f); print(d.get('push',{}).get('feishu_webhook','')); f.close()" 2>/dev/null || true)
            WECHAT=$(python3 -c "import json; f=open('$CONFIG_FILE'); d=json.load(f); print(d.get('push',{}).get('wechat_webhook','')); f.close()" 2>/dev/null || true)
        fi

        cat > "$CONFIG_FILE" <<CONFEOF
{
  "my_funds": [$FUNDS_LIST],
  "push": {
    "feishu_webhook": "${FEISHU}",
    "wechat_webhook": "${WECHAT}"
  },
  "defaults": {
    "format": "md",
    "style": "table",
    "profile": "compare"
  }
}
CONFEOF
        info "已保存 $CONFIG_FILE"
    else
        warn "未输入任何基金"
    fi

    echo ""
    read -p "按回车键返回主菜单..."
}

# ── 配置推送渠道 ──────────────────────────
edit_push() {
    clear
    title "配置推送渠道"

    echo "  ⚠ 推送地址包含你的机器人密钥，不要泄露给他人"
    echo ""

    FEISHU_URL=""
    WECHAT_URL=""

    # 读取已有的
    if [ -f "$CONFIG_FILE" ]; then
        FEISHU_URL=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    d = json.load(f)
print(d.get('push', {}).get('feishu_webhook', ''))
" 2>/dev/null || true)
        WECHAT_URL=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    d = json.load(f)
print(d.get('push', {}).get('wechat_webhook', ''))
" 2>/dev/null || true)
    fi

    echo "  ── 飞书机器人 ──"
    echo "  获取方式：飞书群 → 设置 → 群机器人 → 添加 Webhook 机器人"
    if [ -n "$FEISHU_URL" ]; then
        echo "  当前已配置：${FEISHU_URL:0:40}..."
    fi
    read -p "  请输入飞书 Webhook 地址（直接回车跳过）: " NEW_FEISHU
    [ -n "$NEW_FEISHU" ] && FEISHU_URL="$NEW_FEISHU"
    echo ""

    echo "  ── 企业微信机器人 ──"
    echo "  获取方式：企业微信群 → 群机器人 → 添加机器人 → 复制 Webhook 地址"
    if [ -n "$WECHAT_URL" ]; then
        echo "  当前已配置：${WECHAT_URL:0:40}..."
    fi
    read -p "  请输入企业微信 Webhook 地址（直接回车跳过）: " NEW_WECHAT
    [ -n "$NEW_WECHAT" ] && WECHAT_URL="$NEW_WECHAT"
    echo ""

    # 保存
    mkdir -p "$HOME/.fund-scout"
    MY_FUNDS="[]"
    if [ -f "$CONFIG_FILE" ]; then
        MY_FUNDS=$(python3 -c "
import json
with open('$CONFIG_FILE') as f:
    d = json.load(f)
print(json.dumps(d.get('my_funds', []), ensure_ascii=False))
" 2>/dev/null || echo '[]')
    fi

    cat > "$CONFIG_FILE" <<CONFEOF
{
  "my_funds": $MY_FUNDS,
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

    if [ -n "$FEISHU_URL" ]; then
        # 发送测试消息
        cd "$SCRIPT_DIR/scripts"
        python3 -c "
import sys; sys.path.insert(0, '.')
from adapters.feishu import FeishuAdapter
from core.models import FundDataResult
a = FeishuAdapter(webhook_url='$FEISHU_URL')
ok = a.test_connection()
print('飞书测试:', '成功' if ok else '失败')
" 2>/dev/null || true
    fi
    if [ -n "$WECHAT_URL" ]; then
        cd "$SCRIPT_DIR/scripts"
        python3 -c "
import sys; sys.path.insert(0, '.')
from adapters.wechat import WechatAdapter
a = WechatAdapter(webhook_url='$WECHAT_URL')
ok = a.test_connection()
print('企业微信测试:', '成功' if ok else '失败')
" 2>/dev/null || true
    fi
    echo ""

    info "推送配置已保存"
    read -p "按回车键返回主菜单..."
}

# ── 启动 Web UI ──────────────────────────
start_ui() {
    clear
    title "启动可视化配置界面"

    cd "$SCRIPT_DIR/ui"

    echo "  正在启动本地 Web 服务..."
    echo ""

    # 先关闭已有的 8765 端口进程
    lsof -ti:8765 2>/dev/null | xargs kill -9 2>/dev/null || true
    sleep 0.5

    python3 server.py &
    local PID=$!
    sleep 1

    # 自动打开浏览器
    if command -v open &>/dev/null; then
        open "http://localhost:8765" 2>/dev/null || true
    elif command -v xdg-open &>/dev/null; then
        xdg-open "http://localhost:8765" 2>/dev/null || true
    fi

    echo "  在浏览器中打开 http://localhost:8765"
    echo "  在可视化界面中可以："
    echo "    添加/删除基金"
    echo "    配置飞书/企业微信推送"
    echo "    点击按钮查询基金数据"
    echo ""
    echo "  按回车键停止界面并返回..."
    read -r
    kill $PID 2>/dev/null || true
}

# ── 设置每日定时推送 ──────────────────────────
setup_schedule() {
    clear
    title "设置每日定时推送"

    if [ ! -f "$CONFIG_FILE" ]; then
        warn "尚未配置基金列表，请先添加基金"
        sleep 2
        edit_funds
        if [ ! -f "$CONFIG_FILE" ]; then
            return
        fi
    fi

    # 读取已有的推送配置
    FEISHU_URL=$(read_push_urls | head -1)
    WECHAT_URL=$(read_push_urls | tail -1)

    if [ -z "$FEISHU_URL" ] && [ -z "$WECHAT_URL" ]; then
        warn "未配置推送渠道，请先配置"
        sleep 1
        edit_push
        FEISHU_URL=$(read_push_urls | head -1)
        WECHAT_URL=$(read_push_urls | tail -1)
        if [ -z "$FEISHU_URL" ] && [ -z "$WECHAT_URL" ]; then
            return
        fi
    fi

    echo "  当前推送渠道："
    [ -n "$FEISHU_URL" ] && echo "    飞书 ✓"
    [ -n "$WECHAT_URL" ] && echo "    企业微信 ✓"
    echo ""
    echo "  选择推送时段："
    echo "    1) 工作日 09:00（开盘前）"
    echo "    2) 工作日 15:30（收盘后）"
    echo "    3) 工作日 09:00 + 15:30（两次）"
    echo "    4) 每天 09:00"
    echo "    5) 自定义时间"
    echo ""
    read -p "  请选择 (1-5): " TIME_SLOT
    echo ""

    local CRON_EXPR=""
    local LABEL=""
    case "$TIME_SLOT" in
        1) CRON_EXPR="0 9 * * 1-5"; LABEL="工作日 09:00" ;;
        2) CRON_EXPR="30 15 * * 1-5"; LABEL="工作日 15:30" ;;
        3) CRON_EXPR="0 9,15 * * 1-5"; LABEL="工作日 09:00 + 15:30" ;;
        4) CRON_EXPR="0 9 * * *"; LABEL="每天 09:00" ;;
        5)
            echo "  格式：24小时制，如 08:30 或 16:00"
            echo "  多个时间用逗号分隔，如 08:30,16:00"
            read -p "  请输入推送时间: " CUSTOM_TIME
            if [ -z "$CUSTOM_TIME" ]; then
                warn "未输入时间"; sleep 1; return
            fi
            local MINUTES=""
            local HOURS=""
            local IFS_OLD="$IFS"
            IFS=','; for T in $CUSTOM_TIME; do
                T=$(echo "$T" | tr -d ' ')
                local H=$(echo "$T" | cut -d: -f1)
                local M=$(echo "$T" | cut -d: -f2)
                if [ -z "$M" ]; then M="0"; fi
                if [ -z "$MINUTES" ]; then
                    MINUTES="$M"
                    HOURS="$H"
                else
                    MINUTES="$MINUTES,$M"
                    HOURS="$HOURS,$H"
                fi
            done
            IFS="$IFS_OLD"
            echo ""
            echo "  适用日期："
            echo "    1) 每天运行"
            echo "    2) 仅工作日（周一至周五）"
            read -p "  请选择 (1-2): " DAY_OPT
            local DOW="*"
            case "$DAY_OPT" in
                2) DOW="1-5"; LABEL="工作日" ;;
                *) DOW="*"; LABEL="每天" ;;
            esac
            CRON_EXPR="${MINUTES} ${HOURS} * * ${DOW}"
            LABEL="${LABEL} ${CUSTOM_TIME}"
            ;;
        *) warn "无效选择"; sleep 1; return ;;
    esac

    # 创建定时任务包装脚本
    mkdir -p "$HOME/.fund-scout"
    SCHEDULE_SCRIPT="$HOME/.fund-scout/scheduled_push.sh"
    cat > "$SCHEDULE_SCRIPT" <<'SHSCRIPT'
#!/bin/bash
# QDII-fund-scout 定时推送脚本（由 run.sh 自动生成）
CONFIG_FILE="$HOME/.fund-scout/config.json"
SCRIPT_DIR="
SHSCRIPT
    echo "$SCRIPT_DIR" >> "$SCHEDULE_SCRIPT"
    cat >> "$SCHEDULE_SCRIPT" <<'SHSCRIPT'
"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[$(date)] 配置文件不存在，跳过" >> "$HOME/.fund-scout/schedule.log"
    exit 1
fi

cd "$SCRIPT_DIR/scripts"
python3 cli.py compare --config "$CONFIG_FILE" --push feishu,wechat >> "$HOME/.fund-scout/schedule.log" 2>&1
echo "[$(date)] 推送完成" >> "$HOME/.fund-scout/schedule.log"
SHSCRIPT
    chmod +x "$SCHEDULE_SCRIPT"

    # 根据系统设置定时任务
    local OS_TYPE="$(uname -s)"
    if [ "$OS_TYPE" = "Darwin" ]; then
        # macOS: 使用 launchd
        local MINUTES=$(echo "$CRON_EXPR" | awk '{print $1}')
        local CRON_HOURS=$(echo "$CRON_EXPR" | awk '{print $2}')
        local DOW=$(echo "$CRON_EXPR" | awk '{print $5}')

        cat > "$HOME/Library/LaunchAgents/com.fundscout.push.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.fundscout.push</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCHEDULE_SCRIPT}</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
PLIST

        # 生成 StartCalendarInterval 条目
        local IFS_OLD="$IFS"
        IFS=','; for H in $CRON_HOURS; do
            HOUR=$H
            if [ "$DOW" = "*" ]; then
                # 每天
                cat >> "$HOME/Library/LaunchAgents/com.fundscout.push.plist" <<PLIST
        <dict>
            <key>Hour</key>
            <integer>${HOUR}</integer>
            <key>Minute</key>
            <integer>${MINUTES}</integer>
        </dict>
PLIST
            else
                # 工作日: 1=周一 .. 5=周五
                for D in 1 2 3 4 5; do
                    cat >> "$HOME/Library/LaunchAgents/com.fundscout.push.plist" <<PLIST
        <dict>
            <key>Hour</key>
            <integer>${HOUR}</integer>
            <key>Minute</key>
            <integer>${MINUTES}</integer>
            <key>Weekday</key>
            <integer>${D}</integer>
        </dict>
PLIST
                done
            fi
        done
        IFS="$IFS_OLD"

        cat >> "$HOME/Library/LaunchAgents/com.fundscout.push.plist" <<PLIST
    </array>
    <key>StandardOutPath</key>
    <string>${HOME}/.fund-scout/schedule.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.fund-scout/schedule.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

        launchctl load "$HOME/Library/LaunchAgents/com.fundscout.push.plist" 2>/dev/null
        info "launchd 定时任务已加载"
        info "运行日志：~/.fund-scout/schedule.log"

    elif [ "$OS_TYPE" = "Linux" ]; then
        # Linux: 使用 crontab
        (crontab -l 2>/dev/null | grep -v 'scheduled_push.sh'; echo "$CRON_EXPR bash $SCHEDULE_SCRIPT") | crontab -
        info "crontab 定时任务已添加"

    else
        # Windows
        warn "检测到 Windows 系统"
        echo ""
        echo "  请以管理员身份运行以下命令来创建定时任务："
        echo ""
        echo "  schtasks /create /tn QDIIFundScoutPush /tr \"bash $SCHEDULE_SCRIPT\" /sc DAILY /st 09:00 /f"
        echo ""
        echo "  或者在 Windows 搜索"任务计划程序" → 创建基本任务"
        echo "  设置为每天执行：bash $SCHEDULE_SCRIPT"
        echo ""
        read -p "按回车键返回..."
        return
    fi

    echo ""
    info "每日定时推送设置完成！"
    echo "  时间：$LABEL"
    echo "  推送渠道：飞书 + 企业微信（已配置的均会推送）"
    echo "  日志文件：~/.fund-scout/schedule.log"
    echo ""
    echo "  ⚠ 电脑在计划时间需要保持开机并联网"
    echo "  ⚠ 如需修改时间，先选 10) 取消定时推送，再重新设置"
    echo ""
    read -p "按回车键返回主菜单..."
}

# ── 取消定时推送 ──────────────────────────
remove_schedule() {
    clear
    title "取消定时推送"

    local OS_TYPE="$(uname -s)"
    if [ "$OS_TYPE" = "Darwin" ]; then
        if [ -f "$HOME/Library/LaunchAgents/com.fundscout.push.plist" ]; then
            launchctl unload "$HOME/Library/LaunchAgents/com.fundscout.push.plist" 2>/dev/null
            rm -f "$HOME/Library/LaunchAgents/com.fundscout.push.plist"
            info "macOS launchd 定时任务已取消"
        else
            warn "未设置过定时任务"
        fi
    elif [ "$OS_TYPE" = "Linux" ]; then
        (crontab -l 2>/dev/null | grep -v 'scheduled_push.sh') | crontab -
        info "crontab 定时任务已取消"
    else
        warn "请在 Windows 任务计划程序中手动删除 QDIIFundScoutPush 任务"
    fi

    rm -f "$HOME/.fund-scout/scheduled_push.sh"
    echo ""
    read -p "按回车键返回主菜单..."
}

# ── 主循环 ──────────────────────────
main() {
    check_deps

    while true; do
        show_menu

        case "$CHOICE" in
            2)
                if [ -f "$CONFIG_FILE" ]; then
                    run_compare "" ""
                else
                    warn "尚未配置基金列表，请先选择 3) 编辑我的基金列表"
                    sleep 2
                fi
                ;;
            4)
                clear
                title "手动输入基金代码"
                echo "  输入基金代码，多只基金用逗号分隔"
                echo "  示例：012870,006479,008971"
                echo ""
                read -p "  基金代码: " CODES
                if [ -n "$CODES" ]; then
                    run_compare "$CODES" ""
                fi
                ;;
            5)
                clear
                title "查询并推送飞书"
                FEISHU_URL=$(read_push_urls | head -1)
                if [ -z "$FEISHU_URL" ]; then
                    warn "未配置飞书 Webhook，请先选择 8) 配置推送渠道"
                    sleep 2
                else
                    if [ -f "$CONFIG_FILE" ]; then
                        run_compare "" "feishu"
                    else
                        read -p "  输入基金代码: " CODES
                        run_compare "$CODES" "feishu"
                    fi
                fi
                ;;
            6)
                clear
                title "查询并推送企业微信"
                WECHAT_URL=$(read_push_urls | tail -1)
                if [ -z "$WECHAT_URL" ]; then
                    warn "未配置企业微信 Webhook，请先选择 8) 配置推送渠道"
                    sleep 2
                else
                    if [ -f "$CONFIG_FILE" ]; then
                        run_compare "" "wechat"
                    else
                        read -p "  输入基金代码: " CODES
                        run_compare "$CODES" "wechat"
                    fi
                fi
                ;;
            7)
                clear
                title "查询并同时推送飞书 + 企业微信"
                FEISHU_URL=$(read_push_urls | head -1)
                WECHAT_URL=$(read_push_urls | tail -1)
                if [ -z "$FEISHU_URL" ] && [ -z "$WECHAT_URL" ]; then
                    warn "未配置任何推送渠道，请先选择 8) 配置推送渠道"
                    sleep 2
                else
                    if [ -f "$CONFIG_FILE" ]; then
                        run_compare "" "feishu,wechat"
                    else
                        read -p "  输入基金代码: " CODES
                        run_compare "$CODES" "feishu,wechat"
                    fi
                fi
                ;;
            3)
                edit_funds
                ;;
            8)
                edit_push
                ;;
            1)
                start_ui
                ;;
            9)
                setup_schedule
                ;;
            10)
                remove_schedule
                ;;
            11)
                clear
                title "检查并刷新季报持仓数据"
                echo "  说明: QDII 基金每季度披露持仓 (Q1=4月下旬, Q2=7月下旬,"
                echo "        Q3=10月下旬, Q4=次年1月下旬)"
                echo "        此功能会自动检查所有基金是否有新季报，"
                echo "        命中则下载并更新本地缓存。"
                echo ""
                cd "$SCRIPT_DIR/scripts"
                python3 holdings_refresh.py
                echo ""
                read -p "按回车键返回主菜单..."
                ;;
            0)
                echo ""
                info "再见！"
                exit 0
                ;;
            *)
                warn "无效选择，请输入 0-11"
                sleep 1
                ;;
        esac
    done
}

main
