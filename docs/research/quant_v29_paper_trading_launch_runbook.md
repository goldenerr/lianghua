# V29 模拟盘启动运行手册

状态：由启动包生成，不代表实盘交易批准。

## 候选策略

- 候选：`v29_price_meta_longhorizon_guard`
- 模拟盘启动就绪：`False`
- 生产就绪：`False`
- 全样本 Sharpe：`2.0285`
- OOS Sharpe: `2.4013`
- 最低 OOS Sharpe：`0.198`
- 最大回撤：`-0.0833`

## 模拟盘启动前必须具备的证据

- external_worm_archive
- secret_manager
- approval_service
- real_market_data_provider
- provider_entitlement
- exchange_position_provider
- signed_plugin_review
- production_calendar
- capacity_benchmark

## 当前模拟盘启动阻塞项

- external_worm_archive: 缺少外部不可变 WORM 归档证明
- secret_manager: 缺少已批准的生产 Secret Manager 解析器证明
- approval_service: 缺少风控/配置审批服务证明
- real_market_data_provider: 缺少已批准的非 mock 行情供应商证明
- provider_entitlement: 缺少数据供应商授权、许可和再分发证明
- exchange_position_provider: 缺少交易所/券商背书的对账持仓服务证明
- signed_plugin_review: 缺少签名插件加载和代码审查证明
- production_calendar: 缺少已批准的生产交易所日历证明
- capacity_benchmark: 缺少类生产环境容量压测报告

## 当前完整生产阻塞项

- external_worm_archive: 缺少外部不可变 WORM 归档证明
- secret_manager: 缺少已批准的生产 Secret Manager 解析器证明
- approval_service: 缺少风控/配置审批服务证明
- real_market_data_provider: 缺少已批准的非 mock 行情供应商证明
- provider_entitlement: 缺少数据供应商授权、许可和再分发证明
- alt_archive_readiness: 缺少另类数据归档完整性 artifact 证明
- exchange_position_provider: 缺少交易所/券商背书的对账持仓服务证明
- broker_borrow_availability: 缺少券商背书的借券可用性数据源和账户绑定证明
- paper_trading_90d: 缺少使用真实行情完成的三个月模拟盘门禁证明
- signed_plugin_review: 缺少签名插件加载和代码审查证明
- production_calendar: 缺少已批准的生产交易所日历证明
- rollover_runbook: 缺少已批准的期货换月执行操作手册/证明
- capacity_benchmark: 缺少类生产环境容量压测报告
- failover_dr_test: 缺少多区域故障切换和灾备演练报告

## 操作环境引用

- `QUANT_PRODUCTION_EVIDENCE_FILE`：外部证据引用 JSON 的路径
- `QUANT_SECRET_MANAGER_URL`：Secret Manager 服务端点，不是 token 内容
- `QUANT_SECRET_MANAGER_TOKEN_REF`：Secret Manager token 引用，绝不能填写 token 值
- `QUANT_APPROVAL_SERVICE_URL`：风控审批服务端点
- `QUANT_POSITION_PROVIDER_URL`：交易所/券商对账持仓服务端点
- `QUANT_MARKET_DATA_PROVIDER_REF`：已批准的数据供应商授权引用

## 控制要求

- 90 天模拟盘阶段不得开启实盘下单。
- 不得在本运行手册或证据 JSON 中放入 token、API Key 或 passphrase。
- 任何缺失或占位证据引用都必须视为硬阻塞项。
- 数据或代码发生实质变化后，必须重新运行策略 readiness 和鲁棒性 gate。

## 下一步

- 从已批准的 WORM、Secret Manager、审批服务、供应商授权和券商服务中填写外部证据引用。
- 启动 90 天模拟盘服务前，必须使用 --require-paper-launch-ready 重新运行本脚本。
- 模拟盘服务必须使用真实行情、动态成本和审计归档，连续运行至少 90 天。
- 只有模拟盘证据存在后，才允许重新运行完整生产证据门禁和部署门禁。
