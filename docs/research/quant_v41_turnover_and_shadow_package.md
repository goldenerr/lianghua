# V41 降换手与 Paper-Shadow 启动包

日期：2026-06-23  
状态：降换手方案拒绝，paper-shadow 包已生成但未就绪，生产仍阻塞。

## 研究目标

V40 的 20 万元、股票 7%、危机 20%、40 日调仓候选在默认和高成本单点均相对通过，但 robustness 失败。V41 验证两个问题：

1. 延长股票调仓周期到 80/120 日，是否能降低交易费用并改善高成本稳健性；
2. 能否把 V40 固定候选打包成只读 paper-shadow 启动包，同时保持所有生产门禁关闭。

## 代码变更

- `scripts/research_v40_stock_alpha_defensive_basket.py` 增加 `--stock-rebalance-freqs`，默认仍为 40 日，兼容原 V40。
- 股票子策略、股票 gate return 和共享账户执行均使用同一调仓周期。
- CSV 和 JSON 输出记录 `stock_rebalance_freq`。
- 新增 `scripts/prepare_v40_paper_shadow_package.py`，读取 V40 research 与 robustness，生成 fail-closed 启动包和中文运行手册。

## 降换手默认成本结果

扫描范围：

- 资金：20 万；
- 股票上限：6.3%、7.0%；
- 危机上限：20%、22%；
- 股票调仓周期：40/80/120 日；
- 场景数：12。

结果：

- 股票执行：12/12；
- 相对改善：6/12；
- 生产最低门禁：0/12；
- V39 基线逐日一致。

默认成本下，40 日仍最好：

| 危机 | 股票 | 调仓 | 年化 | 年化增量 | Sharpe | MDD | 最低 OOS | 总费用 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20% | 6.3% | 40 日 | 6.40% | +0.92% | 0.4786 | -14.35% | -0.1465 | 6,587 |
| 20% | 6.3% | 80 日 | 6.08% | +0.60% | 0.4372 | -14.54% | -0.3055 | 5,979 |
| 20% | 6.3% | 120 日 | 5.53% | +0.05% | 0.3860 | -13.48% | -0.4303 | 5,519 |
| 20% | 7.0% | 40 日 | 6.43% | +0.95% | 0.4807 | -14.40% | -0.1190 | 6,589 |
| 20% | 7.0% | 80 日 | 6.10% | +0.62% | 0.4384 | -14.49% | -0.3059 | 6,033 |
| 20% | 7.0% | 120 日 | 4.78% | -0.70% | 0.3185 | -13.45% | -6.9448 | 3,880 |

80 日虽然降低约 8% 至 9% 费用，但收益、Sharpe 和最低 OOS 明显变差。120 日在默认成本下出现 OOS 崩塌，不可采用。

## 高成本结果

将股票与 ETF 滑点同时提高到 10bps 后，同一 12 个场景中 9 个相对通过。

高成本下 120 日调仓出现更好单点：20 万、股票 6.3%、危机 20%、120 日，年化 6.45%、Sharpe 0.4956、MDD -13.41%、最低 OOS -0.3482。

但该 120 日候选在默认成本下不通过，仅在高成本路径下成立，属于路径依赖，不能替换 V40 主候选。能够默认和高成本同时通过的稳定候选仍以 40 日为主；80 日只在部分组合通过，但整体收益和 OOS 均弱于 40 日。

## Paper-Shadow 包

新增输出：

- `data/backtest_results/quant_v40_paper_shadow_package.json`
- `docs/research/quant_v40_paper_shadow_runbook.md`

固定候选：

- 资金：20 万；
- 股票上限：7%；
- 危机上限：20%；
- 股票调仓：40 日；
- 危机资产：黄金 ETF 518880、国债 ETF 511010、十年国债 ETF 511260；
- `auto_trade_enabled=false`；
- `live_order_submission_allowed=false`；
- `max_live_capital_fraction=0.0`。

启动包状态：

- `shadow_candidate_exists=true`；
- `paper_shadow_ready=false`；
- `production_ready=false`；
- 阻塞项 14 个。

主要阻塞项包括：V40 robustness 失败、外部 WORM、Secret Manager、Approval Service、真实 provider entitlement、exchange-backed position provider、broker order/fill 日志、90 天 paper 计划、生产日历、容量/DR 证据。

## 结论

V41 拒绝“单纯延长调仓周期”作为优化方向。40 日调仓虽然费用较高，但在默认成本与高成本之间更一致；80/120 日降低费用的同时破坏收益和 OOS，不能作为生产改进。

V40 20 万 7%/20%/40 日候选可以进入只读 paper-shadow 准备，但启动包仍 fail-closed。下一步如果继续研究，应优先做真实 paper-shadow 接入前置：外部 evidence ref、只读行情、模拟撮合审计、每日滑点/成本报告和周度 backtest-vs-paper 漂移报告，而不是继续调参数。

## 输出文件

- `data/backtest_results/quant_v41_turnover_cooldown.json/csv`
- `data/backtest_results/quant_v41_turnover_cooldown_cost_stress.json/csv`
- `data/backtest_results/quant_v40_paper_shadow_package.json`
- `docs/research/quant_v40_paper_shadow_runbook.md`
- `docs/research/quant_v41_turnover_and_shadow_package.md`
- `scripts/prepare_v40_paper_shadow_package.py`
- `tests/system/test_v40_paper_shadow_package.py`
