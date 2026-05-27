# Quant Trading System Risk Policy

| Field | Value |
| --- | --- |
| Document version | 2.13.0-baseline |
| Updated | 2026-05-25 |
| Status | Production blocked pending approval and operational evidence |
| Config authority | `config/risk.yaml` plus approved, audited amendments |

## Version History

| Version | Date | Author | Change |
| --- | --- | --- | --- |
| 2.13.0-baseline | 2026-05-25 | Codex | Document enforceable limits, Safe Mode rules and approval gates |

This policy summarizes required controls and the current development configuration. It does not approve production use, alter existing approved thresholds or authorize automated trading.

## Safety Priority

1. Capital safety and invariant integrity are veto requirements.
2. Backtest-to-live consistency is required before considering return metrics.
3. Risk, compliance, auditability and reversibility are mandatory for any release.
4. A failed or unverifiable check blocks new positions and must be auditable.

## Configured Development Limits

The following values are transcribed from `config/risk.yaml`; changes require a diff, capital-impact report and risk approval before use outside development.

| Control | Config key | Current value | Required response |
| --- | --- | --- | --- |
| Single instrument exposure | `max_position_pct` | `20%` | Reject order above limit |
| Total leverage | `max_total_leverage` | `1.0x` | Reject leverage increase |
| Daily loss limit | `max_daily_loss_pct` | `2%` | Stop trading and alert |
| Single-trade loss | `max_single_loss_pct` | `0.5%` | Reject order |
| Portfolio VaR(95%) | `max_var_pct` | `5%` | Reject exposure; Safe Mode on violation |
| Liquidity participation | `max_volume_pct` | `10%` | Reject oversized participation |
| Drawdown reduction | `mdd_reduce_to_50pct` | `10%` | Reduce exposure to 50% |
| Drawdown liquidation | `mdd_liquidate_all` | `18%` | Liquidate/stop subject to safe execution |
| Circuit breaker reduce | `circuit_breaker_daily_loss` | `3%` | Reduce exposure and alert |
| Circuit breaker force | `circuit_breaker_daily_loss_force` | `5%` | Emergency liquidation procedure |
| Consecutive losses | `max_consecutive_losses` | `5` | Equity-curve protection |
| Weekly loss | `max_weekly_loss_pct` | `3%` | Reduce global risk exposure |

## Mandatory Invariants

| Name | Assertion | Violation action |
| --- | --- | --- |
| Position | Internal position plus pending orders equals exchange position within `0.01%` | Enter Safe Mode, prohibit new orders, urgent alert and reconcile |
| Equity | Realized PnL plus unrealized PnL plus cash equals total equity | Enter Safe Mode and investigate ledger |
| Idempotency | A `client_order_id` creates at most one exchange order | Reject duplicate submission and retain original mapping |
| Data consistency | Research and live paths consume the same adjusted data basis | Refuse strategy execution |
| VaR | VaR(95%) does not exceed configured limit | Reject exposure and enter Safe Mode if already breached |

Invariant enforcement must execute after recovery, reconciliation, shutdown processing and all material execution-state transitions.

## Order and Capital Controls

- Order generation must pass the risk engine and the final independent safety checks before submission.
- Capital allocation includes reserved funds for pending orders; reservation cannot be spent twice.
- An isolated strategy receives no new capital and cannot issue new exposure-increasing orders.
- Strategy lifecycle progression requires an auditable request, risk approval reference and validation duration evidence.
- Development and paper stages carry no live capital allocation.
- Small-live promotion requires at least 90 days in paper trading and is limited to 1% capital by `config/portfolio.yaml`.
- Full-live review requires at least 30 days in small-live and separate risk approval.

## Model and Factor Controls

- Model candidates cannot enter production without verified signatures, paper evidence and recorded approval.
- Feature drift uses reference-defined PSI bins; drift above threshold must trigger review or rollback.
- Adversarial robustness and market-regime bias results are required before model promotion.
- Model drift during activation returns traffic to the stable model and records the action.
- Factor definitions must be Corporate Action adjusted and version immutable.
- Offline and online factor snapshots require matching hashes before they can support trading validation.

## Data Quality Gate

Before backtest or live decisions:

| Check | Limit / requirement | Failure action |
| --- | --- | --- |
| Missing OHLCV data | `<= 1%` | Refuse strategy execution |
| Price jump | Configurable; baseline `<= +/-20%` adjacent close | Investigate/refuse data |
| Live freshness | `<= 30 seconds` | Alert and refuse new trades |
| Source cross-check | Difference `< 0.5%` | Refuse unapproved discrepancy |
| Corporate Actions | Adjustment completeness required | Refuse unadjusted path |

## Safe Mode and Emergency Procedures

Safe Mode prohibits new opening exposure while permitting controlled reduction and reconciliation. It is required when:

- an invariant fails;
- position/balance reconciliation exceeds tolerance;
- VaR or risk limits are violated;
- data or network state cannot be trusted.

Emergency mode is required for configured severe loss, kill-switch or unrecoverable integrity events. All state changes must be recorded through the authoritative audit bus. Resumption requires explicit human approval and confirmed reconciliation.

## Approval and Audit Requirements

| Change type | Required evidence before promotion |
| --- | --- |
| Strategy, model, portfolio, risk or execution logic | Capital-impact assessment, invariant tests, risk approval and signed artifact |
| Risk/configuration value | Before/after diff, capital-impact assessment and risk approval reference |
| Compliance jurisdiction/report change | Compliance approval, template evidence and immutable archive proof |
| Deployment artifact | Signed version manifest, config drift result and canary decision record |
| Sensitive audit export | RBAC authorization and approval reference |

All authoritative operational events must pass through the hash-chained audit bus. Local append-only archives are useful validation adapters but are not substitutes for approved external immutable retention in production.

Production configuration loading is fail-closed: account YAML documents may contain an approved `secret_ref` only, never credential material or environment-variable substitutions. Runtime must resolve the reference through an approved secret integration and return a configuration approval reference bound to the public configuration hash before startup can succeed.

Market scheduling is also fail-closed at the configuration/router boundary: an enabled market must declare validated local-time trading sessions and rules, timestamps must carry timezone information, and settlement-window checks normalize to UTC. This boundary is not a substitute for the still-required final execution-path integration.

## Validation Gates

| Stage | Minimum evidence | Status as of 2026-05-25 |
| --- | --- | --- |
| Local regression | Coverage `>=80%`, deterministic and safety tests | Met locally: `588` passing, `84.97%` coverage; Ruff/Black/mypy clean |
| Backtest approval | Validated full dataset, overfit controls and capital report | Not approved |
| Paper trading | At least 3 months, performance no less than 70% of accepted backtest | Not completed |
| Small live | At most 1% capital for at least 1 month with limits satisfied | Not started |
| Full live | Committee/human approval and archived evidence | Blocked |

## Outstanding Blockers

- No completed external immutable archive integration or retention attestation.
- No complete CI enforcement of signatures, drift reports and approval evidence.
- Runtime now requires secret-manager and configuration-approval adapters for production, but no approved production service integration, market feed, benchmark, factor-registry or model-registry is connected.
- Configured market-session and settlement restrictions are not yet bound to the final live order submission path.
- No measured production-like latency, capacity or cross-region failover evidence.
- No completed paper-trading or small-live acceptance period.
