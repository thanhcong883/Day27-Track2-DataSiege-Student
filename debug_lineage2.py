"""Debug: compare verdicts with labels for lineage events."""
import sys, json, math
from pathlib import Path
sys.path.insert(0, 'harness')
import crypto
sys.path.insert(0, 'harness/child_env')
from api import Verdict, SiegeContext, ToolkitProxy
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

# Simulate lineage events only
print("Simulating lineage events:")
print("-" * 70)
for i, ev in enumerate(events):
    if ev['type'] != 'lineage_run':
        continue
    label = labels[i]
    payload = ev['payload']
    run_id = payload.get('run_id')
    declared_inputs = payload.get('inputs', [])

    # Reveal GT then call
    toolkit.reveal(ev['type'], run_id)
    result = toolkit.lineage_graph_slice(run_id, depth=1)
    if 'error' in result:
        print(f"  seq={i} ERROR: {result}")
        continue

    actual_upstream = result.get('actual_upstream', [])
    actual_up_list = actual_upstream if isinstance(actual_upstream, list) else []
    actual_dc = result.get('actual_downstream_count', 0)
    duration_ms = result.get('duration_ms', 0)

    # Detection logic
    flags = []
    dur_max = 5134.9804
    if duration_ms > dur_max:
        flags.append("runtime_anomaly")
    if len(actual_up_list) < len(declared_inputs):
        flags.append("missing_upstream")
    elif actual_upstream is None or len(actual_up_list) == 0:
        flags.append("missing_upstream")
    if actual_dc == 0:
        flags.append("orphan_output")

    alert = len(flags) > 0
    correct = (alert == label['is_faulty'])

    status = "✓ CORRECT" if correct else "✗ WRONG"
    print(f"  seq={i} {status}  is_faulty={label['is_faulty']} fault_key={label['fault_key']}")
    print(f"    declared_inputs={len(declared_inputs)}  actual_upstream={actual_up_list}  dc={actual_dc}  dur={duration_ms:.1f}ms")
    print(f"    flags={flags}  alert={alert}")
    print()
