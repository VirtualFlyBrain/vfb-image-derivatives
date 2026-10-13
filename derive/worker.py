"""Produce the outputs one batch of folders needs.

  python -m derive.worker --run-dir /out/runs/<run> --batch 00012 [--validate] [--clean]

Each volume.nrrd is read from the share ONCE (copied to scratch); every output is made from that copy and
written next to it as <name>.tmp-* then renamed, so a killed pod never leaves a partial file.
--validate writes outputs under <run-dir>/validate/<root>/<dir>/ instead of the share (comparison runs).
--clean deletes the legacy intermediates the plan listed; without it they are only reported.
Per-output failures are recorded in <run-dir>/results/<batch>.jsonl and do not fail the batch.
"""
import argparse, json, os, shutil, sys, time, traceback
import nrrd
from .common import FILE_MODE, template_path, atomic_write_bytes, check_share_target, SHARE_NAMES
from .scan import ROOTS
from . import wlz as wlzmod
from .pointcloud import point_cloud


def spacing_of(header):
    if 'space directions' in header:
        sd = header['space directions']
        return [float(sd[0][0]), float(sd[1][1]), float(sd[2][2])]
    return [float(s) for s in header['spacings']]


def share_perms(folder):
    """Everything on the image share is 777: the folder and the outputs this worker writes in it."""
    for n in SHARE_NAMES:
        p = os.path.join(folder, n)
        if os.path.isfile(p) and not os.path.islink(p):
            try: os.chmod(p, 0o777)
            except OSError: pass
    try: os.chmod(folder, 0o777)
    except OSError: pass


def process(item, args, scratch, cache_dir):
    root_path = ROOTS[item['root']]
    src = os.path.join(root_path, item['dir'])
    need = item['needs']
    if args.validate:
        out_dir = os.path.join(args.run_dir, 'validate', item['root'], item['dir'])
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = src                     # never create folders on the share; the folder must already exist
        names = {'thumb': ['thumbnail.png', 'thumbnailT.png'], 'wlz': ['volume.wlz'], 'obj': ['volume.obj']}
        for k in need:
            for n in names.get(k, []):
                check_share_target(item['root'], item['dir'], n)
        if not os.path.isdir(src):
            return {'read': 'error: folder missing on the share'}
    res = {}
    local = None
    data = header = None
    try:
        if any(k in need for k in ('thumb', 'wlz')) or need.get('obj') == 'cloud':
            local = os.path.join(scratch, 'volume.nrrd')
            shutil.copyfile(os.path.join(src, 'volume.nrrd'), local)
            if 'wlz' in need or need.get('obj') == 'cloud':
                data, header = nrrd.read(local)
        if 'thumb' in need:
            try:
                from .thumbs import make_thumbnails
                tpl = template_path(ROOTS['data'], os.path.basename(item['dir']))
                make_thumbnails(tpl, local, out_dir, scratch, cache_dir)
                res['thumb'] = 'ok'
            except Exception as e:
                res['thumb'] = f'error: {type(e).__name__}: {e}'
        if 'wlz' in need:
            try:
                wlzmod.make_wlz(local, os.path.join(out_dir, 'volume.wlz'), scratch, data, header)
                res['wlz'] = 'ok'
            except Exception as e:
                msg = getattr(e, 'stderr', b'') or b''
                res['wlz'] = f'error: {type(e).__name__}: {e} {msg[:300]!r}'
        if need.get('obj') == 'cloud':
            try:
                txt = point_cloud(data, spacing_of(header))
                atomic_write_bytes(os.path.join(out_dir, 'volume.obj'), txt.encode())
                res['obj'] = 'ok'
            except Exception as e:
                res['obj'] = f'error: {type(e).__name__}: {e}'
        elif need.get('obj') == 'link':
            try:
                dst = os.path.join(out_dir, 'volume.obj')
                if not os.path.lexists(dst):
                    tmp = dst + f'.tmp-{os.getpid()}'
                    os.symlink('volume_man.obj', tmp)
                    os.replace(tmp, dst)
                res['obj'] = 'ok'
            except Exception as e:
                res['obj'] = f'error: {type(e).__name__}: {e}'
        if 'clean' in need:
            if args.clean and not args.validate:
                gone = []
                for n in need['clean']:
                    p = os.path.join(src, n)
                    try:
                        os.unlink(p); gone.append(n)
                    except FileNotFoundError:
                        pass
                res['clean'] = f'ok: removed {len(gone)}'
            else:
                res['clean'] = 'listed'
        if not args.validate:
            share_perms(out_dir)
    except Exception as e:
        res['read'] = f'error: {type(e).__name__}: {e}'
    finally:
        if local and os.path.exists(local):
            os.unlink(local)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--batch', required=True)
    ap.add_argument('--scratch', default=os.environ.get('SCRATCH', '/scratch'))
    ap.add_argument('--cache', default=os.environ.get('CACHE_DIR', '/out/cache/template-mips'))
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--clean', action='store_true')
    args = ap.parse_args()
    os.makedirs(args.scratch, exist_ok=True)
    rdir = os.path.join(args.run_dir, 'results'); os.makedirs(rdir, exist_ok=True)
    out = os.path.join(rdir, args.batch + '.jsonl')
    done = set()
    if os.path.exists(out):                       # a retried pod resumes where it stopped
        done = {json.loads(l)['key'] for l in open(out) if l.strip()}
    items = [json.loads(l) for l in open(os.path.join(args.run_dir, 'batches', args.batch + '.jsonl'))]
    n_err = 0
    with open(out, 'a') as f:
        for item in items:
            key = f"{item['root']}:{item['dir']}"
            if key in done:
                continue
            t = time.time()
            res = process(item, args, args.scratch, args.cache)
            n_err += sum(1 for v in res.values() if str(v).startswith('error'))
            f.write(json.dumps({'key': key, 'results': res, 'secs': round(time.time() - t, 1)}) + '\n'); f.flush()
            print(key, res, flush=True)
    print(f'batch {args.batch}: {len(items)} folders, {n_err} output errors')


if __name__ == '__main__':
    main()
