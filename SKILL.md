---
name: "QDII-fund-scout"
description: "查询 QDII 基金数据（限额、净值、收益率、回撤、季报地区分布、T-1 估值预测）。从天天基金、证监会基金披露公开接口抓取，强制要求：禁止硬编码基金代码列表、禁止伪造数据、所有关键字段必须经过显式校验。当用户要求查询、对比、推送 QDII 基金信息时使用。"
---

# QDII-fund-scout · Agent 调用契约

> 这份文档面向 AI Agent。**用户文档（安装、菜单、Webhook 配置）见 [README.md](README.md)，本文件不重复。**

本 Skill 的能力、安装、UI、定时任务等说明请直接引用 README。本文件只规定 Agent **如何调用、如何展示、如何避免错误**。

---

## When to use

| 用户意图 | Agent 应调用 |
|---------|------------|
| 查询单只 QDII 详情 + 季报 + 持仓 | `cli.py detail {code} --holdings --csrc` |
| 批量对比多只 QDII | `cli.py compare {codes}` 或 `--config ~/.fund-scout/config.json` |
| 按关键词找基金 + **拿真实限额数据** | **`cli.py search "纳斯达克100" --type QDII --class C --with-limits`** |
| 仅按关键词列出基金代码（不带详情）| `cli.py search "纳斯达克100" --type QDII` |
| 推送到飞书 / 企业微信 | `--push feishu` / `--push wechat` / `--push feishu,wechat` |
| 校验已有数据文件 | `cli.py validate data.json --profile qdii` |
| 预测尚未公布的当日 NAV 涨跌 | `predict_cli.py {code}` 或 `compare ... --format json`（含 `t1_prediction`）|
| 检查 / 刷新季报持仓缓存 | `holdings_refresh.py` |
| **读 / 写用户的"我的基金"列表** | **`cli.py funds list/add/remove/clear`（详见下文）** |
| Python 集成 | `from core.fetcher import FundFetcher` / `from core.predict import Predictor` |
| 数据可视化（小红书图卡） | **不在本 skill 范围**，转 xhs-fund-holdings-analysis |
| 投资建议 / 合规审查 | **不做**，只输出数据 |

> ⚠️ **重要**：`cli.py search` 默认只返回基金代码 + 名称 + 类型 + 拼音（来自 fundcode_search.js），
> **不含限额、收益率、费率、规模等核心字段**。如果用户问"列出限额情况"等，
> 必须用 `--with-limits`，或先 search 拿代码再 `compare` 拿详情。
> **禁止**直接拿 search 的结果当详情展示给用户（会让申购状态显示为空或错误）。

---

## 用户基金列表协议（重要）

**问题**：Agent 看不到用户磁盘上的 `~/.fund-scout/config.json`，无法直接知道用户持有哪些基金。

**解决**：用户基金列表持久化在配置文件里，agent 通过 `cli.py funds` 子命令读写，浏览器界面也写同一份文件。这样跨对话、跨界面状态一致。

### Agent 标准流程

**新对话开始时（用户没明示代码就要查询）**：

```bash
python3 cli.py funds list --format json
```

返回 `[]` 说明用户从未保存过基金 → **询问用户**："你常关注哪些 QDII 基金？告诉我代码就好（如 012870、006479），我帮你记下来。"

返回 `[{code,name}, ...]` → **直接用这些基金继续操作**，不要再问。

**用户提到新代码 / 新基金时**：

```bash
python3 cli.py funds add 012870 006479                       # 仅代码
python3 cli.py funds add "012870:易方达纳指100C" "006479:广发纳指100C"   # 带名字
```

幂等：已存在的代码会跳过；带 `:name` 会更新名称。

**用户说"不再关注 / 移除 / 删掉"时**：

```bash
python3 cli.py funds remove 012870
```

### 决策树

```
用户问"查我持仓基金的限购情况"
   │
   ├─ 用户消息里带了 6 位代码？
   │     └─ 是 → 直接 cli.py compare {codes}
   │             同时主动追问"要把它们记下来吗？" → funds add ...
   │
   └─ 没带代码
         └─ funds list --format json
               ├─ 非空 → cli.py compare --config ~/.fund-scout/config.json
               └─ 空    → 询问用户，拿到代码后 funds add ... 再 compare
```

### 边界情况

- 用户给的不是 6 位数字（如基金简称）→ 先 `cli.py search "{关键词}" --type QDII` 找代码再写入
- 用户在浏览器界面已经维护过基金 → `funds list` 能读到，不要重复问
- 用户说"清空"或"重新开始" → `funds clear --yes`
- Agent 写入失败（权限 / 磁盘满）→ stdout 会有错误信息，告诉用户

### 输出示例

```bash
$ python3 cli.py funds list --format json
[
  {"code": "012870", "name": "易方达纳指100C"},
  {"code": "006479", "name": "广发纳指100C"}
]

$ python3 cli.py funds add "012922:易方达全球成长精选" --format json
{
  "added": [{"code": "012922", "name": "易方达全球成长精选"}],
  "skipped": [],
  "invalid": [],
  "total": 3
}
```

---

## 四条铁律

### 铁律 1 · 禁止硬编码基金代码列表

当用户说"抓取所有 XXX 类型基金"时，**禁止**在脚本里写死候选代码数组。正确做法：

1. 优先读取 `references/` 下的预审 JSON 列表（如 `qdii_fund_list.json`、`nasdaq_passive_qdii_c_funds.json`）
2. 否则调用 `http://fund.eastmoney.com/js/fundcode_search.js` 拿全市场清单后筛选

如果用户明确给了代码列表（config.json / 直接列出），**必须注明来源**。

### 铁律 2 · 禁止伪造数据

- 禁止生成示例数据、占位数据当作真实结果
- 禁止把网络请求失败的默认值当作有效数据
- 没抓到的字段：要么重试，要么显式 `null` / `""` 并标 `! 缺失`
- **禁止用大模型常识补全字段**。只信抓回的 HTML / PDF / API

### 铁律 3 · 关键字段必须显式校验

抓回字段必须经 `core.validate.validate_data` 校验，校验规则定义在 [references/validation-rules.md](references/validation-rules.md)。常见陷阱：

| 字段 | 典型陷阱 |
|------|---------|
| `purchase_status` | 显示"限购 100 元/天"实际接近暂停申购 → `effectively_closed: true` |
| `purchase_limit` | 把 `100` 当成 100 万元（要识别"万"单位）|
| `total_fee` | 漏掉销售服务费导致 C 类费率失真 |
| `return_*` | RANKING 接口未覆盖时返回空字符串 |
| `scale` | 主页延迟，档案页更准 |
| `drawdown_1y` | 单页 20 条 NAV 算回撤会严重偏小，必须分页拉全 1 年 |
| `market_distribution` | 天天基金的"地区分布"不准，只信 CSRC 季报 PDF |

### 铁律 4 · 检查 `data_unavailable`

主源失败会自动降级到 HTML 详情页。全部失败时 `FundInfo.data_unavailable = True`，`data_source = "unavailable"`。

**Agent 必须**：
- `data_unavailable == True` 时，**禁止**展示该基金数据
- 推送结果中提示"数据暂不可用，请稍后重试"
- 批量场景中，在 `_warnings` 汇总不可用数量

---

## 输出契约

### 控制台表格输出（compare / detail md 格式）

每次查询完成后必须以**表格形式输出到控制台**，列定义：

| 代码 | 名称 | 最新涨跌 | 近1年 | 近一年回撤 | 规模(亿) | 费率% | 申购状态 | 市场投资TOP3 |
|------|------|---------|-------|-----------|---------|-------|---------|------------|

约束：
- 优先用 `rich.table.Table`，无 rich 时用等宽 ASCII
- 名称截断至 18 字符
- 收益率正数加 `+` 前缀，2 位小数
- `market_top3` 无数据显示 `-`
- 表格下方输出一行自然语言摘要：`"共查询 N 只基金，X 条提示"`

### JSON 输出契约（compare / detail json 格式）

每只基金结构（已自动过滤下划线开头的内部字段）：

```jsonc
{
  "code": "012922",
  "name": "易方达全球成长精选混合(QDII)C",
  "short_name": "易方达全球成长精选",
  "type": "QDII",
  "nav": 2.6184,
  "nav_date": "06-01",
  "scale": 61.61,
  "return_1y": 77.83,
  "drawdown_1y": 0.0885,
  "purchase_status": "限小额",
  "purchase_limit": "20元",
  "effectively_closed": false,
  "purchase_info": "限小额 20元",
  "total_fee": 1.8,
  "market_distribution": {
    "美国": 46.0,
    "中国内地": 33.1,
    "_source": "csrc_2026Q1",
    "_total_pct": 79.1
  },
  "market_top3": "美国46.0% / 中国内地33.1% / ...",
  "data_source": "eastmoney_bulk",
  "data_unavailable": false,
  "t1_prediction": { ... }   // 见下文
}
```

### `t1_prediction` 字段（agent 展示重点）

`compare` / `detail` JSON 输出会自动附带每只 QDII 基金的预测结果：

```jsonc
{
  "value": 1.85,                    // 涨跌（%）
  "date": "2026-06-02",             // 对应日期
  "is_estimate": true               // true=估算，false=已公布真值
}
```

完整模型回测信息（用 `predict_cli.py --json` 才有）：

```jsonc
{
  "code": "012922",
  "target_date": "2026-06-02",
  "prev_date": "2026-06-01",
  "model": "hybrid",
  "predicted_pct": 1.85,
  "actual_pct": null,               // 已公布则有值
  "components_top": [               // Top5 贡献因子
    {"name": "新易盛", "pp": 0.595},
    {"name": "源杰科技", "pp": 0.558}
  ],
  "exposure": {
    "top10_total_pct": 51.8,
    "foreign_pct": 92.5
  }
}
```

### Agent 必须遵守的展示规则

- `is_estimate: true` 时**必须**标注"估算"或"预测"，不能让用户误解为真实净值
- `value === null` 且时间是美股未收盘时段 → 必须告诉用户"美股 T 日尚未收盘，预测会有滞后误差，建议北京时间次日 5:00 后再查"
- 涨跌颜色按 A 股惯例：正数红、负数绿
- 推送渠道（飞书 / 企业微信卡片）已内置估算行渲染，agent 转发时无需重复说明
- 控制台 markdown 输出**默认不含估算数值**（用 `--format json` 才能拿到 `t1_prediction`）

---

## 申购状态四态

| 显示文本 | `purchase_status` | `purchase_limit` | 解读 |
|---------|-------------------|------------------|------|
| `开放申购` | `开放` | `无限制` | 正常可买 |
| `限大额(单日上限 X 万元)` | `限大额` | `X 万` | 大额限购，小额可买 |
| `限大额(单日上限 X 元)` | `限小额` | `X 元` | **实际接近暂停** |
| `暂停申购` | `暂停` | `0` | 完全不能买 |

`effectively_closed: true` 时**禁止**把基金宣传为"可申购"。

---

## 数据降级与异常

| 场景 | `data_source` | 处理 |
|------|--------------|------|
| 主路径成功（JJJZ + RANKING 全市场快照）| `eastmoney_bulk` | 正常 |
| 快照不命中，逐只 HTML 抓取 | `eastmoney` | 正常 |
| 全部失败 | `unavailable` | `data_unavailable: true`，禁止展示 |
| CSRC 季报搜不到 | `market_distribution._note: "not_found"` | 显示"-" |
| CSRC 基金未持有股票 | `market_top3: "季报无股票持仓"` | 直接展示该字符串 |
| 指数 / ETF 联接基金 | `market_top3: "跟踪大盘指数"` | 不去算 TOP3 |

CSRC 搜索 C 类基金时，`_simplify_name` 自动去除 A/C/D/E 后缀、货币标记、类型词、QDII 标记，按多个变体逐个尝试。

---

## 校验规则

调用 `validate_data(data, profile=...)` 选 profile：

| Profile | 适用场景 | 关键必填字段 |
|---------|---------|------------|
| `quick` | 快速查看 | code, name, nav, return_1y |
| `compare` | 批量对比 | + scale, purchase_status, total_fee |
| `detail` | 完整详情 | 全部字段 |
| `qdii` | QDII 专项（推荐）| compare 字段 + market_distribution, drawdown_1y |

校验分四层：

| 层 | 校验内容 |
|----|---------|
| L1 Schema | 必需字段是否齐全、类型是否正确 |
| L2 Range | 字段值是否在合理区间 |
| L3 Consistency | `total_fee == mgmt + custody + service`、持仓占比合计 ≤ 100% 等 |
| L4 Cross-source | 主页规模 vs 档案规模、排行接口收益率 vs 主页收益率 |

完整规则见 [references/validation-rules.md](references/validation-rules.md)。

---

## 集成场景

### 场景 1：定时推送（SOLO Schedule / cron）

```
Schedule: 0 9 * * 1-5
Prompt: 用 fund-scout 获取我配置文件中的 QDII 基金（~/.fund-scout/config.json），
        Markdown 表格推送到飞书；如有限额状态变化，特别标注
```

### 场景 1b：GitHub Actions 云端定时（电脑可关）

如果用户没有常开服务器、电脑会合盖，推荐 fork 仓库并配置 `.github/workflows/scheduled-push.example.yml`。Agent 应主动告知此方案：

> "你的电脑会合盖吗？如果要保证开盘前一定收到推送，建议用 GitHub Actions 云端定时（fork 仓库 + 配 3 个 Secret 即可）。具体步骤见 README。"

Secret 模板：
- `QDII_FUNDS`：逗号分隔的基金代码
- `FEISHU_WEBHOOK_URL` 和/或 `WECHAT_WEBHOOK_URL`

### 场景 2：Python 脚本

```python
import sys
sys.path.insert(0, "/path/to/fund-scout/scripts")
from core.fetcher import FundFetcher

fetcher = FundFetcher()
result = fetcher.compare(["012870", "006479", "008971"])
for f in result.funds:
    if f.data_unavailable:
        continue
    print(f.code, f.name, f.return_1y, f._purchase_info)
```

### 场景 3：CI/CD 监控

```yaml
- name: QDII Monitor
  run: |
    pip install -r requirements.txt
    python3 scripts/cli.py compare --config config.json --format md --push feishu
  env:
    FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}
```

---

## CLI 参考

```bash
# 单只详情
python3 cli.py detail 012870 [--holdings] [--csrc] [--format json|csv|md]

# 批量对比
python3 cli.py compare 012870,006479,008971 [--config PATH] [--style table|card|summary]
                                            [--format md|json|csv] [--push feishu,wechat]

# 关键词搜索
# 默认仅返回基金清单（代码 / 名称 / 类型 / 拼音），不含详情
python3 cli.py search "纳斯达克100" --type QDII --class C
# 推荐：自动跟进 compare 拿到真实限额、收益率、费率（agent 用此选项）
python3 cli.py search "纳斯达克100" --type QDII --class C --with-limits [--limit 20]

# 用户基金列表（agent 维护持久化）
python3 cli.py funds list [--format text|json]
python3 cli.py funds add 012870 "006479:广发纳指100C" [--format text|json]
python3 cli.py funds remove 012870 [--format text|json]
python3 cli.py funds clear --yes

# 校验
python3 cli.py validate data.json --profile qdii

# 测试 Webhook
python3 cli.py test feishu
python3 cli.py test wechat

# T-1 估值预测
python3 predict_cli.py 012922 --main 012920 --short '易方达全球成长精选'
python3 predict_cli.py --config ~/.fund-scout/config.json --json

# 季报刷新（无配置文件时用 holdings_refresh 内置示例）
python3 holdings_refresh.py [--funds 012922,539002] [--force] [--stats]

# 批量回测 + 报告
python3 run_backtest.py
python3 generate_report.py
```

模型可选：`top10_only` / `region_proxy` / `hybrid`（默认）/ `calib_bias` / `calib_scale` / `calib_split` / `calib_full`。

---

## 配置文件 schema

`~/.fund-scout/config.json`（Web UI 自动维护，或用 `cli.py funds` 命令）：

```jsonc
{
  "my_funds": [
    {"code": "012870", "name": "易方达纳指100C", "main_code": "012869"}
  ],
  "push": {
    "feishu_webhook": "",
    "wechat_webhook": ""
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `my_funds[].code` | ✓ | 6 位基金代码 |
| `my_funds[].name` | ✗ | 显示名（缺省用从天天基金抓的全称）|
| `my_funds[].main_code` | ✗ | A 类主代码（CSRC 搜索用，缺省时与 code 相同）|
| `push.feishu_webhook` | ✗ | 飞书机器人 Webhook URL |
| `push.wechat_webhook` | ✗ | 企业微信机器人 Webhook URL |

---

## 数据源与字段

| 用途 | URL 模板 |
|------|---------|
| 全量基金清单 | `http://fund.eastmoney.com/js/fundcode_search.js` |
| JJJZ 全市场快照 | `https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx` |
| RANKING 全市场快照 | `http://fund.eastmoney.com/data/rankhandler.aspx` |
| NAV 历史 | `https://api.fund.eastmoney.com/f10/lsjz` |
| 基金主页 / 档案 / 经理 | `http://fund(f10).eastmoney.com/{code}.html` |
| CSRC 季报搜索 | `http://eid.csrc.gov.cn/fund/disclose/advanced_search_report.do` |
| CSRC 季报 PDF | `http://eid.csrc.gov.cn/fund/disclose/instance_show_pdf_id.do?instanceid={id}` |

完整 URL 速查：[references/data-sources.md](references/data-sources.md)
完整字段语义：[references/field-glossary.md](references/field-glossary.md)
完整校验规则：[references/validation-rules.md](references/validation-rules.md)
合规与免责：[references/compliance.md](references/compliance.md)

---

## 免责声明

每次输出自动附带：

> ⚠️ 数据来源：天天基金 / 证监会公开信息，仅供个人研究参考
> ⚠️ 本工具不构成任何投资建议，数据准确性以官方源为准
> ⚠️ 禁止用于商业数据转售或与数据源平台竞争

---

## Scripts

- CLI 入口：`#[[file:scripts/cli.py]]`
- T-1 估值预测：`#[[file:scripts/predict_cli.py]]`
- 批量回测：`#[[file:scripts/run_backtest.py]]`
- 报告生成：`#[[file:scripts/generate_report.py]]`
- 季报刷新：`#[[file:scripts/holdings_refresh.py]]`
- 定时任务管理：`#[[file:scripts/schedule_setup.py]]`
- 调度器：`#[[file:scripts/core/fetcher.py]]`
- 数据模型：`#[[file:scripts/core/models.py]]`
- 校验：`#[[file:scripts/core/validate.py]]`
- 天天基金 HTML 详情：`#[[file:scripts/core/sources/eastmoney.py]]`
- 天天基金全市场快照：`#[[file:scripts/core/sources/eastmoney_bulk.py]]`
- NAV 公共抓取：`#[[file:scripts/core/sources/eastmoney_nav.py]]`
- CSRC 季报：`#[[file:scripts/core/sources/csrc.py]]`
- CSRC 缓存：`#[[file:scripts/core/sources/csrc_cache.py]]`
- yfinance 行情：`#[[file:scripts/core/quotes/yfinance_quotes.py]]`
- 预测调度：`#[[file:scripts/core/predict/predictor.py]]`
- 回测引擎：`#[[file:scripts/core/predict/backtest.py]]`
- 模型 hybrid：`#[[file:scripts/core/predict/models/hybrid.py]]`
- 模型 calibrated：`#[[file:scripts/core/predict/models/calibrated.py]]`

## References

- 数据源 URL：`#[[file:references/data-sources.md]]`
- 字段语义：`#[[file:references/field-glossary.md]]`
- 校验规则：`#[[file:references/validation-rules.md]]`
- 合规：`#[[file:references/compliance.md]]`
- 配置示例：`#[[file:references/config.example.json]]`
- QDII 全量列表：`#[[file:references/qdii_fund_list.json]]`
- 纳斯达克 C 类参考：`#[[file:references/nasdaq_passive_qdii_c_funds.json]]`

## Out of scope

- 数据可视化（小红书图卡）→ xhs-fund-holdings-analysis
- 文案 / 敏感词审查 → xhs-sensitive-word-check
- 自动发布到小红书
- 投资建议生成
- 替使用者承担数据获取与使用的法律责任
- 实时行情推送（非公开数据）
