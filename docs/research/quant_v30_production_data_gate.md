# V30 生产级历史数据与执行准入门禁

## 结论

V30 门禁已经落地为可复跑脚本，但当前生产数据仍未就绪。

脚本：

`scripts/validate_v30_production_data_gate.py`

当前输出：

`data/backtest_results/quant_v30_production_data_gate.json`

`data/backtest_results/quant_v30_production_data_gate.csv`

当前结果：

| 指标 | 结果 |
| --- | ---: |
| 总要求数 | 5 |
| 本地覆盖通过数 | 0 |
| 外部 evidence 通过数 | 0 |
| 生产通过数 | 0 |
| 当前 blocker 数 | 18 |

这不是策略失败，而是生产级数据证据不足。V29 的日线研究结果可以继续用于模拟盘准备，但不能替代以下生产准入要求。

## 五项硬性要求

| 要求 | 默认本地文件 | 外部 evidence key | 当前状态 |
| --- | --- | --- | --- |
| 真正 PIT 历史股票池 | `data/security_master/pit_universe.parquet` | `pit_security_master` | 缺失 |
| 退市/ST/停牌/涨跌停可交易状态 | `data/security_master/trading_status.parquet` | `trading_status_actions` | 缺失 |
| vendor corporate action/复权复核 | `data/corporate_actions/vendor_reconciliation.parquet` | `corporate_action_reconciliation` | 缺失 |
| 二十年真实 tick/minute 执行历史 | `data/execution_history/tick_minute_20y.parquet` | `real_tick_minute_execution_feed` | 缺失 |
| 交易所/券商订单成交回放 | `data/execution_history/order_fill_replay.parquet` | `exchange_order_fill_replay` | 缺失 |

## 必要字段

### PIT 历史股票池

文件：`data/security_master/pit_universe.parquet`

必要字段：

`date`, `code`, `name`, `list_date`, `delist_date`, `is_listed`, `is_tradable`, `is_large_cap`, `source`, `asof_date`

用途：

按每个历史交易日还原当时可交易股票池，避免把 2026 年仍存在的股票回推到 2006 年造成幸存者偏差。

### 交易状态历史

文件：`data/security_master/trading_status.parquet`

必要字段：

`date`, `code`, `is_st`, `is_suspended`, `is_limit_up`, `is_limit_down`, `is_tradable`, `price_limit_pct`, `source`, `asof_date`

用途：

约束回测成交。停牌、ST、涨停无法买入、跌停无法卖出等状态必须进入执行模拟，否则回测收益会虚高。

### 复权复核

文件：`data/corporate_actions/vendor_reconciliation.parquet`

必要字段：

`date`, `code`, `action_type`, `vendor_adjust_factor`, `local_adjust_factor`, `adjustment_diff_bps`, `source`, `evidence_ref`

用途：

证明回测与实盘使用同一套 corporate action 调整逻辑。当前本地日线价格抽样 200 个文件没有发现 `adj_factor`、`qfq_factor`、`hfq_factor`、`adjusted_close` 等复权字段，因此不能声明已完成 vendor 复权复核。

### 真实 tick/minute 执行历史

文件：`data/execution_history/tick_minute_20y.parquet`

必要字段：

`timestamp`, `code`, `price`, `volume`, `bid`, `ask`, `trade_count`, `source`, `asof_date`

用途：

验证滑点、成交率、排队、冲击成本和订单拆分算法。当前 `data/intraday/microstructure_features.parquet` 只有 257 个交易日、2475 个实体的近期分钟特征，不能替代二十年真实 tick/minute 执行历史。

### 订单/成交回放

文件：`data/execution_history/order_fill_replay.parquet`

必要字段：

`timestamp`, `account_id`, `client_order_id`, `exchange_order_id`, `code`, `side`, `order_qty`, `fill_qty`, `fill_price`, `source`, `evidence_ref`

用途：

用券商或交易所返回的真实订单与成交记录回放系统决策，验证 OrderManager、RiskEngine、PositionReconciler 与交易所结果一致。

## 外部 evidence refs

模板文件：

`config/production_data_evidence.v30.template.json`

可选本地文件：

`config/production_data_evidence.v30.local.json`

可选环境变量：

`QUANT_EVIDENCE_PIT_SECURITY_MASTER_REF`

`QUANT_EVIDENCE_TRADING_STATUS_ACTIONS_REF`

`QUANT_EVIDENCE_CORPORATE_ACTION_RECONCILIATION_REF`

`QUANT_EVIDENCE_REAL_TICK_MINUTE_EXECUTION_FEED_REF`

`QUANT_EVIDENCE_EXCHANGE_ORDER_FILL_REPLAY_REF`

这些变量只能填写外部审计引用，例如 `security-master://...`、`vendor-feed://...`、`minute-feed://...`、`broker-fill://...`。禁止填写 token、API key、账号密码，也禁止使用 `local://`、`mock://`、`todo`、`pending` 等占位值。

## 当前联动门禁

V30 gate 已接入：

`scripts/score_strategy_readiness_v27.py`

刷新后的总 readiness 仍为：

`production_ready=false`

当前顶层 blocker：

1. 外部生产 evidence gate 未就绪。
2. V29 approved paper-trading launch package 未就绪。
3. V30 生产级历史数据与执行门禁未就绪。

## 后续执行顺序

1. 从真实供应商或交易所数据源导入 PIT security master。
2. 导入交易状态历史，包括退市、ST、停牌、涨跌停和可成交状态。
3. 导入 vendor corporate action/复权因子，并与本地价格序列做差异复核。
4. 导入二十年 tick/minute 或至少分钟级订单簿/成交数据。
5. 导入券商/交易所订单成交日志，完成历史订单回放。
6. 填写外部 evidence refs。
7. 重新运行 `scripts/validate_v30_production_data_gate.py --require-production-data-ready`。
8. 重新运行 `scripts/score_strategy_readiness_v27.py`。

只有以上全部通过，二十年历史验证才可以从“诊断”升级为“生产级证据”。
