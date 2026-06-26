# V48 外部证据收集清单

状态：这是收集清单，不是生产证据，不解除任何 blocker。

## 摘要

- 总项：19
- 缺失：19
- 已提供待校验：0
- paper report days：2
- paper calendar days：2

## 收集顺序

### P1 `pit_security_master`

- gate: V30/V48
- owner: data/vendor-management
- status: missing
- accepted prefixes: `security-master://`, `vendor-feed://`, `entitlement://`, `worm://`, `s3-object-lock://`
- action: Obtain vendor/broker PIT security-master extract covering listed/delisted/tradable state for the requested universe and attach immutable ref.

### P2 `trading_status_actions`

- gate: V30/V48
- owner: data/vendor-management
- status: missing
- accepted prefixes: `trading-status://`, `exchange-feed://`, `vendor-feed://`, `entitlement://`, `worm://`
- action: Obtain trading-day ST/suspension/limit-up/limit-down/tradability history with vendor/exchange provenance.

### P3 `corporate_action_reconciliation`

- gate: V30/V48
- owner: data/vendor-management/research
- status: missing
- accepted prefixes: `corporate-action://`, `reconciliation://`, `vendor-feed://`, `artifact://`, `worm://`
- action: Reconcile local adjusted prices against vendor corporate-action/adjust-factor feed and archive the reconciliation artifact.

### P4 `real_tick_minute_execution_feed`

- gate: V30/V48
- owner: data/vendor-management/execution
- status: missing
- accepted prefixes: `tick-feed://`, `minute-feed://`, `exchange-feed://`, `vendor-feed://`, `entitlement://`
- action: Procure authorized tick/minute/order-book execution-history feed for capacity and slippage validation.

### P5 `exchange_order_fill_replay`

- gate: V30/V48
- owner: broker-integration/execution
- status: missing
- accepted prefixes: `broker-fill://`, `exchange-fill://`, `order-log://`, `report://`, `artifact://`
- action: Export broker/exchange-backed historical order/fill replay or certified simulator fill logs.

### P6 `provider_entitlement`

- gate: V44/V48
- owner: legal/data-vendor-management
- status: missing
- accepted prefixes: `entitlement://`, `provider://`, `contract://`, `vendor-contract://`
- action: Attach license/entitlement/redistribution proof for every market-data source used by research and paper evidence.

### P7 `real_market_data_provider`

- gate: V44/V48
- owner: data/vendor-management
- status: missing
- accepted prefixes: `provider://`, `exchange-feed://`, `vendor-feed://`
- action: Bind real non-mock market-data provider contract/feed reference.

### P8 `external_worm_archive`

- gate: V44/V48
- owner: platform/secops
- status: missing
- accepted prefixes: `worm://`, `s3-object-lock://`, `vault-audit://`
- action: Provision external WORM/Object-Lock or vault audit archive and record attestation reference.

### P9 `secret_manager`

- gate: V44/V48
- owner: platform/secops
- status: missing
- accepted prefixes: `vault://`, `aws-sm://`, `sealed://`
- action: Bind production secrets to an approved secret manager; store only secret-manager reference, never secret values.

### P10 `approval_service`

- gate: V44/V48
- owner: risk/compliance
- status: missing
- accepted prefixes: `approval://`, `jira://`, `servicenow://`, `git-pr://`
- action: Create risk/config approval workflow record and store approval reference.

### P11 `exchange_position_provider`

- gate: V44/V48
- owner: broker-integration/ops
- status: missing
- accepted prefixes: `exchange://`, `broker://`
- action: Bind exchange/broker position snapshot provider and reconcile account binding.

### P12 `broker_borrow_availability`

- gate: V44/V48
- owner: broker-integration/ops
- status: missing
- accepted prefixes: `broker://`, `borrow-feed://`, `exchange://`, `entitlement://`
- action: Bind broker-backed borrow/availability feed or approved long-only no-borrow scope decision.

### P13 `paper_trading_90d`

- gate: V44/V48
- owner: research/ops
- status: missing
- accepted prefixes: `paper://`, `artifact://`, `ci-artifact://`, `report://`
- action: Let V49/V45 daily report cron accumulate natural 90-calendar/60-report-day evidence; do not backfill.

### P14 `production_calendar`

- gate: V44/V48
- owner: data/platform
- status: missing
- accepted prefixes: `calendar://`, `exchange-calendar://`, `vendor-calendar://`
- action: Attach approved production exchange calendar evidence used by runtime and backtest alignment.

### P15 `capacity_benchmark`

- gate: V44/V48
- owner: sre/platform
- status: missing
- accepted prefixes: `benchmark://`, `grafana://`, `artifact://`
- action: Run production-like capacity benchmark and archive report reference.

### P16 `failover_dr_test`

- gate: V44/V48
- owner: sre/platform
- status: missing
- accepted prefixes: `dr-test://`, `failover://`, `artifact://`
- action: Run multi-region failover/DR exercise and archive signed report reference.

### P17 `signed_plugin_review`

- gate: V44/V48
- owner: security/code-review
- status: missing
- accepted prefixes: `signature://`, `cosign://`, `git-pr://`
- action: Attach signed code-review/plugin-load artifact for production plugins.

### P18 `alt_archive_readiness`

- gate: V44/V48
- owner: data/platform
- status: missing
- accepted prefixes: `artifact://`, `ci-artifact://`, `worm://`, `s3-object-lock://`
- action: Archive alternative-data readiness artifact if alt data remains in scope; otherwise attach approved out-of-scope decision.

### P19 `rollover_runbook`

- gate: V44/V48
- owner: trading-ops/risk
- status: missing
- accepted prefixes: `rollover://`, `runbook://`, `approval://`
- action: Attach futures rollover runbook/evidence or approved out-of-scope decision if futures are disabled.
