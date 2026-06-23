# V45 影子模拟盘日报导入流水线

状态：流水线已生成，当前不代表生产批准。

## 结论

- `production_ready=false`
- 日报数量：0
- 本次动作：未导入新日报，仅刷新门禁
- V42 影子模拟盘证据通过：false
- V44 生产准入通过：false
- V44 阻塞项数量：20
- 最新日报日期：无

## 台账

- `/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v45_paper_shadow_ledger.json`

## 警告

- 无

## 下一步

- 每日收盘后导出只读行情、模拟成交、持仓快照和审计事件，形成单日 JSON 并运行本脚本。
- 连续至少 90 个自然日、至少 60 个日报交易日后，V42 才可能解除日报数量阻塞。
- 日报不得包含 token、API key、password 或私钥值。
- 即使 V42 通过，也必须等待 V40 robustness、外部 evidence 和风控审批全部通过。

## 日报最小字段

- `date`、`paper_daily_return`、`backtest_daily_return`
- `actual_slippage_bps`、`expected_slippage_bps`、`actual_cost_bps`、`expected_cost_bps`
- `audit_hash` 与 `audit_event_count`，或 `audit_events` / `audit_jsonl_path`
- `archive_ref`、`market_data_ref`、`position_snapshot_ref`、`order_fill_log_ref`
- `auto_trade_enabled=false`、`live_order_submission_allowed=false`
