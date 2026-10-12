"""Compare a --validate run's outputs with the files already on the share.

  python -m derive.compare --run-dir /out/runs/<run>

thumbnail.png / thumbnailT.png: identical size and pixels (max abs difference, share of differing pixels)
volume.wlz: identical `WlzFacts` (file names removed)
volume.obj: point counts (vertices are jittered ±1 voxel at random, so only the count can be exact)
Writes <run-dir>/compare.json and prints a summary.
"""
import argparse, glob, json, os, re
import numpy as np
from PIL import Image
from .scan import ROOTS
from .wlz import wlz_facts


def img_diff(a, b):
    A, B = np.asarray(Image.open(a)), np.asarray(Image.open(b))
    if A.shape != B.shape:
        return {'same': False, 'why': f'shape {A.shape} vs {B.shape}'}
    d = np.abs(A.astype(int) - B.astype(int))
    return {'same': int(d.max()) == 0, 'max': int(d.max()), 'frac': round(float((d.max(axis=-1) if d.ndim == 3 else d).astype(bool).mean()), 6)}


def facts(p):
    return re.sub(r'\S*\.wlz', '', wlz_facts(p)).strip()


def vcount(p):
    with open(p, 'rb') as f:
        return sum(1 for l in f if l.startswith(b'v '))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--run-dir', required=True); a = ap.parse_args()
    rows = []
    vroot = os.path.join(a.run_dir, 'validate')
    for new_dir in sorted(glob.glob(os.path.join(vroot, '*', 'VFB', 'i', '*', '*', '*')) + glob.glob(os.path.join(vroot, 'private', '*', '*'))):
        root = os.path.relpath(new_dir, vroot).split(os.sep)[0]
        rel = os.path.relpath(new_dir, os.path.join(vroot, root))
        old_dir = os.path.join(ROOTS[root], rel)
        row = {'dir': rel}
        for n in ('thumbnail.png', 'thumbnailT.png'):
            if os.path.exists(os.path.join(new_dir, n)):
                row[n] = img_diff(os.path.join(old_dir, n), os.path.join(new_dir, n))
        if os.path.exists(os.path.join(new_dir, 'volume.wlz')):
            fo, fn = facts(os.path.join(old_dir, 'volume.wlz')), facts(os.path.join(new_dir, 'volume.wlz'))
            row['volume.wlz'] = {'same': fo == fn, **({} if fo == fn else {'old': fo[:600], 'new': fn[:600]})}
        if os.path.exists(os.path.join(new_dir, 'volume.obj')):
            co, cn = vcount(os.path.join(old_dir, 'volume.obj')), vcount(os.path.join(new_dir, 'volume.obj'))
            row['volume.obj'] = {'same': co == cn, 'old': co, 'new': cn}
        rows.append(row)
    tally = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, dict):
                t = tally.setdefault(k, {'same': 0, 'diff': 0}); t['same' if v['same'] else 'diff'] += 1
    json.dump({'tally': tally, 'rows': rows}, open(os.path.join(a.run_dir, 'compare.json'), 'w'), indent=1)
    print(json.dumps(tally, indent=1))
    for r in rows:
        bad = {k: v for k, v in r.items() if isinstance(v, dict) and not v['same']}
        if bad:
            print(r['dir'], json.dumps(bad)[:400])


if __name__ == '__main__':
    main()
