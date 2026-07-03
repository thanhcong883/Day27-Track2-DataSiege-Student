import sys, json, math, collections
from pathlib import Path
sys.path.insert(0, 'harness')
import crypto

key = Path('phases/practice.key').read_bytes()
ciphertext = Path('phases/practice_schedule.json.enc').read_bytes()
schedule = crypto.decrypt_schedule(ciphertext, key)
events = schedule['events']
truths = schedule['ground_truth']
labels = schedule['labels']

# Ground truth indexed by (type, ref)
gt_by_key = {(t['type'], t['batch_id_or_ref']): t['gt'] for t in truths}

# Find lineage_run events by sequence
lineage_seqs = set()
for label in labels:
    if label.get('pillar') == 'lineage' and label.get('is_faulty'):
        lineage_seqs.add(label['seq'])
print("Faulty lineage sequences:", sorted(lineage_seqs))

print("\nAll lineage_run events + ground truth:")
for i, ev in enumerate(events):
    if ev['type'] == 'lineage_run':
        payload = ev['payload']
        run_id = payload.get('run_id')
        gt = gt_by_key.get(('lineage_run', run_id), {})
        label = labels[i] if i < len(labels) else {}
        print(f"  seq={i} is_faulty={label.get('is_faulty')} fault_key={label.get('fault_key')}")
        print(f"    payload={payload}")
        print(f"    gt={gt}")
        print()
