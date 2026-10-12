"""Decide what each image folder needs and write the work as batches.

  python -m derive.plan --run-dir /out/runs/<run> --state-dir /out/state --batch-size 200 [--outputs thumb,wlz,obj,clean]

Reads <run-dir>/folders/*.jsonl.gz, writes <run-dir>/plan/summary.json, <run-dir>/batches/NNNNN.jsonl and
prints the batch names as JSON (for Argo withParam).

Rules (per folder with a non-empty volume.nrrd):
  thumb  thumbnail.png or thumbnailT.png missing, or older than volume.nrrd; needs the template's template.nrrd
  wlz    volume.wlz missing, empty or older than volume.nrrd
  obj    volume.obj missing: link to volume_man.obj if that exists, else a point cloud;
         a real (non-link) volume.obj older than volume.nrrd is regenerated; a link is never replaced
  clean  legacy intermediates (volume_bounded*, *_th.wlz) older than 2 days, or older than volume.wlz
Folders whose output failed on 3 consecutive runs (state/failures.json) are skipped and listed.
"""
import argparse, glob, gzip, json, os, time

INTERMEDIATES = ('volume_bounded.nrrd', 'volume_bounded.tif', 'volume_bounded.wlz', 'volume_bounded_th.wlz')
CLEAN_AGE = 2 * 86400
MAX_FAILS = 3


def needs_for(rec, templates, now, outputs):
    f = rec['files']
    vol = f.get('volume.nrrd')
    need = {}
    if vol and vol[1] > 0 and not vol[2]:
        vm = vol[0]
        tid = os.path.basename(rec['dir'])
        if 'thumb' in outputs:
            t, tt = f.get('thumbnail.png'), f.get('thumbnailT.png')
            if not t or not tt or t[0] < vm or tt[0] < vm:
                need['thumb'] = tid in templates or 'no-template'
        if 'wlz' in outputs:
            w = f.get('volume.wlz')
            if not w or w[1] == 0 or w[0] < vm:
                need['wlz'] = True
        if 'obj' in outputs:
            o, m = f.get('volume.obj'), f.get('volume_man.obj')
            if not o:
                need['obj'] = 'link' if m else 'cloud'
            elif not o[2] and o[0] < vm:
                need['obj'] = 'cloud'
    if 'clean' in outputs:
        wl = f.get('volume.wlz')
        old = [n for n in INTERMEDIATES if n in f and (now - f[n][0] > CLEAN_AGE or (wl and wl[1] > 0 and wl[0] > f[n][0]))]
        if old:
            need['clean'] = old
    return need


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--state-dir', required=True)
    ap.add_argument('--batch-size', type=int, default=200)
    ap.add_argument('--outputs', default='thumb,wlz,obj,clean')
    ap.add_argument('--sample', type=int, default=0,
                    help='validation: pick N folders that already have every output (spread across shards) and '
                         'regenerate all of them (run the worker with --validate)')
    a = ap.parse_args()
    outputs = set(a.outputs.split(','))
    now = time.time()
    recs = []
    for p in sorted(glob.glob(os.path.join(a.run_dir, 'folders', '*.jsonl.gz'))):
        with gzip.open(p, 'rt') as f:
            recs.extend(json.loads(l) for l in f)
    templates = {os.path.basename(r['dir']) for r in recs if r['root'] == 'data' and 'template.nrrd' in r['files']}
    try:
        fails = json.load(open(os.path.join(a.state_dir, 'failures.json')))
    except (OSError, ValueError):
        fails = {}

    counts = {'folders': len(recs), 'with_volume': 0, 'thumb': 0, 'wlz': 0, 'obj_cloud': 0, 'obj_link': 0,
              'clean_files': 0, 'no_template': 0, 'skipped_failing': 0}
    work, skipped, no_template = [], [], []
    if a.sample:
        full = [r for r in recs if r['root'] == 'data' and os.path.basename(r['dir']) in templates
                and all(n in r['files'] and not r['files'][n][2] and r['files'][n][1] > 0
                        for n in ('volume.nrrd', 'volume.wlz', 'volume.obj', 'thumbnail.png', 'thumbnailT.png'))
                and all(r['files'][n][0] >= r['files']['volume.nrrd'][0] for n in ('volume.wlz', 'thumbnail.png'))]
        by_shard = {}
        for r in full:
            by_shard.setdefault((r['dir'].split('/')[2], os.path.basename(r['dir'])), []).append(r)
        import random
        random.seed(12)
        groups = list(by_shard.values())
        while len(work) < a.sample and any(groups):
            for g in groups:
                if g and len(work) < a.sample:
                    r = g.pop(random.randrange(len(g)))
                    work.append({'root': r['root'], 'dir': r['dir'], 'needs': {'thumb': True, 'wlz': True, 'obj': 'cloud'}})
        recs = []
        counts['sample'] = len(work)
    for r in recs:
        if 'volume.nrrd' in r['files']:
            counts['with_volume'] += 1
        need = needs_for(r, templates, now, outputs)
        key = f"{r['root']}:{r['dir']}"
        for out in list(need):
            if fails.get(key, {}).get(out, 0) >= MAX_FAILS:
                need.pop(out); skipped.append(f'{key} {out}'); counts['skipped_failing'] += 1
        if need.get('thumb') == 'no-template':
            need.pop('thumb'); no_template.append(key); counts['no_template'] += 1
        if not need:
            continue
        counts['thumb'] += 'thumb' in need; counts['wlz'] += 'wlz' in need
        counts['obj_cloud'] += need.get('obj') == 'cloud'; counts['obj_link'] += need.get('obj') == 'link'
        counts['clean_files'] += len(need.get('clean', []))
        work.append({'root': r['root'], 'dir': r['dir'], 'needs': need})

    os.makedirs(os.path.join(a.run_dir, 'plan'), exist_ok=True)
    bdir = os.path.join(a.run_dir, 'batches'); os.makedirs(bdir, exist_ok=True)
    # heavy work first so the long batches start early
    work.sort(key=lambda w: (-len(w['needs']), w['root'], w['dir']))
    names = []
    for i in range(0, len(work), a.batch_size):
        name = f'{i // a.batch_size:05d}'
        with open(os.path.join(bdir, name + '.jsonl'), 'w') as f:
            for w in work[i:i + a.batch_size]:
                f.write(json.dumps(w, separators=(',', ':')) + '\n')
        names.append(name)
    counts['work_folders'] = len(work); counts['batches'] = len(names)
    json.dump({'counts': counts, 'skipped_failing': skipped[:1000], 'no_template': no_template[:1000]},
              open(os.path.join(a.run_dir, 'plan', 'summary.json'), 'w'), indent=1)
    with open(os.path.join(a.run_dir, 'plan', 'clean.tsv'), 'w') as f:
        for w in work:
            for n in w['needs'].get('clean', []):
                f.write(f"{w['root']}\t{w['dir']}/{n}\n")
    print(json.dumps(names))
    import sys; print(json.dumps(counts), file=sys.stderr)


if __name__ == '__main__':
    main()
