"""
Debug: find which clean events are being incorrectly flagged (false positives).
Compare verdicts from the latest practice_report against the answer key.
"""
import sys, json, math
from pathlib import Path
sys.path.insert(0, 'harness')
import crypto
sys.path.insert(0, 'harness/child_env')
from api import SiegeContext, ToolkitProxy, Verdict
from toolkit.metering import ServerToolkit

key = Path('phases/practice.key').read_bytes()
ciphertext = Path('phases/practice_schedule.json.enc').read_bytes()
schedule = crypto.decrypt_schedule(ciphertext, key)
events = schedule['events']
truths = schedule['ground_truth']
labels = schedule['labels']
gt_by_key = {(t['type'], t['batch_id_or_ref']): t['gt'] for t in truths}

BUDGET = 220.0
toolkit = ServerToolkit(gt_by_key, BUDGET)

# Simulate ALL events inline with the same logic as defense.py
# Import the defense module logic here
state = {}
history = {}

def push(key, value, max_len=30):
    if key not in state:
        state[key] = []
    state[key].append(value)
    if len(state[key]) > max_len:
        state[key] = state[key][-max_len:]

def zscore(value, hist):
    if len(hist) < 3:
        return 0.0
    m = sum(hist) / len(hist)
    v = sum((x-m)**2 for x in hist) / (len(hist)-1)
    s = math.sqrt(v) if v > 0 else 0.0
    if s == 0:
        return 0.0
    return (value - m) / s

import json as _json
baseline = _json.loads(Path('data/baselines.json').read_text())

false_positives = []
false_negatives = []

for i, ev in enumerate(events):
    label = labels[i]
    etype = ev['type']
    payload = ev['payload']
    
    # Reveal GT
    ref = payload.get('batch_id') or payload.get('checkpoint_batch_id') or payload.get('run_id') or payload.get('chunk_batch_id')
    toolkit.reveal(etype, ref)
    
    flags = []
    
    if etype == 'data_batch':
        prof = toolkit.batch_profile(payload['batch_id'])
        if 'error' not in prof:
            rc = prof['row_count']
            nr = prof['null_rate']['customer_id']
            ma = prof['mean_amount']
            st = prof['staleness_min']
            if st > baseline['staleness_min_max']: flags.append('freshness_lag')
            if rc < baseline['row_count_min'] or rc > baseline['row_count_max']: flags.append('volume_spike')
            if nr > baseline['null_rate_max']: flags.append('null_spike')
            if ma < baseline['mean_amount_min'] or ma > baseline['mean_amount_max']: flags.append('distribution_shift')
            push('batch_rc', rc); push('batch_ma', ma); push('batch_nr', nr); push('batch_stale', st)
            h_rc = state['batch_rc'][:-1]; h_ma = state['batch_ma'][:-1]
            h_nr = state['batch_nr'][:-1]; h_st = state['batch_stale'][:-1]
            if abs(zscore(rc, h_rc)) > 3.5 and 'volume_spike' not in flags: flags.append('online_volume')
            if abs(zscore(ma, h_ma)) > 3.5 and 'distribution_shift' not in flags: flags.append('online_dist')
            if abs(zscore(nr, h_nr)) > 3.5 and 'null_spike' not in flags: flags.append('online_null')
            if abs(zscore(st, h_st)) > 3.0 and 'freshness_lag' not in flags: flags.append('online_stale')
    
    elif etype == 'contract_checkpoint':
        diff = toolkit.contract_diff(payload['contract_id'], payload['checkpoint_batch_id'])
        if 'error' not in diff:
            flags.extend(diff.get('violations', []))
            if diff['freshness_delay_min'] > baseline['freshness_delay_max_min']: flags.append('freshness_sla')
    
    elif etype == 'lineage_run':
        graph = toolkit.lineage_graph_slice(payload['run_id'], depth=1)
        if 'error' not in graph:
            dur = graph['duration_ms']
            up = graph['actual_upstream'] or []
            up = up if isinstance(up, list) else []
            dc = graph['actual_downstream_count']
            job_key = 'max_up_' + payload.get('job', '?')
            if job_key not in state: state[job_key] = 0
            if len(up) > state[job_key]: state[job_key] = len(up)
            if dur > baseline['lineage_duration_ms_max']: flags.append('runtime_anomaly')
            if len(up) < state[job_key]: flags.append('missing_upstream')
            elif len(up) == 0: flags.append('missing_upstream')
            if dc == 0: flags.append('orphan_output')
            push('ldur', dur)
            if abs(zscore(dur, state['ldur'][:-1])) > 3.0 and 'runtime_anomaly' not in flags: flags.append('online_runtime')
    
    elif etype == 'feature_materialization':
        remaining = toolkit.budget_remaining()
        if remaining >= 5.0:
            drift = toolkit.feature_drift(payload['feature_view'], payload['batch_id'])
            if 'error' not in drift:
                sigma = drift['mean_shift_sigma']
                smax = baseline['feature_mean_shift_sigma_max']
                if sigma > smax: flags.append('feature_skew')
                if smax * 0.75 < sigma <= smax: flags.append('subtle_feature_skew')
                push('fsig', sigma)
                if abs(zscore(sigma, state['fsig'][:-1])) > 2.5 and not flags: flags.append('online_feat')
    
    elif etype == 'embedding_batch':
        remaining = toolkit.budget_remaining()
        if remaining >= 5.0:
            emb = toolkit.embedding_drift(payload['corpus'], payload['chunk_batch_id'])
            if 'error' not in emb:
                cs = emb['centroid_shift']
                age = emb['avg_doc_age_days']
                if cs > baseline['embedding_centroid_shift_max']: flags.append('embedding_drift')
                if age > baseline['corpus_avg_doc_age_days_max']: flags.append('corpus_staleness')
                push('ecs', cs); push('eage', age)
                if abs(zscore(cs, state['ecs'][:-1])) > 3.0 and 'embedding_drift' not in flags: flags.append('online_emb')
                if abs(zscore(age, state['eage'][:-1])) > 3.0 and 'corpus_staleness' not in flags: flags.append('online_age')
    
    alert = len(flags) > 0
    is_faulty = label['is_faulty']
    
    if alert and not is_faulty:
        false_positives.append((i, etype, payload, flags))
    if not alert and is_faulty:
        false_negatives.append((i, etype, payload, label['fault_key']))

print("=== FALSE POSITIVES (clean events flagged) ===")
for seq, etype, payload, flags in false_positives:
    print(f"  seq={seq} type={etype} flags={flags}")
    # Get actual profile values for debugging
    ref2 = payload.get('batch_id') or payload.get('checkpoint_batch_id') or payload.get('run_id') or payload.get('chunk_batch_id')
    gt = gt_by_key.get((etype, ref2), {})
    print(f"  payload ref={ref2}  gt={gt}")
    print()

print("=== FALSE NEGATIVES (faulty events missed) ===")
for seq, etype, payload, fk in false_negatives:
    print(f"  seq={seq} type={etype} fault_key={fk}")
