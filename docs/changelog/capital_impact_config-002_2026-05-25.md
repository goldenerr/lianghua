# 资金影响评估报告 - config-002 多市场交易时段与结算窗口安全基线

| Field | Value |
| --- | --- |
| Change ID | `config-002` |
| Assessment date | `2026-05-25`; submission-boundary extension validated `2026-05-27` |
| Environment scope | Development validation only |
| Approval status | Pending risk review; not approved for production |
| Release posture | Production blocked |

## Change Summary

- Runtime market calendars and settlement windows are now populated from validated `SystemSettings`, rather than hard-coded module constants.
- Data-source priority and fee-model routing identifiers are now explicit `market_rules` configuration values included in the public configuration hash; routing metadata is unavailable for disabled markets.
- `DataSourceManager` now consumes the active `MarketRouter` data-source order by default, so runtime provider selection follows the hash-bound market configuration instead of a separate hard-coded table.
- Configured-but-unimplemented provider IDs such as `tushare`/`polygon` are surfaced through `get_missing_configured_sources()` diagnostics rather than silently disappearing from the operational picture.
- Runtime market-control publication is now loader-only: direct router publication is denied even with fabricated hash/approval strings; loader publication requires hash/production approval evidence and atomically preserves the last verified snapshot when reload validation fails.
- Validated market-control collections (`primary_markets`, sessions, rules and data-source priority) are immutable tuples, preventing in-process list mutation after a snapshot hash is accepted.
- Trading-session definitions use market-local IANA timezones and are converted to UTC at decision time.
- `A股` and `期货` configured economic opening hours are unchanged; their representation changes from fixed UTC strings to local-market clocks.
- `美股` schedule metadata is added to support daylight saving conversion, but it is not active because `primary_markets` remains `["A股"]`.
- Markets missing from `primary_markets`, missing controls, or timestamps without timezone information fail closed.
- `OrderManager.submit()` requires explicit market metadata, blocks unavailable market times, and audits schedule rejections before an order becomes submitted.
- `OrderManager.submit()` now rejects non-positive/non-lot quantities and invalid price-field/tick combinations before submission; quantity utilities normalize toward zero only and never increase requested exposure.
- Settlement-window bypass cannot be claimed solely through `reduce_only=True`; it requires an independent injected validator.
- A standard `ReconciledReduceOnlyValidator` is now available for settlement-window reductions: it requires a fresh timezone-aware reconciled position snapshot, rejects stale/non-finite data, rejects orders that would increase exposure, and rejects oversized reductions that would flip the position.
- The strategy base class now wraps subclass/plugin `on_data` methods with the same market-session guard, requiring an explicit market and suppressing candidate signals outside permissible market times, including settlement restrictions.
- `pandas-market-calendars` is now a locked runtime dependency; non-24/7 markets refuse decisions when an exchange holiday calendar is missing, unsupported or unavailable.
- Runtime calendar registry publication is atomic: startup or hot reload rejects an active non-24/7 market whose approved exchange calendar cannot be initialized, leaving the previous verified controls intact on reload failure.
- `holiday_calendar` is now explicit validated session configuration rather than a source-code mapping; it is included in the public configuration hash so changing the selected exchange calendar requires renewed approval evidence.
- Futures rollover detection now emits an immutable audit-bus intent only: detectors are created only through the configured router when `期货` is active, the same candidate contract must lead on strictly increasing daily observations for the configured threshold, invalid/duplicate/backward observations are rejected without state mutation, configured basis-point cost assumptions replace hidden constants, and `require_manual_approval` cannot be disabled.

## Capital Exposure Assessment

| Risk area | Impact of change | Current mitigation |
| --- | --- | --- |
| Unauthorized opening trades outside sessions | Reduced in the configuration/router layer because configured closures and inactive markets are rejected | Unit tests cover lunch closure, weekend closure, inactive market rejection and configured schedule override |
| Settlement-window new exposure | Reduced in the configuration/router layer by local-time settlement conversion and configurable restriction window | Unit tests cover A-share and futures windows plus non-UTC aware timestamp conversion |
| Daylight saving errors | Reduced for US-market scheduling because local clock rules are mapped with `America/New_York` | DST winter/summer tests; US market is not enabled in current development primary markets |
| Overnight futures misclassification | Reduced through explicit overnight session trading-date logic | Overnight continuation test |
| Final order-submission boundary | Reduced locally: `OrderManager` now enforces configured market-time gates and audits rejected submissions | Production remains blocked until an approved exchange-backed reduce-only position provider and approval evidence exist |
| Lot/tick rule bypass at submission | Reduced locally: non-lot quantities, malformed order price fields and off-tick executable prices are rejected and audited before submission | Rule configuration is validated; production approval remains required |
| Hard-coded routing metadata or disabled-market selection | Reduced locally: source priority and fee-model IDs are configured/hash-bound; `DataSourceManager` resolves providers from `MarketRouter`; disabled markets cannot return routing metadata or data providers | Approved provider and compliance integrations remain dependencies of later features |
| Unapproved runtime publication or failed reload state loss | Reduced locally: direct router publication refuses even forged hash/approval strings; only the loader publishes after validation, and failed reload retains prior verified settings and controls | Python-private capabilities remain subject to signed-code/review enforcement |
| Mutation after configuration hash acceptance | Reduced locally: market/session/rule/source collections are immutable tuples after validation | Complementary risk/API/account snapshot immutability is recorded under `config-001` |
| Forged reduce-only request | Reduced locally: a flag alone cannot bypass settlement restrictions; the standard validator checks fresh reconciled position direction and size | Missing/failed validator blocks the order |
| Off-session plugin strategy signal | Reduced locally: `Strategy.__init_subclass__` wraps subclass `on_data` with the market-session guard and emits audit evidence on suppression | Production still requires signed plugin loading and review evidence |
| Holiday or calendar-provider outage | Reduced locally: unavailable or unsupported non-24/7 calendars resolve to blocked trade decisions and rejected audited orders | Production adapter availability and calendar approval evidence remain required |
| Missing or unregistered active market calendar at startup/reload | Reduced locally: strict configuration or registry publication fails before the market can be used; a failed reload does not replace existing verified controls | Startup and atomic-registry regression tests |
| Calendar selection changed without review | Reduced locally: the configured `holiday_calendar` participates in the public configuration hash | Config-hash regression test; production approval still required |
| Incorrect futures roll due to alternating candidate contracts | Reduced locally: leadership accumulation is reset when the leading candidate contract changes | Detector regression test; no orders generated |
| Repeated/invalid rollover observations forge a roll intent | Reduced locally: dates must strictly increase and observations must include the current matching-underlying contract with finite volume and a forward-expiry candidate | Rejected observations are audited without mutating roll state |
| Automatic rollover exposure or unreviewed rollover cost assumptions | Reduced locally: rollover policy is strict, hash-bound and forces manual approval; detector records intent/cost through audit bus only | Execution remains disconnected until approved reconciliation and order controls exist |
| Direct detector construction bypasses approved rollover policy | Reduced locally: unbound construction is rejected; only the configured router factory can provide an active policy | Factory enforcement and unconfigured-runtime regression tests |
| Disabled futures market emits rollover intent | Reduced locally: detector creation rejects unless `期货` is enabled in `primary_markets` | Production remains blocked until execution and approval evidence exist |

No position, leverage, loss, VaR, drawdown or liquidity threshold was changed. This change does not authorize capital deployment or automated trading.

## Invariant Analysis

| Invariant | Risk introduced or reduced | Required follow-up |
| --- | --- | --- |
| Position reconciliation | Rejected schedule requests do not enter submitted state; settlement-window reductions require fresh reconciled position proof | Bind validator to an approved exchange-backed provider before production |
| Equity identity | No accounting values or fee calculations changed | Retain existing invariant tests |
| Order idempotency | No `client_order_id` behavior changed | Retain existing execution tests |
| Data consistency | Timestamp interpretation is stricter; unzoned inputs are rejected | Ensure data feeds emit aware UTC timestamps |
| VaR limit | No threshold or risk calculation change | Keep risk-policy values unchanged |

## Failure Modes And Mitigations

| Failure mode | Consequence | Mitigation / block |
| --- | --- | --- |
| Incorrect configured session or timezone | Incorrect availability decision | Pydantic validation, required controls per active market, configuration approval required before production |
| Invalid lot/tick/price configuration or submission | An order could increase exposure above intended size or be rejected/mispriced by the venue | Non-finite/precision-inconsistent rules fail loading; final submission rejects non-lot or off-tick requests with audit evidence |
| Direct registry access to inactive market controls | A caller could evaluate or route a market excluded from `primary_markets` | Calendar/rule/settlement registry access now fails closed for inactive markets |
| Holiday-calendar adapter unavailable | New orders cannot be authorized for affected non-24/7 markets until a trusted calendar is restored | Fail-closed implementation and tests; approved production calendar operational evidence remains required |
| Invalid active-market calendar deployment | The service might otherwise start with a market it cannot safely schedule | Strict `holiday_calendar` validation and startup/reload rejection before publishing controls |
| Incorrect but library-recognized calendar selected | Wrong closures could be accepted if an inappropriate identifier is approved | Calendar identifier is explicit and hash-bound; production risk approval and calendar evidence remain blocking requirements |
| Rollover intent incorrectly treated as authorization | Unapproved close/open orders could change exposure and incur cost | Detector never submits orders; `require_manual_approval=true` is immutable in validated configuration |
| Validator not backed by reconciled positions | A falsely approved reduction could increase exposure during settlement restrictions | Reduced locally with `ReconciledReduceOnlyValidator`; production still needs an approved exchange-backed position provider |
| Plugin strategy bypasses framework guard | A custom implementation could generate off-session intent without equivalent evidence | Reduced locally by automatic base-class wrapping; production still requires signed plugin loading and review evidence |

## Validation Evidence

- Development configuration loads with only `A股` active; inactive `加密货币` trading is rejected.
- Focused strategy, execution and market-router guard suite: `110 passed`.
- Full regression: `668 passed` with total coverage `85.68%`.
- Focused data/config routing suite: `91 passed`; subset-only coverage gate is expected to fail outside the full regression run because project coverage is enforced globally.
- Smoke backtest consistency: `100.0%`; Ruff, Black and strict mypy gates pass locally.

## Approval Record

| Required approver | Status | Reference |
| --- | --- | --- |
| Risk officer | Pending | Not provided |
| Release/operations authorization | Pending | Not provided |

This report records development impact only. It must not be treated as production approval.
