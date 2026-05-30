---
name: "QDII-fund-scout"
description: "从天天基金、证监会基金披露网站等公开权威渠道抓取基金信息。覆盖单只基金详情（主页、经理、持仓、费率、回撤）、批量基金筛选（指数 / 主题）、季报披露数据（市场投资分布、行业分布等PDF解析）。强制要求：禁止硬编码基金代码列表、禁止伪造数据、所有关键字段必须经过显式校验，并通过内置的数据自校验机制（validate.py）做范围 / 一致性 / 跨源核对。当需要抓取基金数据并保存为 data.json 时使用。"
---

# QDII-fund-scout · QDII 基金数据获取 Skill

专为 QDII 基金设计的开源数据获取工具。从天天基金、证监会基金披露网站等公开权威渠道抓取数据，支持格式化输出和飞书/微信推送。

**核心能力**：限额数据获取 · 收益率对比 · 回撤计算 · 权威季报持仓/地区分布

## Quick Start · 5 分钟上手

### 1. 获取单只 QDII 基金详情

```bash
cd ~/.trae-cn/skills/fund-scout/scripts
python3 -c "
import sys; sys.path.insert(0, '.')
from core.fetcher import FundFetcher
fetcher = FundFetcher()
fund = fetcher.get_detail('012870', include_holdings=True, include_csrc=True)
print(f'基金: {fund.name}')
print(f'近1年收益: {fund.return_1y}%')
print(f'近1年回撤: {fund.drawdown_1y}%')
print(f'申购状态: {fund.purchase_status} {fund.purchase_limit}')
print(f'费率: 管理{fund.mgmt_fee}% + 托管{fund.custody_fee}% + 服务{fund.service_fee}% = 综合{fund.total_fee}%')
print(f'市场分布: {fund.market_distribution}')
"
```

### 2. 批量对比 QDII 基金限额

```bash
python3 cli.py compare 012870,006479,008971 --format md
```

输出 Markdown 表格，一眼看清各基金限额差异：

| 代码 | 名称 | 近1年 | 费率 | 限购 | 实质暂停 |
|------|------|-------|------|------|---------|
| 012870 | 易方达纳指100C | 30.44% | 0.90% | 暂停 | ✅ |
| 006479 | 广发纳指100C | 36.79% | 1.20% | 10元/天 | ✅ |
| 008971 | 大成纳指100C | 36.65% | 1.30% | 500元/天 | ✅ |

### 3. 获取证监会季报地区分布

```bash
python3 cli.py detail 012870 --csrc --format json
```

输出权威的季报市场投资分布（来自证监会 PDF 原文，非天天基金推断）：

```json
{
  "market_distribution": {
    "美国": 92.5,
    "中国香港": 4.5,
    "_source": "csrc_2026Q1",
    "_total_pct": 97.0
  }
}
```

### 4. 使用配置文件管理你的基金列表

```bash
# 创建配置文件
cp references/config.example.json ~/.fund-scout/config.json

# 编辑你的基金列表后一键获取
python3 cli.py compare --config ~/.fund-scout/config.json --format md --push feishu
```

## When to use

- 需要 QDII 基金完整信息（限额 + 收益 + 回撤 + 持仓 + 季报分布）→ `detail {code} --holdings --csrc`
- 需要批量 QDII 基金对比（限额差异一目了然）→ `compare {codes}` 或 `search "纳斯达克100" --type QDII`
- 需要证监会季报市场/行业分布（最权威）→ `detail {code} --csrc`
- 需要校验已有数据 → `validate data.json --profile qdii`
- 需要推送到飞书/微信 → `--push feishu` / `--push wechat`
- Python API 集成 → `from core.fetcher import FundFetcher`
- 定时自动化 → 配合 SOLO Schedule / OpenClaw / Harness 使用

## QDII 专项数据说明

QDII 基金与普通基金的关键差异，本 skill 已针对性处理：

| 数据项 | QDII 特殊性 | 本 skill 处理方式 |
|--------|------------|-----------------|
| **申购限额** | QDII 限购极为频繁，10元/天~5000万/天不等 | 四态校验 + `effectively_closed` 标记 |
| **市场分布** | 投资全球市场，地区分布是核心指标 | 证监会季报 PDF 原文解析（非天天基金推断） |
| **市场投资TOP3** | 用户最关心的市场分布摘要 | `market_top3` 字段：主动基金显示 TOP3 市场（如"美国46.0% / 中国内地33.1%"）；指数/ETF联接基金显示"跟踪大盘指数"；季报无持仓显示"季报无股票持仓" |
| **回撤** | 海外市场波动大，回撤是关键风险指标 | 分页拿全量 NAV 计算，单页 20 条会严重偏小 |
| **汇率影响** | QDII 净值受汇率影响 | 输出中标注人民币/美元份额 |
| **持仓** | 海外股票代码非纯数字（如 00700.HK） | 港股代码特殊处理，不跳过 |
| **费率** | C 类综合费率差异大（0.9%~1.5%） | 三费拆分 + 合计校验 |

## 常用基金参考列表

当用户查询特定类别基金时，优先使用 `references/` 下的预审列表作为候选代码来源（无需再调用搜索接口）：

| 基金类别 | 参考列表 | 说明 |
|---------|---------|------|
| 全量 QDII 基金 | `#[[file:references/qdii_fund_list.json]]` | 727 只，含分类，由天天基金实时数据生成 |
| 纳斯达克被动QDII C类 | `#[[file:references/nasdaq_passive_qdii_c_funds.json]]` | 17 只主流跟踪纳指100的C类份额 |

用法：Agent 读取对应 JSON 文件，取出 `code` 列表，调用 `fetch_batch()` 批量拉取数据，再按需排序、格式化、推送。

## ⚠ 四条铁律

### 铁律 1 · 禁止硬编码基金代码列表

当用户要求"抓取所有 XXX"时，**禁止**在 Python 脚本或配置里写死候选基金代码数组。

**正确做法**：必须先调用 `http://fund.eastmoney.com/js/fundcode_search.js` 拿到全市场基金清单，再用代码/名称/类型字段做筛选。

**例外**：`references/` 目录下的 `.json` 文件是预审通过的常用基金参考列表，如 `nasdaq_passive_qdii_c_funds.json`。Agent 可以直接读取此列表作为候选基金代码来源，但仍需校验数据准确性并注明来源。

如果用户明确给了代码列表（如通过 config.json），**必须注明来源**。

### 铁律 2 · 禁止伪造任何数据

- 禁止生成示例数据、占位数据充当真实结果
- 禁止把网络请求失败时的默认值当作有效数据
- 凡是没抓到的字段，要么重试，要么显式写入 `null` / `""` 并在控制台打 `! 缺失`
- 禁止用大模型「常识」补全字段。**只信抓回来的 HTML / PDF**

### 铁律 3 · 关键字段必须显式验证

抓回来的字段必须经过校验，不通过的标 `verified: false` 并告警。详见 `scripts/core/validate.py` 与 `references/validation-rules.md`。

| 字段 | 验证方法 | 典型陷阱 |
|------|---------|----------|
| `purchase_status` | 四态校验 | 显示「限购 100 元/天」实际是暂停申购 |
| `purchase_limit` | 单位识别 + 数值范围 | 把 `100` 当成 100 万元 |
| `total_fee` | 等于 mgmt + custody + service | 漏掉销售服务费导致 C 类费率失真 |
| `return_*` | 主页面 + NAV API + 好买基金三路对照 | 排行接口未覆盖时返回空字符串 |
| `scale` | 主页和档案页交叉验证 | 主页延迟，用档案页更准 |
| `drawdown_1y` | 必须分页拿全量 NAV | 单页 20 条→回撤严重偏小 |
| `market_distribution` | 来自证监会季报 PDF；百分比合计 ≤ 100% | 天天基金的"地区分布"不准，必须用 CSRC 原文 |

### 铁律 4 · 并发请求规则

- 单只基金内部的全部 HTTP 请求使用 `ThreadPoolExecutor(max_workers=8)` 并发执行
- 多只基金之间使用 `_fetch_batch_parallel` 并行获取（最多 4 workers）
- 交叉验证（好买基金）与 CSRC 季报获取在同一线程池中并行执行，减少总耗时
- 发生连续失败时自动降低并行度（`_fail_count` + `threading.Lock` 保护）
- 单个请求超时 `timeout=20`

### 铁律 5 · 数据源降级与不可用处理

主数据源（天天基金 eastmoney）获取失败时，自动切换到备用数据源（好买基金 howbuy）。
两个数据源都失败时，`FundInfo.data_unavailable` 被标记为 `True`，`data_source` 为 `"unavailable"`。

**Agent 必须检查 `data_unavailable` 字段：**
- 如果 `data_unavailable == True`，**禁止**向用户展示该基金数据
- 在推送结果中应提示："数据暂不可用，请稍后重试"
- 批量场景下，在 `_warnings` 中汇总不可用数量

**所有解析阶段的异常统一处理方式：**
- 使用 Python `logging` 模块输出详细错误（不静默吞异常）
- 单个字段解析失败不影响其他字段
- 单个页面（如经理页、费率页）获取失败不影响主页核心数据
- CSRC 数据源失败返回含 `"_note": "not_found"` 标记的空结果
- CSRC 搜索 C 类基金时，`_simplify_name` 自动去除 A/C/D/E 后缀、货币标记（人民币/美元）、类型词（混合/股票）、QDII 标记，生成多个搜索变体逐个尝试

### 交叉验证策略（三路径）

数据从天天基金（主）拉取后，自动做三层验证：

1. **天天基金内部多路径验证**：通过 NAV API 独立计算近 1 年收益率，与主页面解析的收益率比对（差异 <1-2% 视为一致）
2. **好买基金外部验证**：天天基金与好买基金比对申购状态、收益率、规模等关键字段
3. **净值新鲜度检查**：最近净值日期是否超过 7 天，过期则提示"限额可能已变化"

验证结果对用户完全透明：
- ✅ 数据一致 → 后台确认，用户无感知
- ⚠️ 净值过期 → 显示 `⚠ 净值超过 7 天未更新，申购限额可能已变化`
- ☒ 数据不可用 → 显示 `⚠ 数据暂不可用（所有数据源均失败）`

## 申购状态四态校验（QDII 核心功能）

QDII 基金限购频繁变动，是用户最关心的数据之一。

| 显示文本 | `purchase_status` | `purchase_limit` | 解读 |
|---------|-------------------|------------------|------|
| `开放申购` | `"开放"` | `"无限制"` | 正常可买 |
| `限大额(单日累计购买上限 X 万元)` | `"限大额"` | `"X 万"` | 大额限购，小额可买 |
| `限大额(单日累计购买上限 X 元)` | `"限小额"` | `"X 元"` | **实际接近暂停申购** |
| `暂停申购` | `"暂停"` | `"0"` | 完全不能买 |

当申购状态为 `"暂停"` 时，附加 `"effectively_closed": true`，表示该基金实质暂停申购。**禁止**把这种基金宣传为「可申购」。

## 用户自定义配置

### 配置文件路径

`~/.fund-scout/config.json`（首次使用自动创建）

### 配置文件格式

```jsonc
{
  "my_funds": [
    {"code": "012870", "name": "易方达纳指100C", "main_code": "012869"},
    {"code": "006479", "name": "广发纳指100C", "main_code": "006479"},
    {"code": "008971", "name": "大成纳指100C", "main_code": "008970"}
  ],
  "push": {
    "feishu_webhook": "",
    "wechat_webhook": ""
  },
  "defaults": {
    "format": "md",
    "style": "table",
    "profile": "qdii",
    "include_holdings": true,
    "include_csrc": true
  }
}
```

### 配置项说明

| 字段 | 说明 | 示例 |
|------|------|------|
| `my_funds` | 你持有的/关注的基金列表 | code 必填，name/main_code 选填 |
| `push.feishu_webhook` | 飞书机器人 Webhook URL | `https://open.feishu.cn/open-apis/bot/v2/hook/xxx` |
| `push.wechat_webhook` | 企业微信机器人 Webhook URL | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` |
| `defaults.format` | 默认输出格式 | json / csv / md |
| `defaults.style` | Markdown 样式 | table / card / summary |
| `defaults.profile` | 默认校验 profile | qdii（推荐） |
| `defaults.include_holdings` | 默认是否获取持仓 | true |
| `defaults.include_csrc` | 默认是否获取季报分布 | true |

### 使用配置文件

```bash
# 用配置文件中的基金列表批量获取
python3 cli.py compare --config ~/.fund-scout/config.json --format md

# 用配置文件中的 Webhook 推送
python3 cli.py compare --config ~/.fund-scout/config.json --push feishu
```

## 推送渠道配置指南

### 飞书机器人配置

1. 打开飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 复制 Webhook URL（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`）
3. 写入配置文件或设置环境变量：

```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
```

4. 测试连接：

```bash
python3 cli.py test feishu
```

### 企业微信机器人配置

1. 打开企业微信群 → 添加群机器人 → 新建机器人
2. 复制 Webhook URL（格式：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`）
3. 写入配置文件或设置环境变量：

```bash
export WECHAT_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx"
```

4. 测试连接：

```bash
python3 cli.py test wechat
```

### 推送渠道格式

| 渠道 | card 格式 | text 格式 |
|------|----------|----------|
| 飞书 | 交互式卡片（Grid 布局 + 指标高亮） | 纯文本摘要 |
| 微信 | Markdown 表格 + 引用式告警 | 纯文本摘要 |

## 集成与自动化场景

### 场景 1：SOLO Schedule 定时推送

使用 SOLO 的 Schedule 功能，每天定时获取基金数据并推送：

```
# 每天早上 9:00 获取我的 QDII 基金数据并推送到飞书
Schedule: 0 9 * * 1-5
Message: 使用 fund-scout skill 获取我配置文件中的 QDII 基金数据（~/.fund-scout/config.json），重点获取限额状态变化，格式化为 Markdown 表格推送到飞书，如果有限额状态变化要特别标注
```

### 场景 2：Python 脚本集成

```python
import sys
sys.path.insert(0, "/path/to/fund-scout/scripts")
from core.fetcher import FundFetcher
from formatters.csv_fmt import format as csv_format

fetcher = FundFetcher()
result = fetcher.compare(
    codes=["012870", "006479", "008971"],
)
csv_format(result, "qdii_daily.csv")

# 接入你自己的分析工具
import pandas as pd
df = pd.read_csv("qdii_daily.csv", header=[0, 1])
print(df[["基金代码", "近1年", "综合费率"]].describe())
```

### 场景 3：OpenClaw / Harness 自动化

将 fund-scout 作为数据处理管线的一环：

```yaml
# OpenClaw pipeline 示例
pipeline:
  - name: fetch_qdii_data
    type: python
    script: /path/to/fund-scout/scripts/cli.py
    args: ["compare", "--config", "~/.fund-scout/config.json", "--format", "json", "--output", "/tmp/qdii_data.json"]

  - name: analyze_and_chart
    type: python
    script: your_analyze_script.py
    input: /tmp/qdii_data.json

  - name: notify
    type: webhook
    url: ${FEISHU_WEBHOOK_URL}
    template: qdii_daily_report
```

### 场景 4：CI/CD 数据监控

在 GitHub Actions / GitLab CI 中定时运行：

```yaml
# .github/workflows/qdii-monitor.yml
name: QDII Fund Monitor
on:
  schedule:
    - cron: '0 9 * * 1-5'  # 工作日 9:00
jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Fetch QDII data
        run: |
          pip install requests pdfplumber
          cd fund-scout/scripts
          python3 cli.py compare --config config.json --format md --push feishu
        env:
          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}
```

## 标准工作流

1. **明确目标**：单只详情 / 批量对比 / 季报数据？
2. **选择调用方式**：CLI / Python API / Agent 自动调用 / 配置文件
3. **执行获取**：`fund-scout detail/compare/search`
4. **自动校验**：内置 validate，fatal 错误必须修复
5. **格式化输出**：`--format json/csv/md`
6. **按需推送**：`--push feishu/wechat`
7. **人工抽检**：随机 2~3 只与源网页对照

## CLI 完整用法

```bash
# 单只基金详情
python3 cli.py detail 012870
python3 cli.py detail 012870 --holdings --csrc

# 批量对比
python3 cli.py compare 012870,006479,008971
python3 cli.py search "纳斯达克100" --type QDII
python3 cli.py search "标普500" --type 指数型 --class C

# 使用配置文件
python3 cli.py compare --config ~/.fund-scout/config.json

# 输出格式
python3 cli.py compare 012870,006479 --format json
python3 cli.py compare 012870,006479 --format csv
python3 cli.py compare 012870,006479 --format md --style card
python3 cli.py compare 012870,006479 --format md --style summary

# 推送
python3 cli.py compare 012870,006479 --push feishu
python3 cli.py compare 012870,006479 --push wechat --push-format card
python3 cli.py compare 012870,006479 --push feishu,wechat

# 校验
python3 cli.py validate data.json --profile qdii

# 测试连接
python3 cli.py test feishu
python3 cli.py test wechat
```

## Python API

```python
import sys
sys.path.insert(0, "/path/to/fund-scout/scripts")

from core.fetcher import FundFetcher
from formatters.json_fmt import format as json_format
from adapters.feishu import FeishuAdapter

fetcher = FundFetcher(rate_limit=1.0)

# 单只 QDII 基金（含持仓 + 季报分布）
fund = fetcher.get_detail("012870", include_holdings=True, include_csrc=True)
print(fund.name, fund.return_1y, fund.drawdown_1y, fund.market_distribution)

# 批量对比
result = fetcher.compare(keyword="纳斯达克100", fund_type="QDII")

# 格式化输出
json_format(result, "data.json")

# 推送到飞书
adapter = FeishuAdapter(webhook_url="https://open.feishu.cn/...")
adapter.send(result, fmt="card")
```

## 数据自校验机制

校验分四层：

| 层 | 校验内容 | 典型问题 |
|----|---------|---------|
| L1 Schema | 必需字段是否齐全，类型是否正确 | 漏抓 `manager_name` / `nav` |
| L2 Range | 字段值在合理区间 | 收益率 1000%、规模负数 |
| L3 Consistency | 相关字段间逻辑一致 | `total_fee ≠ mgmt + custody + service` |
| L4 Cross-source | 同一字段多源数值一致 | 主页规模 vs 档案规模 |

Profile 决定必填字段集：

| Profile | 适用场景 | 必填字段 |
|---------|---------|---------|
| `quick` | 快速查看 | code, name, nav, return_1y |
| `compare` | 批量对比 | code, name, scale, return_1y, purchase_status, total_fee |
| `detail` | 完整详情 | 全部字段 |
| `qdii` | QDII 专项（推荐） | compare + market_distribution, drawdown_1y |

## 常用数据源 URL 速查

完整版见 `#[[file:references/data-sources.md]]`。

| 用途 | URL 模板 |
|------|---------|
| 全量基金清单 | `http://fund.eastmoney.com/js/fundcode_search.js` |
| 基金主页 | `http://fund.eastmoney.com/{code}.html` |
| 基金档案 | `http://fundf10.eastmoney.com/jbgk_{code}.html` |
| 基金经理 | `http://fundf10.eastmoney.com/jjjl_{code}.html` |
| 季度持仓 | `http://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year={year}` |
| NAV 历史 | `https://api.fund.eastmoney.com/f10/lsjz` |
| 证监会季报搜索 | `http://eid.csrc.gov.cn/fund/disclose/advanced_search_report.do` |
| 证监会季报 PDF | `http://eid.csrc.gov.cn/fund/disclose/instance_show_pdf_id.do?instanceid={id}` |

## 免责声明

每次输出自动附带：

> ⚠️ 数据来源：天天基金/证监会公开信息，仅供个人研究参考
> ⚠️ 本工具不构成任何投资建议，数据准确性以官方源为准
> ⚠️ 禁止将本工具用于商业数据转售或与数据源平台竞争
> ⚠️ 使用者需遵守当地法律法规，因滥用产生的法律责任由使用者承担

## Scripts

- CLI 入口：`#[[file:scripts/cli.py]]`
- 请求调度器：`#[[file:scripts/core/fetcher.py]]`
- 数据模型：`#[[file:scripts/core/models.py]]`
- 校验机制：`#[[file:scripts/core/validate.py]]`
- 天天基金数据源（主）：`#[[file:scripts/core/sources/eastmoney.py]]`
- 好买基金数据源（备用）：`#[[file:scripts/core/sources/howbuy.py]]`
- 数据源基类：`#[[file:scripts/core/sources/base.py]]`
- 证监会数据源：`#[[file:scripts/core/sources/csrc.py]]`
- JSON 格式化：`#[[file:scripts/formatters/json_fmt.py]]`
- CSV 格式化：`#[[file:scripts/formatters/csv_fmt.py]]`
- Markdown 格式化：`#[[file:scripts/formatters/markdown_fmt.py]]`
- 适配器注册：`#[[file:scripts/adapters/__init__.py]]`
- 飞书适配器：`#[[file:scripts/adapters/feishu.py]]`
- 微信适配器：`#[[file:scripts/adapters/wechat.py]]`

## References

- 数据源 URL 速查：`#[[file:references/data-sources.md]]`
- 字段语义与陷阱：`#[[file:references/field-glossary.md]]`
- 校验规则完整列表：`#[[file:references/validation-rules.md]]`
- 合规指南 + 免责声明：`#[[file:references/compliance.md]]`
- 配置文件示例：`#[[file:references/config.example.json]]`
- QDII 全量基金列表：`#[[file:references/qdii_fund_list.json]]`
- 纳斯达克被动QDII C类基金参考列表：`#[[file:references/nasdaq_passive_qdii_c_funds.json]]`

## Out of scope

本 skill 不负责：
- 数据可视化（小红书风格卡片）→ xhs-fund-holdings-analysis
- 小红书文案写作 → 由可视化 skill 内置规则处理
- 敏感词检测 → xhs-sensitive-word-check
- 自动发布笔记到小红书
- 投资建议生成
- 合规审查 → 使用者须自行遵守 `#[[file:references/compliance.md]]` 中的合规要求
- 替使用者承担数据获取和使用的法律责任
- 实时行情推送（非公开数据）
