# V29 fold-internal bucket-aware WF validation

状态：研究验证；fold 内训练生成阈值；不调参；不改策略；不解除 production blocker。

- production_ready: false
- can_change_strategy_now: false
- fold_internal_trigger_generation: true
- oos_data_used_for_specs: false

## v29_price_meta_longhorizon_guard

- passes_research_wf_gate: true
- avg_oos_delta: 0.035731
- positive_fold_rate: 1.0
- beats_random_median_fold_rate: 1.0
- blockers: []

- fold 0: delta=0.03929, flags=107, random_median=0.018181, beats_random=true, specs=6, test=2021-04-13..2022-04-26
- fold 1: delta=0.035157, flags=101, random_median=0.017898, beats_random=true, specs=6, test=2022-04-27..2023-05-11
- fold 2: delta=0.042036, flags=128, random_median=0.028354, beats_random=true, specs=6, test=2023-05-12..2024-05-27
- fold 3: delta=0.016661, flags=100, random_median=0.015995, beats_random=true, specs=6, test=2024-05-28..2025-06-11
- fold 4: delta=0.045509, flags=99, random_median=0.024247, beats_random=true, specs=6, test=2025-06-12..2026-06-25

## v29_price_meta_ultradefensive

- passes_research_wf_gate: true
- avg_oos_delta: 0.022779
- positive_fold_rate: 1.0
- beats_random_median_fold_rate: 1.0
- blockers: []

- fold 0: delta=0.026352, flags=116, random_median=0.013154, beats_random=true, specs=6, test=2021-04-13..2022-04-26
- fold 1: delta=0.021398, flags=99, random_median=0.011529, beats_random=true, specs=6, test=2022-04-27..2023-05-11
- fold 2: delta=0.026501, flags=124, random_median=0.018471, beats_random=true, specs=6, test=2023-05-12..2024-05-27
- fold 3: delta=0.010075, flags=98, random_median=0.00948, beats_random=true, specs=6, test=2024-05-28..2025-06-11
- fold 4: delta=0.029568, flags=97, random_median=0.015411, beats_random=true, specs=6, test=2025-06-12..2026-06-25

## Decision

This validates only fold-internal research timing. Production remains blocked by external evidence/paper/live gates even if research folds pass.
