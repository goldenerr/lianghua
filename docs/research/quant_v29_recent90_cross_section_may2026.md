# V29 recent-90 May-2026 股票袖子横截面归因

状态：研究诊断；不调参；不解除生产 blocker。

- target_month: 2026-05
- production_ready: false
- symbols_loaded: 1198

## v29_price_meta_longhorizon_guard

- total_raw_stock_contribution: -0.205434
- note: Sum of per-sleeve selected-stock contributions before V29 meta allocation scaling; use for cross-sectional root-cause attribution, not portfolio PnL.
- symbol_count: 278
- worst_sleeves: price_lowvol_reversal=-0.05815, price_ic_diversified=-0.055608, price_ic_alpha=-0.048171
- worst_industries: I65软件和信息技术服务业=-0.031514, C39计算机、通信和其他电子设备制造业=-0.026926, C35专用设备制造业=-0.023995, C38电气机械和器材制造业=-0.02058, C26化学原料和化学制品制造业=-0.020396
- worst_liquidity_buckets: low_amount_proxy=-0.081589, mid_amount_proxy=-0.077203, high_amount_proxy=-0.046642

## v29_price_meta_ultradefensive

- total_raw_stock_contribution: -0.157263
- note: Sum of per-sleeve selected-stock contributions before V29 meta allocation scaling; use for cross-sectional root-cause attribution, not portfolio PnL.
- symbol_count: 262
- worst_sleeves: price_lowvol_reversal=-0.05815, price_ic_diversified=-0.055608, price_defensive_breadth=-0.043505
- worst_industries: I65软件和信息技术服务业=-0.025571, C26化学原料和化学制品制造业=-0.019108, __UNKNOWN__=-0.017962, C35专用设备制造业=-0.017665, C39计算机、通信和其他电子设备制造业=-0.016695
- worst_liquidity_buckets: low_amount_proxy=-0.066072, mid_amount_proxy=-0.061605, high_amount_proxy=-0.029586

## 限制

- liquidity/amount bucket 只是 60 日成交额代理，不是真实市值。真实市值归因需要 vendor PIT security master。
- V31 trading status 是免费源研究近似，不是生产 broker/vendor evidence。
