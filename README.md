# QDII-fund-scout

QDII 基金限购、收益、回撤一站查询工具。命令行 / 浏览器 / 飞书或企业微信群推送，全部在本地运行。

> **数据都在本地。** 查询直连天天基金和证监会公开接口，配置和 Webhook 地址只保存在你自己的电脑上，不会上传到任何服务器。

---

## 这个工具能做什么

| 能力 | 说明 |
|------|------|
| **全市场快照** | 2 次 HTTP 拉取 2.6 万只基金的限购、净值、收益率，几秒钟出结果 |
| **多渠道推送** | 飞书 / 企业微信群机器人，手机端也能看 |
| **每日定时推送** | 配置一次，电脑自动按计划查询并推送，开盘前 / 收盘后即可看 |
| **可视化界面** | 浏览器图形化操作（http://localhost:8765），含搜索 + 多选 + 排序 + 暗色模式 |
| **命令行 CLI** | 单只详情 / 批量对比 / JSON / CSV / Markdown 导出 |
| **T-1 估值预测** | 在基金 NAV 公布前用真实持仓 + 海外行情估算当日涨跌（hybrid 模型，平均 MAE 0.56pp）|
| **季报自动跟进** | 索引文件 24 小时 TTL，启动 run.sh / 浏览器界面 / 触发定时推送时后台自动刷新，无需手动操作 |
| **Python API** | 可集成到自己的脚本或 AI Agent |

> **AI Agent 用法**：本项目同时是一个 Skill，可被 Trae / SOLO / Claude 等 AI 助手调用。Agent 调用规则、字段语义、展示约定见 [SKILL.md](SKILL.md)。

---

## 快速开始

### 安装

**方式 1：一行命令（推荐，无需 git）**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/hangliuc/QDII-fund-scout-skill/main/install.sh)
```

**方式 2：git clone**

```bash
git clone git@github.com:hangliuc/QDII-fund-scout-skill.git
cd QDII-fund-scout-skill
bash setup.sh
```

**方式 3：作为 Skill 导入 AI 助手**

在 Trae 等 AI 助手中导入本项目（页面下载 ZIP 上传），即可用自然语言查询：「查 012870 的申购限额」「对比 012870、006479」。

### 启动

```bash
bash run.sh
```

打开浏览器界面（http://localhost:8765），所有交互操作都在浏览器里完成：

- 搜索 / 多选添加基金（覆盖 700+ 只 QDII），按分类标签筛选
- 配置飞书 / 企业微信 Webhook 并测试
- 一键查询，可同时勾选推送到飞书 / 企业微信
- 设置 / 取消每日定时推送
- 表格列点击排序、暗色模式、键盘快捷键
- 季报缓存诊断（折叠面板，平时无需操作）

> **首次使用**：点右上角"+"或"添加基金"按钮选择基金，再点"查询"按钮即可。
>
> **自动化 / cron / SSH 远程**：直接调用 Python 脚本，见下方"命令行用法"。

---

## 使用方式

### 浏览器（推荐）

```bash
bash run.sh
```

在 http://localhost:8765 添加基金、配置 Webhook、点击查询，所有功能都在这里。

### 命令行

每个功能都是独立的 Python 脚本，每个都支持 `--help`：

```bash
cd scripts

# 查询、对比、推送
python3 cli.py compare 012870,006479,008971
python3 cli.py compare --config ~/.fund-scout/config.json --push feishu
python3 cli.py compare 012870,006479 --push feishu,wechat
python3 cli.py detail 012870 --holdings --csrc
python3 cli.py compare 012870,006479 --format json   # 也支持 csv / md

# T-1 估值预测
python3 predict_cli.py 012922 --main 012920

# 刷新季报缓存（cron 用法）
python3 holdings_refresh.py [--force | --stats | --funds CODES]

# 定时任务管理
python3 schedule_setup.py setup --times "09:00,15:30" --weekdays
python3 schedule_setup.py status
python3 schedule_setup.py remove
```

每个脚本完整参数运行 `python3 <脚本> --help` 查看。

T-1 估值预测、批量回测、季报刷新等高级用法见 [SKILL.md](SKILL.md) 与 `scripts/predict_cli.py --help`、`scripts/holdings_refresh.py --help`。

### Python API

```python
import sys
sys.path.insert(0, "/path/to/QDII-fund-scout/scripts")
from core.fetcher import FundFetcher

fetcher = FundFetcher()
result = fetcher.compare(["012870", "006479"])
for fund in result.funds:
    print(f"{fund.name}: {fund._purchase_info}  近1年={fund.return_1y}")
```

---

## 配置

### 基金列表 / Webhook

全部在浏览器界面里维护，配置自动保存到 `~/.fund-scout/config.json`。

也可以手动编辑：

```jsonc
{
  "my_funds": [
    {"code": "012870", "name": "易方达纳指100C", "main_code": "012869"},
    {"code": "006479", "name": "广发纳指100C"}
  ],
  "push": {
    "feishu_webhook":  "",
    "wechat_webhook":  ""
  }
}
```

完整 schema 见 `references/config.example.json`。

### Webhook 怎么获取

非必需。

**飞书**：群聊 → ⋯ → 群机器人 → 添加 Webhook 机器人 → 复制以 `https://open.feishu.cn/open-apis/bot/v2/hook/` 开头的地址，粘贴到浏览器界面"飞书"输入框，点"测试"验证。

**企业微信**：群聊 → ⋯ → 群机器人 → 新建机器人 → 复制以 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=` 开头的地址，同上。

### 每日定时推送

定时推送有 **两种方式**，按"电脑能否常开"二选一：

#### 方式 A：本机定时（电脑常开）

浏览器界面 → 右上角齿轮按钮 → "每日定时推送"：

- 工作日 09:00 / 15:30 / 每天 09:00 等预设
- 也可自定义时间
- 启用后由 macOS launchd / Linux crontab 执行（自动适配）

约束：
- 电脑在计划时间需要保持开机并联网
- macOS 合盖睡眠时定时任务不会触发
- 日志：`~/.fund-scout/schedule.log`

定时脚本会自动先刷新季报缓存再推送，所以新季报披露后无感跟进。底层由 `scripts/schedule_setup.py` 统一管理。

#### 方式 B：GitHub Actions 云端定时（推荐，电脑无需开机）

适用场景：用笔记本、电脑会合盖、希望"开盘前一定收得到推送"。完全在 GitHub 服务器跑，免费。

1. **Fork 本仓库**到你自己的 GitHub 账号
2. 在你 fork 的仓库：**Settings → Secrets and variables → Actions** 添加：
   - `FEISHU_WEBHOOK_URL`（可选）
   - `WECHAT_WEBHOOK_URL`（可选，至少配一个）
   - `QDII_FUNDS`（要查询的基金代码，逗号分隔，如 `012870,006479,012922`）
3. 复制 `.github/workflows/scheduled-push.example.yml` 为 `.github/workflows/scheduled-push.yml`（去掉 `.example`）
4. 提交到 `main` 分支，GitHub Actions 自动按时跑，结果直接推到你的群

> 默认 cron 是工作日北京 08:17（UTC 00:17 错峰），可在 yml 里改。
> 也可以在仓库 Actions 页面 **手动触发**一次（workflow_dispatch）测试。
> 你的基金清单在 Secret 里，仓库公开也不会泄露。
>
> ⚠️ **触发延迟提示**：GitHub Actions scheduled cron 是 best-effort，不保证准点。
> 实测可能延迟 30 分钟到 4+ 小时（特别是 UTC 整点 + fork 仓库）。
> 已默认错峰到 UTC 00:17。如需分钟级精确，建议用 Cloudflare Workers Cron
> 或腾讯云函数定时触发 `workflow_dispatch` 来兜底。

---

## 结果解读

### 输出示例

```
┌─────────────────────────┬────────┬──────────────┬─────────┬────────────┬──────────┬───────┬──────────────┬──────────────────────────────┐
│ 名称                    │ 代码   │ 最新涨跌     │ 近1年   │ 近一年回撤 │ 规模(亿) │ 费率% │ 申购状态     │ 市场投资TOP3                 │
├─────────────────────────┼────────┼──────────────┼─────────┼────────────┼──────────┼───────┼──────────────┼──────────────────────────────┤
│ 易方达全球成长精选..    │ 012922 │ 06-01 -0.83% │ +77.83% │      8.85% │    61.61 │   1.8 │ 限小额 20元  │ 美国46.0% / 中国内地33.1% .. │
│ 大成纳斯达克100ETF..    │ 008971 │ 06-01 +0.51% │ +14.97% │     14.12% │     6.85 │   1.3 │ 限小额 50元  │ 跟踪大盘指数                 │
└─────────────────────────┴────────┴──────────────┴─────────┴────────────┴──────────┴───────┴──────────────┴──────────────────────────────┘
```

字段释义：

- **最新涨跌** = 已公布的真实日涨跌，未公布时为估算值（带 `(估算)` 后缀）
- **近1年** = 近 1 年累计收益率
- **近一年回撤** = 过去一年从最高点跌落的最大幅度
- **申购状态** = 能否买入及每日限额
- **市场投资TOP3** = 占比最高的三个市场（来自证监会季报 PDF）

更详细的字段定义、合理范围、典型陷阱见 [references/field-glossary.md](references/field-glossary.md)。

---

## 数据来源

| 数据 | 来源 | 抓取方式 |
|------|------|----------|
| 净值 / 申购状态 / 限额 | 天天基金 JJJZ 全市场快照 | 1 次 HTTP 拉 2.6 万只 |
| 近 1 年 / 3 年收益率 | 天天基金 RANKING 全市场排行 | 1 次 HTTP 拉 2.4 万只 |
| 规模 / 费率 | 天天基金档案页 | 每只 1 次 HTTP，并行 ~1s |
| 近一年回撤 | 天天基金 NAV API | 每只 ~250 条 NAV |
| 市场分布 / 行业分布 | 证监会基金季报 PDF | 自动取最新季报，本地缓存 |
| FoF / ETF 联接持仓 | 证监会基金季报 PDF | QDII-LOF / 指数基金适配 |
| 海外股票 / ETF 实时行情 | yfinance（Yahoo Finance）| T-1 估值预测使用 |
| 美元兑人民币汇率 | yfinance（USDCNY=X）| T-1 估值汇率层 |

完整数据源 URL 速查：[references/data-sources.md](references/data-sources.md)。

### 数据流

```
┌──────────────────── 用户调用 compare ────────────────────┐
│                                                           │
│  ┌─────────────────┐   ┌─────────────────┐               │
│  │ JJJZ 全市场快照 │   │ RANKING 全市场  │  2 次 HTTP    │
│  │ 限购 + 净值     │   │ 收益率          │  ~1.5 秒      │
│  └────────┬────────┘   └────────┬────────┘               │
│           └──────────┬──────────┘                         │
│                      ▼                                    │
│              ┌──────────────────┐                         │
│              │ 本地查表 + 组装  │  毫秒级，无网络         │
│              └────────┬─────────┘                         │
│                       │                                   │
│       ┌───────────────┼───────────────┐                   │
│       ▼               ▼               ▼                   │
│  ┌──────────┐  ┌─────────────┐  ┌──────────────┐         │
│  │ 档案页   │  │ CSRC 季报   │  │ T-1 估值预测 │         │
│  │ 规模/费率│  │ 地区/行业   │  │ yfinance + 模型│        │
│  └──────────┘  └─────────────┘  └──────────────┘         │
│   并行补充     按需，本地缓存    按需（未公布时）         │
│                                                           │
│  降级路径：JJJZ/RANKING 失败 → 逐只 HTML 详情页抓取       │
└───────────────────────────────────────────────────────────┘
```

---

## 项目结构

```
QDII-fund-scout/
├── README.md                # 用户文档（本文件）
├── SKILL.md                 # AI Agent 调用契约
├── setup.sh / install.sh    # 安装入口
├── run.sh                   # 浏览器启动器
├── requirements.txt         # 依赖锁
├── references/              # Agent 引用文档（数据源、字段、校验、合规）
├── reports/                 # 回测报告
├── ui/                      # 浏览器界面
│   ├── server.py                # HTTP server
│   ├── index.html               # 页面骨架
│   └── app.js                   # 前端交互逻辑
└── scripts/
    ├── cli.py                  # 命令行入口
    ├── predict_cli.py          # T-1 估值预测
    ├── run_backtest.py         # 批量回测
    ├── holdings_refresh.py     # 季报刷新
    ├── schedule_setup.py       # 定时任务统一管理
    ├── core/
    │   ├── fetcher.py          # 调度 + 交叉验证
    │   ├── models.py           # 数据模型
    │   ├── validate.py         # 数据校验
    │   ├── predict_inline.py   # 最新涨跌轻量预测
    │   ├── sources/            # 天天基金 / CSRC 数据源
    │   ├── quotes/             # yfinance 行情
    │   └── predict/            # T-1 估值模型 + 回测
    ├── formatters/             # JSON / CSV / Markdown 格式化
    └── adapters/               # 飞书 / 企业微信推送
```

---

## 常见 QDII 基金参考

完整名单（含分类）保存在 `references/qdii_fund_list.json`，覆盖 700+ 只 QDII 基金，由天天基金实时数据生成。

热门基金（也可在浏览器界面通过搜索 / 多选弹层添加）：

| 代码 | 名称 | 类型 |
|------|------|------|
| 008971 | 大成纳斯达克100ETF联接C | 纳斯达克100 指数 |
| 006479 | 广发纳斯达克100ETF联接C | 纳斯达克100 指数 |
| 012870 | 易方达纳斯达克100ETF联接C | 纳斯达克100 指数 |
| 012922 | 易方达全球成长精选C | QDII-主动 |
| 539002 | 建信新兴市场混合A | QDII-主动 |
| 161128 | 易方达标普信息科技A | 指数型-海外股票 |
| 017204 | 华宝海外科技C | QDII-主动 |
| 015202 | 汇添富全球移动互联C | QDII-主动 |

---

## License

MIT

## Disclaimer

**数据来源及版权**：所有数据来源于天天基金、证监会基金披露网站、Yahoo Finance 等公开渠道。数据著作权归原始平台或数据提供方所有。

**不构成投资建议**：本工具数据仅供个人学习研究参考，不构成任何投资建议。基金投资有风险，过往业绩不代表未来表现，申购限额可能随时变动。

**禁止商业使用**：禁止商业数据转售、构建竞争性产品、向第三方提供付费数据 API。未经授权不得转载、分发基金季报 PDF 原文。

完整合规文档见 [references/compliance.md](references/compliance.md)。
