# V44 生产准入总门禁

状态：总门禁已生成，当前不允许进入生产。

## 结论

- `production_ready=false`
- 候选包通过：false
- 90 天影子模拟盘证据通过：false
- 本地容量/容灾门禁通过：true
- 外部生产证据通过：false
- 总阻塞项数量：20

## 候选配置

- `capital`：200000.0
- `stock_alpha_cap`：0.07
- `crisis_cap`：0.2
- `stock_rebalance_freq`：40
- `auto_trade_enabled`：False
- `live_order_submission_allowed`：False
- `max_live_capital_fraction`：0.0

## 候选包阻塞项

- V40 影子模拟盘包未就绪
- V40 影子模拟盘包仍有 14 个阻塞项

## 90 天影子模拟盘阻塞项

- V42 影子模拟盘 90 天证据门禁未通过
- V42: 影子模拟盘包仍有 14 个阻塞项
- V42: 缺少每日影子模拟盘报告
- V42: 缺少可信 paper_trading_90d 证据引用

## 容量与容灾阻塞项

- 无

## 外部生产证据阻塞项

- external_worm_archive: 缺少外部不可变 WORM 归档证明
- secret_manager: 缺少已批准的生产 Secret Manager 证明
- approval_service: 缺少风控/配置审批服务证明
- real_market_data_provider: 缺少已批准的非 mock 行情供应商证明
- provider_entitlement: 缺少数据供应商授权、许可和再分发证明
- alt_archive_readiness: 缺少另类数据归档完整性证明
- exchange_position_provider: 缺少交易所/券商背书的对账持仓服务证明
- broker_borrow_availability: 缺少券商背书的借券可用性和账户绑定证明
- paper_trading_90d: 缺少使用真实行情完成的三个月模拟盘证明
- signed_plugin_review: 缺少签名插件加载与代码审查证明
- production_calendar: 缺少已批准的生产交易所日历证明
- rollover_runbook: 缺少已批准的期货换月执行手册/证明
- capacity_benchmark: 缺少类生产环境容量压测报告
- failover_dr_test: 缺少多区域故障切换和灾备演练报告

## 下一步

- 修复 V40 robustness 与候选包 blocker。
- 接入真实只读行情、模拟成交、每日审计归档和券商/交易所持仓快照，连续生成至少 90 天 V42 日报。
- 提供外部 WORM、Secret Manager、Approval Service、真实 provider、position provider、broker logs、生产日历、签名插件、容量压测和 DR 演练 refs。
- 所有外部 refs 到位后重新运行本脚本，并继续保持实盘下单关闭，直到风控审批和生产 gate 全部通过。

## 说明

- V44 只是总门禁，不会启动实盘交易。
- 外部证据齐全只是必要条件；策略 robustness、90 天模拟盘、容量/容灾和审批仍必须全部通过。
- 不得把本地报告、mock、pending 或 todo 引用填入生产证据。
