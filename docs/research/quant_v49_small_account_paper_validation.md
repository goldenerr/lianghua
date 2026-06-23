# V49 5-10万小账户 Paper Trading / 准生产验证

状态：已进入小账户 paper validation 准备阶段，但不代表生产批准。

## 结论

- `paper_validation_ready=true`
- `paper_evidence_ready=false`
- `production_ready=false`
- 自动交易：关闭
- 实盘下单：禁止
- 实盘资金占用：0

## 候选账户

### paper_small_account_50000

- 资金：50000 元
- 策略：`v33_small_account_small_balanced_blend_50000_5pos`
- 年化收益：3.81%
- Sharpe：0.1407
- 最大回撤：-22.30%
- 胜率：51.07%
- 平均持仓数：4.83
- 平均股票暴露：12.66%
- 小账户可执行门禁：true
- 生产最低绩效门禁：false

### paper_small_account_100000

- 资金：100000 元
- 策略：`v33_small_account_small_balanced_blend_100000_5pos`
- 年化收益：6.73%
- Sharpe：0.4123
- 最大回撤：-22.20%
- 胜率：54.01%
- 平均持仓数：5.3
- 平均股票暴露：21.19%
- 小账户可执行门禁：true
- 生产最低绩效门禁：false

## 文件

- 日报台账：`/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v49_small_account_paper_ledger.json`
- 日报模板：`/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v49_small_account_daily_report_template.json`

## 每日流程

- 只读取真实行情，生成策略信号和模拟订单。
- 模拟订单必须经过同一套风控、整数手、费用、滑点和不可成交状态约束。
- 每天输出 V49/V45 兼容日报，包含收益、成本、滑点、审计 hash、持仓快照和订单成交日志 reference。
- 导入日报后刷新 V42 paper evidence gate，连续 90 个自然日且至少 60 个报告日后再评估。

## 生产阻塞项

- 5-10 万账户当前只允许 paper/pre-production validation，不允许实盘生产。
- 小账户股票 alpha 回测未达到生产最低绩效门槛。
- 缺 broker/provider/WORM/approval/position/borrow/capacity/DR 外部 evidence refs。
- 缺真实 PIT/vendor corporate action、真实交易状态和真实 tick/minute 执行数据复核。
- 缺连续 90 天 paper trading 日报、订单成交日志、持仓快照和审计归档。

## 严禁事项

- 严禁开启自动交易。
- 严禁连接真实下单权限。
- 严禁把模板、示例或本地 mock reference 当作真实证据。
- 严禁在未完成 V30/V42/V48/V44 前把任何字段改成 `production_ready=true`。
