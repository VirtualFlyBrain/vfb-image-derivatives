"""Scan one shard of the image store into per-folder file stats.

  python -m derive.scan --root data --shard 0010 --out /out/run/folders/0010.jsonl.gz
  python -m derive.scan --root private --shard . --out /out/run/folders/private.jsonl.gz

data:    <DATA_ROOT>/VFB/i/<shard>/<b>/<VFB_template>/   (DATA_ROOT default /data)
private: <PRIVATE_ROOT>/VFBu_<id>/<VFB_template>/         (PRIVATE_ROOT default /private)

One JSON line per folder: {"root", "dir" (relative to the root), "files": {name: [mtime, size, is_link]}}.
NFS metadata calls are latency-bound, so second-level directories are walked by a thread pool.
"""
import argparse, gzip, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

ROOTS = {'data': os.environ.get('DATA_ROOT', '/data'), 'private': os.environ.get('PRIVATE_ROOT', '/private')}


def folder_record(root_name, root_path, rel):
    files = {}
    try:
        with os.scandir(os.path.join(root_path, rel)) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    is_link = e.is_symlink()
                    if not is_link and not e.is_file(follow_symlinks=False):
                        continue
                    files[e.name] = [round(st.st_mtime, 3), st.st_size, is_link]
                except OSError:
                    continue
    except OSError:
        return None
    return {'root': root_name, 'dir': rel, 'files': files}


def subdirs(path, prefix=None):
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and (prefix is None or e.name.startswith(prefix)))
    except OSError:
        return []


def scan(root_name, shard, out, threads):
    root_path = ROOTS[root_name]
    if root_name == 'data':
        base = os.path.join('VFB', 'i', shard)
        seconds = [os.path.join(base, b) for b in subdirs(os.path.join(root_path, base))]
    else:
        seconds = [u for u in subdirs(root_path, 'VFBu_')]

    def one(second):
        recs = []
        for t in subdirs(os.path.join(root_path, second), 'VFB_'):
            r = folder_record(root_name, root_path, os.path.join(second, t))
            if r is not None:
                recs.append(r)
        return recs

    t0 = time.time(); n = 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with gzip.open(tmp, 'wt') as f, ThreadPoolExecutor(threads) as pool:
        for recs in pool.map(one, seconds):
            for r in recs:
                f.write(json.dumps(r, separators=(',', ':')) + '\n'); n += 1
    os.replace(tmp, out)
    print(f'{root_name}/{shard}: {len(seconds)} dirs, {n} image folders in {time.time() - t0:.0f}s')


def list_shards():
    """Top-level shards of VFB/i (directories only), as a JSON list for Argo withParam."""
    shards = subdirs(os.path.join(ROOTS['data'], 'VFB', 'i'))
    print(json.dumps(shards))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--list-shards', action='store_true')
    ap.add_argument('--root', choices=ROOTS, default='data')
    ap.add_argument('--shard')
    ap.add_argument('--out')
    ap.add_argument('--threads', type=int, default=8)
    a = ap.parse_args()
    if a.list_shards:
        list_shards(); sys.exit(0)
    scan(a.root, a.shard, a.out, a.threads)
