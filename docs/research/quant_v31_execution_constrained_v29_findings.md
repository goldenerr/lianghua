# V31 交易状态约束下的 V29 回测结论

## 结论

V31 免费交易状态近似表接入 V29 后，策略收益略有下降，但仍保持研究口径下的行业领先水平。更重要的是，当前推荐候选已经从“无成交约束版本”切换为“V31 交易状态约束 + robustness 通过版本”。

当前推荐候选：

```text
v29_price_meta_lowvol_recent_guard
```

推荐理由：

```text
selected by V31 execution-constrained robustness gate
```

## 新增执行约束

脚本：[research_v29_portfolio_layer.py](/Users/jacklondon/Documents/study/lianghua/scripts/research_v29_portfolio_layer.py)

新增参数：

```bash
--trading-status-path data/security_master/free_pit_approx/trading_status_v31.parquet
--require-trading-status
```

执行规则：

- 买入或加仓时，如果下一交易日 `is_tradable=false` 或 `is_limit_up=true`，则不执行该买入。
- 卖出或减仓时，如果下一交易日 `is_tradable=false` 或 `is_limit_down=true`，则保留旧仓位。
- 如果卖不出去导致资金不足，不允许凭空加杠杆，未能买入的部分保留为现金。
- 该约束是研究级压力测试，不是生产交易所状态表。

## 主回测结果

输出：

- `data/backtest_results/quant_logic_research_v29_portfolio_layer_v31_execution_constrained.json`
- `data/backtest_results/quant_logic_research_v29_portfolio_layer_v31_execution_constrained.csv`

最佳收益候选：

| 候选 | Sharpe | 年化收益 | MDD | OOS Sharpe |
| --- | ---: | ---: | ---: | ---: |
| `v29_price_meta_balanced_carry_floor` | 2.4267 | 22.23% | -5.30% | 2.4991 |

推荐稳健候选：

| 候选 | Sharpe | 年化收益 | MDD | OOS Sharpe | 最低 OOS Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: |
| `v29_price_meta_lowvol_recent_guard` | 2.1838 | 19.82% | -9.82% | 2.4237 | 0.1607 |

与旧无约束主回测相比：

| 口径 | Sharpe | MDD | OOS Sharpe |
| --- | ---: | ---: | ---: |
| V29 无约束最佳 | 2.5173 | -5.08% | 2.5686 |
| V29 + V31 约束最佳 | 2.4267 | -5.30% | 2.4991 |

解释：

- 交易状态约束后，收益和 OOS 有小幅下降，属于预期。
- MDD 基本稳定，未出现“成交约束一加就失效”的情况。
- 推荐候选仍满足行业领先研究门槛。

## 约束影响统计

V31 状态矩阵加载情况：

```text
rows_loaded=3,866,705
unique_dates=2,281
unique_symbols=1,994
```

四个 stock sleeve 的约束影响：

| sleeve | 受约束再平衡次数 | 阻断买入笔数 | 阻断卖出笔数 | 平均现金 |
| --- | ---: | ---: | ---: | ---: |
| `price_ic_alpha` | 98 | 146 | 113 | 0.84% |
| `price_ic_diversified` | 60 | 105 | 75 | 0.69% |
| `price_lowvol_reversal` | 101 | 161 | 102 | 0.85% |
| `price_defensive_breadth` | 62 | 97 | 86 | 0.44% |

## 约束版鲁棒性

脚本：[validate_v29_robustness.py](/Users/jacklondon/Documents/study/lianghua/scripts/validate_v29_robustness.py)

输出：

- `data/backtest_results/quant_v29_robustness_v31_execution_constrained.json`
- `data/backtest_results/quant_v29_robustness_v31_execution_constrained_cases.csv`

结果：

```text
robustness_gate_passes=true
case_count=21
base_full_sharpe=2.1838
base_oos_sharpe=2.4237
base_max_drawdown=-0.0982
min_sensitivity_full_sharpe=2.0724
min_sensitivity_oos_sharpe=2.3301
min_sensitivity_drawdown=-0.1037
min_cost_stress_full_sharpe=2.0155
min_cost_stress_oos_sharpe=2.2416
recent_90_sharpe=0.8937
recent_252_sharpe=2.8458
```

说明：

- 约束版 robustness gate 通过。
- 21 个 case 中有部分 case 不满足更苛刻的“行业领先”定义，但全部满足当前 robustness gate 的生产前研究阈值。
- 这仍是本地研究证据，不是实盘批准。

## Readiness 变化

总 readiness 已更新：

- `recommended_candidate`：`v29_price_meta_lowvol_recent_guard`
- `recommended_candidate.version`：`V29-dynamic-sleeve-portfolio-layer-v31-execution-constrained`
- `recommended_candidate_reason`：`selected by V31 execution-constrained robustness gate`
- `production_ready=false`

剩余顶层 blocker：

- 外部生产 evidence gate 未就绪。
- V29 approved paper-trading launch package 未就绪。
- V30 production historical data/execution gate 未就绪。

## 生产判断

这一步提升了研究真实性，但不能解除生产 blocker。

不能解除的原因：

- V31 交易状态来自免费源/本地日线近似，不是真正交易所或 vendor 历史状态。
- ST 状态仍未知。
- tick/minute、订单簿、真实订单成交回放仍缺。
- 模拟盘仍未完成 90 天真实行情和审批证据。
- 外部 WORM、Secret Manager、Approval Service、Provider Entitlement、Broker Position Provider、Capacity/DR evidence refs 仍未提供。

下一步最有效的本地工作：

```text
把 V31 约束版本作为唯一研究候选口径，继续补 20 年长周期约束回测和 paper launch evidence 结构。
```
