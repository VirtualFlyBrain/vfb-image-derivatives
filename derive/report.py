"""Summarise a run and update the consecutive-failure counts the planner uses.

  python -m derive.report --run-dir /out/runs/<run> --state-dir /out/state [--no-state]
"""
import argparse, glob, json, os, collections


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--state-dir', required=True)
    ap.add_argument('--no-state', action='store_true', help='validation / dry runs: do not touch failure counts')
    a = ap.parse_args()
    counts = collections.Counter(); errors = collections.Counter(); secs = 0.0
    results = []
    for p in sorted(glob.glob(os.path.join(a.run_dir, 'results', '*.jsonl'))):
        results += [json.loads(l) for l in open(p) if l.strip()]
    for r in results:
        secs += r.get('secs', 0)
        for out, v in r['results'].items():
            ok = not str(v).startswith('error')
            counts[f'{out}_{"ok" if ok else "error"}'] += 1
            if not ok:
                errors[f'{out}: {str(v)[:120]}'] += 1
    try:
        plan = json.load(open(os.path.join(a.run_dir, 'plan', 'summary.json')))['counts']
    except (OSError, ValueError):
        plan = {}
    summary = {'plan': plan, 'results': dict(counts), 'folders_processed': len(results),
               'worker_seconds': round(secs), 'top_errors': errors.most_common(20)}
    json.dump(summary, open(os.path.join(a.run_dir, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summary, indent=1))
    if a.no_state:
        return
    os.makedirs(a.state_dir, exist_ok=True)
    sp = os.path.join(a.state_dir, 'failures.json')
    try:
        fails = json.load(open(sp))
    except (OSError, ValueError):
        fails = {}
    for r in results:
        f = fails.setdefault(r['key'], {})
        for out, v in r['results'].items():
            if str(v).startswith('error'):
                f[out] = f.get(out, 0) + 1
            else:
                f.pop(out, None)
        if not f:
            fails.pop(r['key'])
    tmp = sp + '.tmp'; json.dump(fails, open(tmp, 'w')); os.replace(tmp, sp)


if __name__ == '__main__':
    main()
