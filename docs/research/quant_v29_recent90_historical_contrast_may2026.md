# V29 recent-90 May-2026 historical failed-bucket contrast

状态：研究诊断；不调参；不解除生产 blocker。

- target_month: 2026-05
- production_ready: false
- window: 20170101 -> 20260625

## v29_price_meta_longhorizon_guard

- diagnosis: failed_buckets_flip_vs_positive_history
- target_raw_contribution: -0.18748
- positive_month_count: 62 / 102

- industry: I65软件和信息技术服务业 target=-0.03726 hist_mean=0.016683 flip=True, C35专用设备制造业 target=-0.029944 hist_mean=0.018312 flip=True, C26化学原料和化学制品制造业 target=-0.018303 hist_mean=0.007323 flip=True
- liquidity_bucket: mid_amount_proxy target=-0.078418 hist_mean=0.067709 flip=True, low_amount_proxy target=-0.075338 hist_mean=0.075829 flip=True, high_amount_proxy target=-0.033724 hist_mean=0.055341 flip=True
- score_bucket: selected_mid target=-0.071377 hist_mean=0.066917 flip=True, selected_top target=-0.059735 hist_mean=0.068049 flip=True, selected_low target=-0.056367 hist_mean=0.063963 flip=True
- sleeve: price_lowvol_reversal target=-0.053975 hist_mean=0.048655 flip=True, price_ic_diversified target=-0.051769 hist_mean=0.051639 flip=True, price_ic_alpha target=-0.041511 hist_mean=0.052473 flip=True

## v29_price_meta_ultradefensive

- diagnosis: failed_buckets_flip_vs_positive_history
- target_raw_contribution: -0.145969
- positive_month_count: 62 / 102

- industry: I65软件和信息技术服务业 target=-0.028271 hist_mean=0.012395 flip=True, C35专用设备制造业 target=-0.022405 hist_mean=0.013544 flip=True, __UNKNOWN__ target=-0.01406 hist_mean=0.011439 flip=True
- liquidity_bucket: low_amount_proxy target=-0.060101 hist_mean=0.055921 flip=True, mid_amount_proxy target=-0.053682 hist_mean=0.049311 flip=True, high_amount_proxy target=-0.032185 hist_mean=0.041174 flip=True
- score_bucket: selected_mid target=-0.055429 hist_mean=0.048591 flip=True, selected_top target=-0.045404 hist_mean=0.050049 flip=True, selected_low target=-0.045136 hist_mean=0.047816 flip=True
- sleeve: price_lowvol_reversal target=-0.053975 hist_mean=0.048655 flip=True, price_ic_diversified target=-0.051769 hist_mean=0.051639 flip=True, price_defensive_breadth target=-0.040226 hist_mean=0.046019 flip=True

## Decision

仍然不能调参。该报告只区分 May-2026 失败 bucket 是历史上通常有正贡献但本月翻负，还是本身长期弱势。任何后续假设必须再跑 WF/random/look-ahead/monthly checks。
