# V29 外部证据引用填写指南

状态：操作指南，不代表生产批准。

## 基本原则

外部证据引用必须来自本仓库之外的受信系统。本地文件、mock 端点、待办工单、占位字符串都不能作为生产证据。采集器只负责汇总和校验引用格式，不负责创造信任来源。

## 启动 Paper Trading 前必须提供的证据

V29 price-only 候选策略在启动经批准的 90 天模拟盘服务前，必须先提供以下外部引用：

- `external_worm_archive`：不可变审计/归档证明，例如 `worm://...` 或 `s3-object-lock://...`。
- `secret_manager`：已批准的密钥解析器或账户密钥引用，例如 `vault://...`、`aws-sm://...` 或 `sealed://...`。
- `approval_service`：绑定已审阅配置 hash 的风控/配置审批引用，例如 `approval://...` 或 `git-pr://...`。
- `real_market_data_provider`：非 mock 的实时行情或供应商数据源证明，例如 `provider://...` 或 `vendor-feed://...`。
- `provider_entitlement`：供应商授权、许可或合同证明，例如 `entitlement://...` 或 `vendor-contract://...`。
- `exchange_position_provider`：券商或交易所对账持仓服务证明，例如 `broker://...` 或 `exchange://...`。
- `signed_plugin_review`：已签名的插件/代码加载审查证明，例如 `signature://...`、`cosign://...` 或 `git-pr://...`。
- `production_calendar`：已批准的交易所生产日历证明，例如 `calendar://...` 或 `vendor-calendar://...`。
- `capacity_benchmark`：类生产环境延迟/容量压测证明，例如 `benchmark://...` 或 `grafana://...`。

## 完整生产还需要的额外证据

完整生产准入在上述证据之外，还必须补齐以下引用：

- `alt_archive_readiness`：另类数据归档就绪的外部 artifact/WORM 证明。
- `broker_borrow_availability`：券商背书的融券/借券可用性数据源证明。
- `paper_trading_90d`：使用真实行情完成的 90 天模拟盘报告。
- `rollover_runbook`：已批准的期货换月操作手册或执行证据。
- `failover_dr_test`：多区域故障切换与灾备演练报告。

V29 price-only 模拟盘不使用卖空/借券、另类数据快照或期货换月。它们仍然是完整生产 gate 的阻塞项，因为平台级生产 gate 比当前候选策略的 paper-start 范围更严格。

## 操作流程

1. 将 `config/production_evidence.v29.template.json` 复制为未纳入 Git 跟踪的本地文件，例如 `config/production_evidence.v29.local.json`。
2. 只填写受信外部系统返回的证据引用。不要粘贴 token、API Key、密码或 passphrase。
3. 也可以通过 `.env.local` 或 shell 导出 `QUANT_EVIDENCE_<KEY>_REF` 变量。
4. 运行采集器：

```bash
.venv/bin/python scripts/collect_v29_external_evidence_refs.py \
  --input-json config/production_evidence.v29.local.json
```

5. 校验外部生产证据 gate：

```bash
.venv/bin/python scripts/validate_external_evidence_v26.py \
  --evidence-file data/backtest_results/quant_v29_production_evidence_collected.json
```

6. 校验 V29 是否可以启动 paper trading：

```bash
.venv/bin/python scripts/prepare_v29_paper_launch_package.py \
  --evidence-file data/backtest_results/quant_v29_production_evidence_collected.json \
  --require-paper-launch-ready
```

7. 只有第 6 步通过后，才允许启动经批准的 90 天模拟盘服务。在模拟盘证据完成且完整生产 gate 通过前，实盘下单必须保持关闭。

## 可选 HTTPS 探活

如果真实外部服务已经存在，可以在安全的本地运行环境中设置非密钥 URL 和 token 环境变量，然后使用 `--probe-services`。采集器可以探活以下服务：

- Secret Manager：`QUANT_SECRET_MANAGER_URL`、`QUANT_SECRET_MANAGER_TOKEN`、`QUANT_SECRET_MANAGER_SECRET_REF`。
- Approval Service：`QUANT_APPROVAL_SERVICE_URL`、`QUANT_APPROVAL_SERVICE_TOKEN`、`QUANT_APPROVAL_CONFIG_HASH`。
- Position Provider：`QUANT_POSITION_PROVIDER_URL`、`QUANT_POSITION_PROVIDER_TOKEN`、`QUANT_EVIDENCE_EXCHANGE_POSITION_PROVIDER_REF`。
- Borrow Provider：`QUANT_BORROW_PROVIDER_URL`、`QUANT_BORROW_PROVIDER_TOKEN`。

探活 token 只在内存中使用，不会写入输出文件。
