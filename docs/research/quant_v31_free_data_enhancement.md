# V31 免费数据增强方案

## 结论

V31 新增的是“免费/公开源研究增强层”，不是生产级 vendor 数据替代品。

它可以帮助我们把 20 年回测从“只有幸存日线价格”推进到“带可观察上市区间、停牌近似、涨跌停近似、复权列审计”的更严格研究口径；但它不能解除 V30 的生产 blocker。生产上线仍需要真实 PIT 主数据、交易状态历史、vendor corporate action reconciliation、tick/minute 执行历史和券商订单成交回放。

## 新增产物

运行构建脚本：

```bash
.venv/bin/python scripts/build_v31_free_data_pit_approximation.py
```

默认输出：

- `data/security_master/free_pit_approx/pit_universe_v31.parquet`
- `data/security_master/free_pit_approx/trading_status_v31.parquet`
- `data/corporate_actions/free_reconciliation_v31.parquet`
- `data/backtest_results/quant_v31_free_data_build_report.json`
- `data/backtest_results/quant_v31_free_data_build_report.csv`

运行质量门禁：

```bash
.venv/bin/python scripts/validate_v31_free_data_quality_gate.py
```

默认输出：

- `data/backtest_results/quant_v31_free_data_quality_gate.json`
- `data/backtest_results/quant_v31_free_data_quality_gate.csv`

总 readiness scorecard 已接入 V31 摘要：

```bash
.venv/bin/python scripts/score_strategy_readiness_v27.py
```

输出字段：

- `v31_free_data_quality_gate_summary.free_research_ready`
- `v31_free_data_quality_gate_summary.production_data_ready`
- `v31_free_data_quality_gate_summary.research_blockers`
- `v31_free_data_quality_gate_summary.production_blockers`

## V31 能补上的问题

- 从本地日线 parquet 反推每只股票在研究窗口内的可观察上市区间。
- 生成日级 `is_listed`、`is_tradable`、`is_suspended` 字段，减少停牌/无成交日被误当成可成交的风险。
- 按 A 股板块规则近似生成 `is_limit_up`、`is_limit_down`、`price_limit_pct`，让回测能过滤涨跌停不可成交情形。
- 审计本地日线文件是否包含 `adj_factor`、`qfq_factor`、`hfq_factor`、`close_qfq` 等复权字段。
- 将免费数据研究就绪状态接入 readiness scorecard，避免凭感觉判断数据覆盖。

## V31 不能补上的问题

- 不是真正 PIT security master。上市、退市和可交易状态来自本地可见价格区间推断，仍可能有幸存者偏差。
- 不能可靠还原历史 ST 状态。输出中 `is_st_known=false`，明确表示 ST 口径未知。
- 停牌状态只是由交易日缺 bar 或成交量为 0 近似推断，不能替代交易所状态历史。
- 涨跌停规则是近似规则，不覆盖所有历史规则、ST 5% 特殊规则和特殊处理公告。
- 公司行动只是本地列审计，没有 vendor 复权因子和 corporate action reconciliation。
- 没有真实 tick/minute、订单簿、券商订单成交日志和成交回放。
- 不能解除 V30 production-data gate，也不能让 `production_ready=true`。

## 使用建议

短期研究：

- 将 `trading_status_v31.parquet` 接入研究回测，买入时过滤 `is_tradable=false`、`is_limit_up=true`，卖出时过滤 `is_tradable=false`、`is_limit_down=true`。
- 将 `pit_universe_v31.parquet` 用作研究 universe 的日期对齐层，避免在股票尚无本地可见历史时参与选股。
- 重新跑 V29/V30 候选策略，观察加入成交约束后 Sharpe、回撤、换手和容量是否显著恶化。

中期免费路线：

- 每日归档 AKShare、交易所公告、行情快照、行业分类、ST/名称变更、停复牌通知，生成本地 WORM manifest 和 hash。
- 逐日积累之后，可以形成“从今天开始的真实快照 PIT”，但不能自动补齐过去 20 年。

生产路线：

- 仍需导入真实 vendor/broker 文件和外部 evidence refs。
- V30 的五类生产证据必须独立满足，V31 只能作为研究辅助证据。

## 生产判断

V31 通过时只能说明：

```text
free_research_ready=true
production_data_ready=false
```

这代表“可以更严肃地继续研究和重跑回测”，不代表“可以上生产”。
