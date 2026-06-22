# V29 动态袖口组合层研究结论

日期：2026-06-15
状态：仅研究用途，生产仍被外部证据 gate 阻塞。

## 摘要

V29 不再继续微调 V5/V28 参数，而是引入动态组合构建层。该层组合多个只使用滞后价格信息的股票袖口，并叠加剩余危机/Carry 底仓；袖口权重只基于历史绩效、回撤和相关性信息分配。

V29 现在已经出现同时满足生产最低研究门槛和行业领先研究指标的候选策略。这是相对 V28 的重大进展，但不是生产批准。上线前仍必须补齐外部 WORM、供应商授权、Secret Manager、审批服务、真实模拟盘和运维证据。

## 数据范围

- 股票池：`data/stock_list_provider_qualified_non_largecap_2000_v27.json`
- 已加载股票数：1,994
- 日期数：2,281
- 行业分类覆盖：1,994
- 最优/推荐候选均为 price-only，不使用已归档/快照类另类数据面板，也不使用合成借券或卖空。
- 生产环境仍必须通过外部 evidence gate 提供供应商授权证据。

## 候选结果

最高分候选：

| 字段 | 数值 |
| --- | --- |
| 名称 | `v29_price_meta_lowvol_small_carry_floor` |
| 全样本 Sharpe | 2.2668 |
| 年化收益 | 22.09% |
| 年化波动 | 8.64% |
| 最大回撤 | -9.82% |
| 胜率 | 57.31% |
| 平均 OOS Sharpe | 2.4573 |
| 最低 OOS Sharpe | 0.0266 |
| Sharpe 衰减 | 0.0046 |

鲁棒性推荐候选：

| 字段 | 数值 |
| --- | --- |
| 名称 | `v29_price_meta_lowvol_recent_guard` |
| 全样本 Sharpe | 2.2568 |
| 年化收益 | 20.52% |
| 年化波动 | 7.98% |
| 最大回撤 | -9.63% |
| 胜率 | 57.26% |
| 平均 OOS Sharpe | 2.4867 |
| 最低 OOS Sharpe | 0.1770 |
| Sharpe 衰减 | -0.0004 |

建议下一阶段 paper trading 优先使用 `v29_price_meta_lowvol_recent_guard`。它的原始收益略低，但 OOS 地板和回撤安全边际更好。

## 鲁棒性 Gate

鲁棒性文件：`data/backtest_results/quant_v29_robustness.json`

| 检查项 | 结果 |
| --- | --- |
| 候选 | `v29_price_meta_lowvol_recent_guard` |
| 用例数 | 21 |
| 鲁棒性 gate | 通过 |
| 基准全样本 Sharpe | 2.2568 |
| 基准 OOS Sharpe | 2.4867 |
| 基准最大回撤 | -9.63% |
| 基准最低 OOS Sharpe | 0.1770 |
| 敏感性压力下最低全样本 Sharpe | 2.1447 |
| 敏感性压力下最低 OOS Sharpe | 2.3938 |
| 敏感性压力下最大回撤 | -10.19% |
| 成本压力下最低全样本 Sharpe | 2.0856 |
| 成本压力下最低 OOS Sharpe | 2.3054 |
| 近 90 日 Sharpe | 0.9009 |
| 近 252 日 Sharpe | 2.8892 |

注意：部分压力场景不再满足更严格的“行业领先”定义，但仍高于生产风格阈值。最近 90 日本地切片为正但弱于全周期表现，因此真实 paper trading 证据仍是必要条件。

## 生产阻塞项

当前 readiness 文件：`data/backtest_results/quant_v27_strategy_readiness.json`

- `production_ready=false`
- `strategy_gate_passes=true`
- `industry_leading_candidate_exists=true`
- 剩余阻塞项：外部生产 evidence gate 尚未 ready。

仍缺的外部证据包括 WORM 归档、Secret Manager、审批服务、真实行情供应商、供应商授权、交易所/券商背书持仓服务、90 天模拟盘、签名插件证据、生产日历、容量压测和故障切换/灾备证据。

## 下一步

下一步不应继续做本地指标微调，而应接入外部证据，并使用批准的行情数据、审计归档和持仓对账证据，为推荐的 V29 候选运行真实 90 天 paper trading。
