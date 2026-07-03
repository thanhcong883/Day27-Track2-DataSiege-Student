# Reflection — Data Siege

## Hardest Fault Types

### 1. `missing_upstream` (Lineage pillar)
This was the trickiest fault to detect. The event payload's `inputs` field only
declares **one** input (e.g. `raw.orders`), but clean runs returned two actual
upstream nodes (`raw.orders` and `raw.customers`). The toolkit's
`actual_upstream` list simply became shorter — not empty — for faulty events.
A naive "is upstream empty?" check completely failed. The solution was to **track
the historical maximum upstream count per job name** in `ctx.state` and alert
whenever the current event falls below that learned expectation. This generalizes
well because it adapts to any job topology rather than hardcoding a count.

### 2. `feature_skew` — subtle tier (ai_infra pillar)
Some feature skew events had `mean_shift_sigma` values below the baseline
`feature_mean_shift_sigma_max` threshold (0.4095 σ). A fixed threshold alone
would miss these. I added a secondary detection at **75% of the baseline max**
and an online z-score over the rolling history of sigma values, catching events
that are statistically anomalous relative to recent clean history even when they
don't breach the absolute threshold.

### 3. `distribution_shift` (Checks pillar)
Distribution drift on `mean_amount` can be subtle when it drifts gradually. I
combined the absolute `mean_amount_min / mean_amount_max` bounds with a rolling
online z-score (window=30) over past mean_amount values. This dual-signal
approach lets us catch both step-change and gradual drift.

## Cost / Coverage Tradeoff

**Budget used: 180.0 / 220.0 credits (82% utilization)**

The main cost drivers were the `ai_infra` handlers: each `feature_drift` or
`embedding_drift` call costs 2.0 credits. I added a **budget guard** that skips
these expensive calls if fewer than 5 credits remain, trading coverage for
cost safety at the tail of the stream.

**What I'd change:**
- Implement adaptive budget management: spend more aggressively early in the
  stream (where subtle faults are harder to detect without data history) and
  become more conservative late (by then, the online z-score histories are warm
  and can partially substitute for expensive metered calls).
- Explore skipping `batch_profile` for data batches whose rolling statistics
  look stable, calling it only every N events or when a lightweight heuristic
  (e.g., a hash of the batch_id modulo pattern) suggests drift. This could free
  up budget for the more expensive ai_infra calls.
- For `lineage`, the current approach learns expected upstream counts from
  history. In the private phase with more subtle faults, a small early run of
  faulty events could poison the learned max — a more robust solution would
  initialize the expected count from a pre-known topology if available.

## Final Practice Score: 49.31
- TPR: 1.0 (100% of faults caught)
- FPR: 0.023 (2.3% false alarm rate on clean events)
- All four pillars rated HIGH
- Cost overage: 0.0 (well within 220-credit budget)
