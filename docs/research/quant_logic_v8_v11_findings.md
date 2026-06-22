# V8-V15 Strategy Research Findings

Status: production blocked.
Date: 2026-06-05

## Scope

This note records research-only strategy work after the strict 2026-05-29 data
alignment refresh. None of these results authorizes live trading or feature
approval.

## Data Baseline

- A-share OHLCV parquet coverage: 1200/1200 symbols refreshed through
  2026-05-29.
- Industry classification: CNInfo-backed `data/industry_fixed.parquet`,
  1200/1200 symbols.
- Fundamentals: THS financial abstract parquet coverage about 97.9%; remaining
  gaps are explicit provider failures, not filled with synthetic values.
- Benchmark/ETF data: 11 benchmark and ETF series through 2026-05-29.
- Earnings events: EastMoney/AkShare `data/earnings_events/earnings_events.parquet`,
  137,060 rows, 5,580 symbols, 0 fetch failures after retry. Within the aligned
  1,196-symbol research universe, 1,195 symbols have event coverage. Research
  uses `announcement_date <= rebalance_date - lag`, never raw report date.

## Research Results

| Version | Main idea | Full Sharpe | Max drawdown | WF result | Gate |
| --- | --- | ---: | ---: | --- | --- |
| V8 | V7 plus portfolio overlay grid | ~0.55 | ~-20% | Avg OOS negative | Fail |
| V9 | Dynamic IC/ICIR factors plus fundamentals | 0.9038 | -11.68% | Avg OOS 0.1666, decay 0.8277 | Fail |
| V10 | V9 plus beta hedge proxy | 0.9260 | -10.39% | Avg OOS 0.1614, decay 0.8385 | Fail |
| V11 broad | V10 plus broad equity/defensive ETF trend sleeve | 0.6032 | -45.99% | Avg OOS improved in some folds but drawdown worsened | Fail |
| V11 defensive | V10 plus cash/gold defensive trend sleeve | 0.9485 | -10.47% | Avg OOS 0.3808, decay 0.6177 | Fail |
| V12 | Full industry rotation whitelist | 0.8558 | -13.42% | Avg OOS 1.0793, decay improved but full Sharpe lower | Fail |
| V13 | Regime-gated industry rotation | 0.9854 | -12.73% | Avg OOS 0.8194, decay 0.2010 | Fail |
| V14 | Industry-relative alpha on V13 | 1.0434 | -13.60% | Avg OOS mixed; decay/drawdown gates not both stable | Fail |
| V15 | V14 plus point-in-time earnings events | 1.0325 | -13.49% | Avg OOS 0.8057, decay 0.2344 | Fail |
| V16 | Multi-expert regime router | 1.0328 | -13.58% | Avg OOS 0.8151, decay 0.2474 | Fail |

## Defensive V11 Walk-Forward

- F0: IS 1.0294, OOS 0.7014, MDD -4.26%.
- F1: IS 1.0165, OOS -1.4144, MDD -5.62%.
- F2: IS 0.9882, OOS 1.6153, MDD -2.62%.
- F3: IS 0.9595, OOS 0.7136, MDD -5.09%.
- F4: IS 0.9866, OOS 0.2882, MDD -7.62%.
- Average IS Sharpe: 0.9960.
- Average OOS Sharpe: 0.3808.
- Sharpe decay: 0.6177.

## Interpretation

- The cash/gold defensive sleeve materially improves drawdown control versus
  earlier long-only variants.
- The broad equity ETF trend sleeve is rejected because it reintroduces equity
  beta and pushes full-sample drawdown to -45.99%.
- V12-V14 show that industry rotation and industry-relative ranking improve
  drawdown and some OOS folds, but do not lift full-sample Sharpe to the
  required 1.2 threshold.
- V15 earnings events provide modest incremental alpha. The best grid candidate
  is `event_weight=0.15`; larger event weights degrade Sharpe. V15 improves WF
  decay to 0.2344, but full-sample Sharpe remains only 1.0325 and one OOS fold
  remains negative (`F1` OOS Sharpe -0.9343).
- V16 implements a public top-quant-inspired multi-expert router across
  technical, fundamental, earnings and defensive experts. The best grid variant
  is `moe_more_exposure`, but its full Sharpe improves only from V15's 1.0325
  to 1.0328 and its difficult `F1` OOS fold worsens to -1.0347. This is not a
  material breakthrough.
- The remaining blocker is not primarily risk overlay. It is alpha/regime
  instability and insufficient independent return sources, especially the
  sample-out window where the long-only A-share sleeve still turns negative.
- Continuing small V5.9/V7 parameter tweaks is unlikely to pass production
  gates. The next research iteration needs a stronger independent alpha source
  or explicit regime/industry rotation model.

## V15 Walk-Forward

- F0: IS 1.0657, OOS 1.2123, MDD -4.87%.
- F1: IS 1.0604, OOS -0.9343, MDD -5.94%.
- F2: IS 1.0402, OOS 2.6296, MDD -2.36%.
- F3: IS 1.0458, OOS 0.4559, MDD -7.41%.
- F4: IS 1.0501, OOS 0.6652, MDD -7.81%.
- Average IS Sharpe: 1.0524.
- Average OOS Sharpe: 0.8057.
- Sharpe decay: 0.2344.
- Gates: `S=false`, `M=true`, `D=true`, `W=true`, `A=false`.

## V16 Walk-Forward

- F0: IS 1.1102, OOS 1.0531, MDD -6.73%.
- F1: IS 1.0939, OOS -1.0347, MDD -6.10%.
- F2: IS 1.0717, OOS 3.2198, MDD -2.36%.
- F3: IS 1.0769, OOS 0.3173, MDD -9.07%.
- F4: IS 1.0627, OOS 0.5202, MDD -8.16%.
- Average IS Sharpe: 1.0831.
- Average OOS Sharpe: 0.8151.
- Sharpe decay: 0.2474.
- Gates: `S=false`, `M=true`, `D=true`, `W=true`, `A=false`.

## V16 Interpretation

- The V16 structure is directionally closer to industry practice: multiple weak
  signals, regime routing and cost-aware portfolio construction.
- The experiment demonstrates that model complexity alone is not enough. With
  the current daily-bar, fundamentals and earnings-event data, the router mostly
  reshuffles existing information and does not create new alpha.
- The next meaningful production-candidate research must add genuinely new
  information: analyst estimate revisions, fund/order-flow, intraday liquidity
  and reversal data, borrow/short availability, or an approved independent
  multi-asset/market-neutral sleeve.

## Next Research Requirements

- Keep the point-in-time earnings event factor as a low-weight auxiliary signal,
  but do not rely on it as the production breakthrough alpha.
- Add stronger independent data: analyst estimate revisions, order-flow/funds,
  intraday reversal/liquidity, lending/shorting where available, or an approved
  market-neutral sleeve.
- Replace the index hedge proxy with approved exchange-backed futures data,
  margin modelling, rollover evidence and position reconciliation before any
  production consideration.
- Keep paper/live gates closed until Sharpe >= 1.2, MDD <= 15%, win rate >= 40%
  and WF decay <= 30% are met under strict point-in-time validation.
