"""
Data Siege — defense.py
Implements register(ctx) and one handler per event type.

Strategy:
- For each event type, call the metered toolkit method once and compare
  against ctx.baseline thresholds (mean ± 3σ derived bounds).
- Use ctx.state to track per-pillar history and derive additional
  statistical signals (rolling z-score style detection for subtle faults).
- Be cost-aware: only call expensive tools (feature_drift, embedding_drift at
  cost 2.0 each) when budget allows; skip if budget is critically low.
- Combine multiple signals per event to catch both obvious and subtle faults.

Note: only imports from the allowlist are used (api, math, collections, itertools).
      'statistics' is excluded because it transitively imports 'numbers'.
"""
from api import Verdict
import math
import collections


# ── helpers ──────────────────────────────────────────────────────────────────

def _mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)


def _variance(values):
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return sum((x - m) ** 2 for x in values) / (len(values) - 1)


def _stdev(values):
    return math.sqrt(_variance(values))


def _zscore(value, history):
    """Return z-score of value relative to history list (excluding itself)."""
    if len(history) < 3:
        return 0.0
    m = _mean(history)
    s = _stdev(history)
    if s == 0:
        return 0.0
    return (value - m) / s


def _push(state, key, value, max_len=30):
    """Append value to state[key] circular buffer."""
    if key not in state:
        state[key] = []
    state[key].append(value)
    if len(state[key]) > max_len:
        state[key] = state[key][-max_len:]


# ── register ─────────────────────────────────────────────────────────────────

def register(ctx):
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


# ── 1. data_batch (pillar: checks) ───────────────────────────────────────────

def check_data_batch(payload, ctx):
    """
    Checks: freshness_lag, volume_spike, null_spike, distribution_shift.
    Tool cost: 1.0 per call.
    Baseline keys: row_count_min/max, null_rate_max,
                   mean_amount_min/max, staleness_min_max
    """
    batch_id = payload.get("batch_id")
    if not batch_id:
        return Verdict(alert=False, pillar="checks", reason="no batch_id")

    prof = ctx.tools.batch_profile(batch_id)
    if "error" in prof:
        return Verdict(alert=False, pillar="checks", reason="profile error")

    row_count     = prof.get("row_count", 0)
    null_rate     = prof.get("null_rate", {}).get("customer_id", 0)
    mean_amount   = prof.get("mean_amount", 0)
    staleness_min = prof.get("staleness_min", 0)

    bl = ctx.baseline
    flags = []

    # freshness_lag
    stale_max = bl.get("staleness_min_max", 1e9)
    if staleness_min > stale_max:
        flags.append("freshness_lag")

    # volume anomaly
    rc_min = bl.get("row_count_min", 0)
    rc_max = bl.get("row_count_max", 1e9)
    if row_count < rc_min or row_count > rc_max:
        flags.append("volume_spike")

    # null_spike
    nr_max = bl.get("null_rate_max", 1e9)
    if null_rate > nr_max:
        flags.append("null_spike")

    # distribution_shift
    ma_min = bl.get("mean_amount_min", 0)
    ma_max = bl.get("mean_amount_max", 1e9)
    if mean_amount < ma_min or mean_amount > ma_max:
        flags.append("distribution_shift")

    # online z-score — catch subtle drift not yet out of baseline bounds
    _push(ctx.state, "batch_rc", row_count)
    _push(ctx.state, "batch_ma", mean_amount)
    _push(ctx.state, "batch_nr", null_rate)
    _push(ctx.state, "batch_stale", staleness_min)
    hist_rc = ctx.state["batch_rc"][:-1]
    hist_ma = ctx.state["batch_ma"][:-1]
    hist_nr = ctx.state["batch_nr"][:-1]
    hist_st = ctx.state["batch_stale"][:-1]
    if abs(_zscore(row_count, hist_rc)) > 3.5 and "volume_spike" not in flags:
        flags.append("online_volume_anomaly")
    if abs(_zscore(mean_amount, hist_ma)) > 3.5 and "distribution_shift" not in flags:
        flags.append("online_dist_anomaly")
    if abs(_zscore(null_rate, hist_nr)) > 3.5 and "null_spike" not in flags:
        flags.append("online_null_anomaly")
    if abs(_zscore(staleness_min, hist_st)) > 3.0 and "freshness_lag" not in flags:
        flags.append("online_staleness_anomaly")

    alert = len(flags) > 0
    reason = "; ".join(flags) if flags else "clean"
    return Verdict(alert=alert, confidence=min(1.0, 0.5 + 0.15 * len(flags)),
                   reason=reason, pillar="checks")


# ── 2. contract_checkpoint (pillar: contracts) ───────────────────────────────

def check_contract_checkpoint(payload, ctx):
    """
    Checks: schema_break, type_violation, freshness SLA.
    Tool cost: 1.5 per call.
    Baseline keys: freshness_delay_max_min
    """
    contract_id         = payload.get("contract_id")
    checkpoint_batch_id = payload.get("checkpoint_batch_id")
    if not contract_id or not checkpoint_batch_id:
        return Verdict(alert=False, pillar="contracts", reason="missing ids")

    diff = ctx.tools.contract_diff(contract_id, checkpoint_batch_id)
    if "error" in diff:
        return Verdict(alert=False, pillar="contracts", reason="diff error")

    violations      = diff.get("violations", [])
    freshness_delay = diff.get("freshness_delay_min", 0)

    flags = list(violations)

    # freshness SLA
    fd_max = ctx.baseline.get("freshness_delay_max_min", 1e9)
    if freshness_delay > fd_max:
        flags.append("freshness_sla_violation")

    alert = len(flags) > 0
    reason = "; ".join(flags) if flags else "clean"
    return Verdict(alert=alert, confidence=min(1.0, 0.6 + 0.2 * len(flags)),
                   reason=reason, pillar="contracts")


# ── 3. lineage_run (pillar: lineage) ─────────────────────────────────────────

def check_lineage_run(payload, ctx):
    """
    Checks: missing_upstream, orphan_output, runtime_anomaly.
    Tool cost: 1.0 per call (depth=1).
    Baseline keys: lineage_duration_ms_max

    Key insight from data inspection:
    - missing_upstream: actual_upstream count is LESS than the historical max
      seen for this job. Clean events show N nodes; faulty show fewer.
    - orphan_output: actual_downstream_count == 0
    - runtime_anomaly: duration_ms > baseline max
    """
    run_id = payload.get("run_id")
    if not run_id:
        return Verdict(alert=False, pillar="lineage", reason="no run_id")

    graph = ctx.tools.lineage_graph_slice(run_id, depth=1)
    if "error" in graph:
        return Verdict(alert=False, pillar="lineage", reason="graph error")

    duration_ms             = graph.get("duration_ms", 0)
    actual_upstream         = graph.get("actual_upstream", [])
    actual_downstream_count = graph.get("actual_downstream_count", 0)

    actual_upstream_list = actual_upstream if isinstance(actual_upstream, list) else []
    actual_upstream_count = len(actual_upstream_list)

    # Track job-specific upstream count history
    job_key = "lineage_upstream_max_" + payload.get("job", "unknown")
    if job_key not in ctx.state:
        ctx.state[job_key] = 0
    # Update max seen upstream count
    if actual_upstream_count > ctx.state[job_key]:
        ctx.state[job_key] = actual_upstream_count

    bl = ctx.baseline
    flags = []

    # runtime_anomaly
    dur_max = bl.get("lineage_duration_ms_max", 1e9)
    if duration_ms > dur_max:
        flags.append("runtime_anomaly")

    # missing_upstream: actual < max ever seen for this job
    expected_upstream_count = ctx.state[job_key]
    if actual_upstream_count < expected_upstream_count:
        flags.append("missing_upstream")
    elif actual_upstream_count == 0:
        flags.append("missing_upstream")

    # orphan_output: no downstream consumers
    if actual_downstream_count == 0:
        flags.append("orphan_output")

    # online z-score on runtime for subtle anomaly
    _push(ctx.state, "lineage_dur", duration_ms)
    hist_dur = ctx.state["lineage_dur"][:-1]
    if abs(_zscore(duration_ms, hist_dur)) > 3.0 and "runtime_anomaly" not in flags:
        flags.append("online_runtime_anomaly")

    alert = len(flags) > 0
    reason = "; ".join(flags) if flags else "clean"
    return Verdict(alert=alert, confidence=min(1.0, 0.6 + 0.2 * len(flags)),
                   reason=reason, pillar="lineage")


# ── 4. feature_materialization (pillar: ai_infra) ────────────────────────────

def check_feature_materialization(payload, ctx):
    """
    Checks: feature_skew (training-serving skew).
    Tool cost: 2.0 per call.
    Budget-aware: skip if budget < 5 credits.
    Baseline keys: feature_mean_shift_sigma_max
    """
    feature_view = payload.get("feature_view")
    batch_id     = payload.get("batch_id")
    if not feature_view or not batch_id:
        return Verdict(alert=False, pillar="ai_infra", reason="missing ids")

    remaining = ctx.tools.budget_remaining()
    if (isinstance(remaining, (int, float)) and remaining < 5.0):
        return Verdict(alert=False, pillar="ai_infra", reason="budget low")

    drift = ctx.tools.feature_drift(feature_view, batch_id)
    if "error" in drift:
        return Verdict(alert=False, pillar="ai_infra", reason="drift error")

    mean_shift_sigma = drift.get("mean_shift_sigma", 0)

    bl = ctx.baseline
    sigma_max = bl.get("feature_mean_shift_sigma_max", 0.4095)

    flags = []

    # primary threshold (strict baseline: mean ± 3σ derived bound)
    if mean_shift_sigma > sigma_max:
        flags.append("feature_skew")

    # online z-score — catches subtle persistent skew accumulation over time
    # Use stricter threshold (3.5) to avoid false positives
    _push(ctx.state, "feat_sigma", mean_shift_sigma)
    hist_sigma = ctx.state["feat_sigma"][:-1]
    if abs(_zscore(mean_shift_sigma, hist_sigma)) > 3.5 and not flags:
        flags.append("online_feature_anomaly")

    alert = len(flags) > 0
    reason = "; ".join(flags) if flags else "clean"
    return Verdict(alert=alert, confidence=min(1.0, 0.5 + 0.15 * len(flags)),
                   reason=reason, pillar="ai_infra")


# ── 5. embedding_batch (pillar: ai_infra) ────────────────────────────────────

def check_embedding_batch(payload, ctx):
    """
    Checks: embedding_drift, corpus_staleness.
    Tool cost: 2.0 per call.
    Budget-aware: skip if budget < 5 credits.
    Baseline keys: embedding_centroid_shift_max, corpus_avg_doc_age_days_max
    """
    corpus         = payload.get("corpus")
    chunk_batch_id = payload.get("chunk_batch_id")
    if not corpus or not chunk_batch_id:
        return Verdict(alert=False, pillar="ai_infra", reason="missing ids")

    remaining = ctx.tools.budget_remaining()
    if (isinstance(remaining, (int, float)) and remaining < 5.0):
        return Verdict(alert=False, pillar="ai_infra", reason="budget low")

    emb = ctx.tools.embedding_drift(corpus, chunk_batch_id)
    if "error" in emb:
        return Verdict(alert=False, pillar="ai_infra", reason="emb error")

    centroid_shift   = emb.get("centroid_shift", 0)
    avg_doc_age_days = emb.get("avg_doc_age_days", 0)

    bl = ctx.baseline
    shift_max = bl.get("embedding_centroid_shift_max", 0.0435)
    age_max   = bl.get("corpus_avg_doc_age_days_max", 49.7955)

    flags = []

    if centroid_shift > shift_max:
        flags.append("embedding_drift")

    if avg_doc_age_days > age_max:
        flags.append("corpus_staleness")

    # online z-score
    _push(ctx.state, "emb_shift", centroid_shift)
    _push(ctx.state, "emb_age", avg_doc_age_days)
    hist_shift = ctx.state["emb_shift"][:-1]
    hist_age   = ctx.state["emb_age"][:-1]
    if abs(_zscore(centroid_shift, hist_shift)) > 3.0 and "embedding_drift" not in flags:
        flags.append("online_emb_shift_anomaly")
    if abs(_zscore(avg_doc_age_days, hist_age)) > 3.0 and "corpus_staleness" not in flags:
        flags.append("online_doc_age_anomaly")

    alert = len(flags) > 0
    reason = "; ".join(flags) if flags else "clean"
    return Verdict(alert=alert, confidence=min(1.0, 0.5 + 0.15 * len(flags)),
                   reason=reason, pillar="ai_infra")
