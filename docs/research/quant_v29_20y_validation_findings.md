# V29 二十年长周期验证说明

## 结论

应该做二十年验证，但当前只能作为“长周期诊断”，不能作为“二十年全量生产验证”对外声称。

原因是当前 V27/V29 使用的是近期构建的 2000 只非大盘股候选池。把这个候选池回推到 2006 年，会天然带来幸存者偏差、上市时间不一致和早期横截面不足问题。真正可生产的二十年验证，需要按历史日期还原当时可交易股票池，并包含退市、暂停上市、ST、复权、行业分类和可交易性状态。

## 数据覆盖审计

审计脚本：

`scripts/audit_v29_20y_data_coverage.py`

输出文件：

`data/backtest_results/quant_v29_20y_data_coverage_audit.json`

`data/backtest_results/quant_v29_20y_symbol_coverage.csv`

`data/backtest_results/quant_v29_20y_daily_cross_section.csv`

审计窗口：2006-01-01 至 2026-05-29。

关键结果：

| 指标 | 结果 |
| --- | ---: |
| 目标股票数 | 2000 |
| 成功加载日线 parquet | 1994 |
| 覆盖完整请求窗口的股票数 | 566 |
| 严格二十年质量合格股票数 | 519 |
| 严格质量合格比例 | 25.95% |
| warm-up 后达到 500 只可交易股票日期 | 2013-07-02 |
| warm-up 后达到 1000 只可交易股票日期 | 2018-11-28 |
| warm-up 后达到 1900 只可交易股票日期 | 2024-06-03 |

因此，2006 起跑的结果可以帮助观察长周期稳定性，但不能替代 PIT 历史股票池上的正式生产门禁。

## 长周期诊断结果

输出文件：

`data/backtest_results/quant_logic_research_v29_portfolio_layer_20y_diagnostic.json`

`data/backtest_results/quant_logic_research_v29_portfolio_layer_20y_diagnostic.csv`

该诊断从 2006-01-01 启动，覆盖 4954 个交易日；由于早期横截面不足，结果必须标记为 diagnostic。

| 候选 | 交易日 | 年化收益 | 年化波动 | Sharpe | 最大回撤 | 胜率 | OOS Sharpe | Sharpe 衰减 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v29_price_meta_balanced_carry_floor | 4512 | 18.02% | 7.29% | 2.1286 | -8.56% | 56.25% | 2.4620 | -8.98% |
| v29_price_meta_lowvol_recent_guard | 4575 | 15.25% | 6.47% | 1.9686 | -9.49% | 56.59% | 2.3979 | -6.59% |

## 覆盖阈值诊断结果

输出文件：

`data/backtest_results/quant_logic_research_v29_portfolio_layer_coverage500_diagnostic.json`

`data/backtest_results/quant_logic_research_v29_portfolio_layer_coverage500_diagnostic.csv`

该诊断从 2013-07-02 启动，也就是审计确认 warm-up 后至少 500 只股票可交易的起点。

| 候选 | 交易日 | 年化收益 | 年化波动 | Sharpe | 最大回撤 | 胜率 | OOS Sharpe | Sharpe 衰减 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v29_price_meta_lowvol_carry_floor | 2759 | 16.22% | 6.38% | 2.1507 | -5.89% | 57.01% | 2.4698 | -7.06% |
| v29_price_meta_lowvol_recent_guard | 2759 | 16.66% | 7.15% | 1.9804 | -9.56% | 57.01% | 2.3550 | -2.99% |

## 与原 V29 结果的关系

原 V29 主结果从 2017-01-01 到 2026-05-29，覆盖 2281 个交易日。推荐候选 `v29_price_meta_lowvol_recent_guard` 的主口径结果为 Sharpe 2.2568、年化收益 20.52%、最大回撤 -9.63%、OOS Sharpe 2.4867。

长周期诊断显示：加入更长历史后，收益和 Sharpe 有所下降，但仍明显高于生产最低门槛；最大回撤仍控制在 10% 以内。这个结果支持“继续推进 V29 进入模拟盘准备”的研究判断，但不解除外部 evidence、PIT 数据、真实 provider、paper trading 和生产准入 blocker。

## 下一步

1. 构建真正的 PIT 历史 A 股非大盘 universe，按每个交易日还原当时可交易股票池。
2. 纳入退市股、ST 状态、停牌状态、上市未满 N 日过滤、涨跌停不可成交约束。
3. 使用同一套 corporate action 调整数据重跑 2006-2026 walk-forward、鲁棒性和成本压力测试。
4. 将覆盖审计接入 readiness gate：若 `can_claim_20y_full_universe_production_validation=false`，禁止把二十年结果标记为生产级证据。
5. 在真实 provider entitlement 和 WORM 归档到位后，重新生成二十年验证包并进入 90 天模拟盘。
