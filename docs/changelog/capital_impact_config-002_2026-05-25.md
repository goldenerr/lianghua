# 资金影响评估报告 - config-002 多市场交易时段与结算窗口安全基线

| Field | Value |
| --- | --- |
| Change ID | `config-002` |
| Assessment date | `2026-05-25` |
| Environment scope | Development validation only |
| Approval status | Pending risk review; not approved for production |
| Release posture | Production blocked |

## Change Summary

- Runtime market calendars and settlement windows are now populated from validated `SystemSettings`, rather than hard-coded module constants.
- Trading-session definitions use market-local IANA timezones and are converted to UTC at decision time.
- `A股` and `期货` configured economic opening hours are unchanged; their representation changes from fixed UTC strings to local-market clocks.
- `美股` schedule metadata is added to support daylight saving conversion, but it is not active because `primary_markets` remains `["A股"]`.
- Markets missing from `primary_markets`, missing controls, or timestamps without timezone information fail closed.

## Capital Exposure Assessment

| Risk area | Impact of change | Current mitigation |
| --- | --- | --- |
| Unauthorized opening trades outside sessions | Reduced in the configuration/router layer because configured closures and inactive markets are rejected | Unit tests cover lunch closure, weekend closure, inactive market rejection and configured schedule override |
| Settlement-window new exposure | Reduced in the configuration/router layer by local-time settlement conversion and configurable restriction window | Unit tests cover A-share and futures windows plus non-UTC aware timestamp conversion |
| Daylight saving errors | Reduced for US-market scheduling because local clock rules are mapped with `America/New_York` | DST winter/summer tests; US market is not enabled in current development primary markets |
| Overnight futures misclassification | Reduced through explicit overnight session trading-date logic | Overnight continuation test |
| Final live-order enforcement | Not yet reduced end to end: `OrderManager` is not wired to this route gate in the current dependency stage | Production remains blocked until execution-path integration and approval evidence exist |

No position, leverage, loss, VaR, drawdown or liquidity threshold was changed. This change does not authorize capital deployment or automated trading.

## Invariant Analysis

| Invariant | Risk introduced or reduced | Required follow-up |
| --- | --- | --- |
| Position reconciliation | No direct state transition introduced; a future execution integration must prevent rejected orders from entering pending state | Verify when `exec-001` binds this gate |
| Equity identity | No accounting values or fee calculations changed | Retain existing invariant tests |
| Order idempotency | No `client_order_id` behavior changed | Retain existing execution tests |
| Data consistency | Timestamp interpretation is stricter; unzoned inputs are rejected | Ensure data feeds emit aware UTC timestamps |
| VaR limit | No threshold or risk calculation change | Keep risk-policy values unchanged |

## Failure Modes And Mitigations

| Failure mode | Consequence | Mitigation / block |
| --- | --- | --- |
| Incorrect configured session or timezone | Incorrect availability decision | Pydantic validation, required controls per active market, configuration approval required before production |
| Holiday-calendar adapter unavailable | Weekend fallback does not supply full production holiday assurance | Approved production calendar integration and evidence remain required |
| Router gate not invoked by execution path | Orders could bypass scheduling controls | Explicit production blocker; integrate under execution feature with audit and tests |

## Validation Evidence

- Development configuration loads with only `A股` active; inactive `加密货币` trading is rejected.
- Focused configuration, smoke and safety regression suite: `123 passed`.
- Full regression: `588 passed` with total coverage `84.97%`.
- Smoke backtest consistency: `100.0%`; Ruff, Black and strict mypy gates pass locally.

## Approval Record

| Required approver | Status | Reference |
| --- | --- | --- |
| Risk officer | Pending | Not provided |
| Release/operations authorization | Pending | Not provided |

This report records development impact only. It must not be treated as production approval.
