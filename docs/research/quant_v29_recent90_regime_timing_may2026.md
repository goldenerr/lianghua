# V29 recent-90 May-2026 regime trigger timing audit

状态：研究诊断；不调参；不解除生产 blocker。

- target_month: 2026-05
- production_ready: false
- pre_start: 2026-04-02

## v29_price_meta_longhorizon_guard

- diagnosis: risk_off_missed_loss_window
- target compound_return: -0.027217
- target risk_off_ratio: 0.0
- avg_stock_exposure: 0.457432
- avg_crisis_weight: 0.406926
- avg_mom_20: 0.038676
- avg_mom_60: 0.000715
- avg_vol_20: 0.168156
- reason_counts: {'none': 14, 'bull_conditions': 4}

## v29_price_meta_ultradefensive

- diagnosis: risk_off_missed_loss_window
- target compound_return: -0.018951
- target risk_off_ratio: 0.0
- avg_stock_exposure: 0.314492
- avg_crisis_weight: 0.685508
- avg_mom_20: 0.038676
- avg_mom_60: 0.000715
- avg_vol_20: 0.168156
- reason_counts: {'none': 14, 'bull_conditions': 4}

## Decision

仍然不能调阈值或暴露。下一步先证明 equal-weight market regime 输入是否过宽，导致行业/袖子级别亏损没有触发 risk-off。
