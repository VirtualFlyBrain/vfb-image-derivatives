"""Load Janelia FlyLight H5J stacks into the VFB image store (replaces Jenkins JaneliaWorkstationPipeline).

  python -m janelia.load plan   --processing /processing --run-dir /out/runs/<wf> [--batch-size 25]
  python -m janelia.load run    --processing /processing --run-dir /out/runs/<wf> --batch <name>
  python -m janelia.load report --run-dir /out/runs/<wf>

The H5J files arrive in the NAS Processing/ folder from fetch_batch.py (it needs the Janelia VPN, so it runs
off-cluster). Each one becomes volume.nrrd (or volume.nrrd + volume_R/G/B.nrrd for 3-signal images) in its KB
image folder, warped onto JRC2018U / JRCVNC2018U with the baked maps of warp_fields.py - the same decode, warp
and NRRD as the Jenkins job. Differences from the Jenkins job:
  * alignment spaces are mapped explicitly (SPACES); any other space (JFRC2010, the 2017 VNC template, a
    'primary' alignment in an older space) is refused and listed, never written unwarped as if it were unisex;
  * every volume must come out on the target template's grid (KB dims) before anything is written;
  * writes only <data>/VFB/i/xxxx/xxxx/VFB_xxxxxxxx/volume*.nrrd (the folder may be created), atomically,
    files and folder 777;
  * folders come from the KB (kbw) by the image key, as the Jenkins job did for H5Js missing from its JSON.
The H5J is deleted once its image is written (or already there); unreadable, unsupported and unmapped files stay.
Derivatives (thumbnails, WLZ, OBJ) and the KB unblock follow from the nightly image-derivatives run.
"""
import argparse, base64, glob, json, os, re, shutil, sys, time, traceback, urllib.request
import numpy as np
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # the vendored modules import each other by bare name

DATA_ROOT = os.environ.get('DATA_ROOT', '/data')
FOLDER_PREFIX = 'http://www.virtualflybrain.org/data/'
SHARE_DIR = re.compile(r'^VFB/i/[^/]{4}/[^/]{4}/VFB_[^/]{8}$')
RGB_NAMES = ('volume.nrrd', 'volume_R.nrrd', 'volume_G.nrrd', 'volume_B.nrrd')

# alignment_space (H5J attribute, also the REG_ suffix of the file name) -> (source, target) template symbol
SPACE_RE = re.compile(r'^JRC2018_(VNC_)?(Unisex|FEMALE|MALE)(_.*)?$', re.I)
SOURCE = {('', 'unisex'): 'JRC2018U', ('', 'female'): 'JRC2018F', ('', 'male'): 'JRC2018M',
          ('vnc', 'unisex'): 'JRCVNC2018U', ('vnc', 'female'): 'JRCVNC2018F', ('vnc', 'male'): 'JRCVNC2018M'}


# Older Janelia 'primary' alignment: JFRC2010 brain (1024x512x218). The stack is on the JFRC2 pixel grid, whose
# calibration is 0.622088 um isotropic (flybrains' JFRC2); the H5J's own voxel_size says a rounded 0.62.
OLD_BRAIN_SPACE = re.compile(r'^JFRC2010_20x$')
SOURCE_VOXEL = {'JFRC2': [0.622088, 0.622088, 0.622088]}


def templates_for(space):
    if OLD_BRAIN_SPACE.match(space or ''):
        return 'JFRC2', 'JRC2018U'
    m = SPACE_RE.match(space or '')
    if not m:
        return None
    vnc = 'vnc' if m.group(1) else ''
    return SOURCE[(vnc, m.group(2).lower())], ('JRCVNC2018U' if vnc else 'JRC2018U')


def patch_h5j_info(p):
    """Make the vendored processor use templates_for() for the source/target and the calibrated voxel size.

    Its own read_h5j_info maps every space without FEMALE/MALE to the unisex template (no warp), which is how
    JFRC2010 and 2017-VNC stacks were once written unwarped. Spaces templates_for() refuses never get this far
    (plan() skips them); this makes the processor agree with the plan for the ones it accepts."""
    orig = p.read_h5j_info

    def read_h5j_info(path):
        info = orig(path)
        pair = templates_for(info['alignment_space'])
        if pair is None:
            raise ValueError(f"unsupported alignment space {info['alignment_space']}")
        info['source_template'], info['target_template'] = pair
        if pair[0] in SOURCE_VOXEL:
            info['voxel_size'] = np.asarray(SOURCE_VOXEL[pair[0]], dtype=float)
        return info
    p.read_h5j_info = read_h5j_info


class Neo:
    def __init__(self, url):
        self.url = url.rstrip('/') + '/db/neo4j/tx/commit'
        user, pw = os.environ.get('NEO4J_USER', 'neo4j'), os.environ.get('NEO4J_PASSWORD')
        if pw is None:
            user, _, pw = os.environ['NEO4J_AUTH'].partition('/')
        self.h = {'Content-Type': 'application/json', 'Accept': 'application/json',
                  'Authorization': 'Basic ' + base64.b64encode(f'{user}:{pw}'.encode()).decode()}

    def rows(self, statement, **params):
        body = json.dumps({'statements': [{'statement': statement, 'parameters': params,
                                           'resultDataContents': ['row']}]}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, body, self.h), timeout=600) as r:
            d = json.load(r)
        if d.get('errors'):
            raise RuntimeError(d['errors'])
        return [x['row'] for x in d['results'][0]['data']]


def template_specs(neo, symbols):
    """{symbol: {'id', 'dims', 'voxdims'}} from the KB, same arithmetic as get_vfb_template_specs."""
    out = {}
    for sym, sf, extent, voxel in neo.rows(
            'UNWIND $s AS s MATCH (t:Template)<-[:depicts]-(tc:Template)-[r:in_register_with]->(tc) '
            'WHERE s IN t.symbol RETURN s, t.short_form, r.extent[0], r.voxel[0]', s=list(symbols)):
        e, v = json.loads(extent), json.loads(voxel)
        out[sym] = {'id': sf, 'voxdims': [v['X'], v['Y'], v['Z']],
                    'dims': [round(e[k] - v[k]) for k in 'XYZ']}
    return out


def kb_folders(neo):
    """{'20x-Brain-<sample>': {template_id: [(folder, block), ...]}} - as jenkins_pipeline.kb_folder_index."""
    index = {}
    for fn, folder, block in neo.rows(
            "MATCH (:Individual)<-[:depicts]-(:Individual)-[r:in_register_with]->(:Template) "
            "WHERE exists(r.filename) AND exists(r.folder) AND r.filename[0] =~ '(20x|40x|63x)-(Brain|VNC)-[0-9]+' "
            "RETURN r.filename[0], r.folder[0], CASE WHEN exists(r.block) THEN r.block[0] ELSE null END"):
        index.setdefault(fn, {}).setdefault(folder.rstrip('/').split('/')[-1], []).append((folder, block))
    return index


def choose_folder(regs, template_id):
    if not regs:
        return None, 'not registered in the KB'
    cands = regs.get(template_id)
    if cands is None:
        return None, f'registered to {sorted(regs)}, not {template_id}'
    if len(cands) == 1:
        return cands[0][0], None
    waiting = [f for f, b in cands if b == 'Missing Image']
    if len(waiting) == 1:
        return waiting[0], None
    return None, 'duplicate KB individuals ' + ', '.join(f.rstrip('/').split('/')[-2] for f, _ in cands)


def h5j_key(stem):
    p = stem.split('-')
    return '-'.join(p[1:4]) if len(p) >= 4 and p[0] == 'Janelia' else None


def rel_dir(folder):
    rel = folder[len(FOLDER_PREFIX):].rstrip('/') if folder.startswith(FOLDER_PREFIX) else None
    return rel if rel and SHARE_DIR.match(rel) else None


def plan(a):
    import process_h5j_simple as p
    neo = Neo(a.neo4j)
    files = sorted(glob.glob(os.path.join(a.processing, '*.h5j')))
    c, skipped, items = Counter(), [], []
    specs = template_specs(neo, ['JRC2018U', 'JRCVNC2018U'])
    index = kb_folders(neo) if files else {}
    for path in files:
        stem = os.path.basename(path)[:-4]
        c['h5j'] += 1
        try:
            info = p.read_h5j_info(path)
        except Exception as e:
            c['unreadable'] += 1; skipped.append([stem, f'unreadable: {type(e).__name__}: {str(e)[:160]}']); continue
        pair = templates_for(info['alignment_space'])
        if not pair:
            c['unsupported_space'] += 1; skipped.append([stem, f"unsupported alignment space {info['alignment_space']}"]); continue
        target = specs[pair[1]]
        folder, why = choose_folder(index.get(h5j_key(stem) or '', {}), target['id'])
        if folder is None:
            c['unmapped'] += 1; skipped.append([stem, why]); continue
        rel = rel_dir(folder)
        if rel is None:
            c['bad_folder'] += 1; skipped.append([stem, f'folder outside VFB/i/xxxx/xxxx/VFB_xxxxxxxx: {folder}']); continue
        items.append({'stem': stem, 'dir': rel, 'source': pair[0], 'target': pair[1],
                      'space': info['alignment_space'], 'signals': len(info['signal_channels'])})
    os.makedirs(os.path.join(a.run_dir, 'batches'), exist_ok=True)
    names = []
    for pair in sorted({(i['source'], i['target']) for i in items}):
        group = [i for i in items if (i['source'], i['target']) == pair]
        for n in range(0, len(group), a.batch_size):
            name = f'{pair[0]}-{n // a.batch_size:05d}'
            with open(os.path.join(a.run_dir, 'batches', name + '.jsonl'), 'w') as f:
                for i in group[n:n + a.batch_size]:
                    f.write(json.dumps(i) + '\n')
            names.append(name)
    c['to_load'] = len(items); c['batches'] = len(names)
    json.dump({'counts': c, 'templates': specs, 'skipped': skipped},
              open(os.path.join(a.run_dir, 'plan.json'), 'w'), indent=1)
    print(json.dumps(dict(c)), file=sys.stderr)
    print(json.dumps(names))


def atomic_nrrd(p, path, data, header):
    tmp = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + f'.tmp-{os.getpid()}')
    try:
        p.write_nrrd(tmp, data, header)
        os.chmod(tmp, 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_one(p, item, fields, specs, a):
    path = os.path.join(a.processing, item['stem'] + '.h5j')
    dest = os.path.join(DATA_ROOT, item['dir'])
    if not SHARE_DIR.match(item['dir']):
        return 'refused: folder pattern'
    rgb = item['signals'] == 3
    names = RGB_NAMES if rgb else RGB_NAMES[:1]
    if not a.overwrite and all(os.path.exists(os.path.join(dest, n)) for n in names):
        os.unlink(path)
        return 'exists'
    info = p.read_h5j_info(path)
    sig = info['signal_channels']
    if not sig:
        return 'failed: no signal channels'
    if rgb:
        subset = sig
    else:
        k = int(a.channel) - 1 if a.channel.isdigit() and 0 < int(a.channel) <= len(sig) else 0
        subset = [sig[k]]
    target = specs[item['target']]
    if item['source'] != item['target']:
        p._field_cache[(item['source'], item['target'])] = fields[(item['source'], item['target'])]
    vols = p.process_h5j_file(path, process_reference=False, process_signal=True, signal_subset=subset, write=False)
    for name, v in vols.items():
        if list(v.shape) != target['dims']:
            return f"failed: {name} came out {list(v.shape)}, template {item['target']} is {target['dims']}"
    voxdims = fields[(item['source'], item['target'])].voxdims if item['source'] != item['target'] else target['voxdims']
    header = {'space directions': np.diag(voxdims), 'space origin': [0, 0, 0], 'kinds': ['domain'] * 3,
              'space dimension': 3, 'space units': ['microns'] * 3, 'encoding': 'gzip'}
    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
    try:
        if (os.stat(dest).st_mode & 0o777) != 0o777:
            os.chmod(dest, 0o777)
    except PermissionError:
        pass  # dir owned by another uid (pre-existing); only matters if it is not already writable
    if not os.access(dest, os.W_OK):
        return f'failed: {dest} is not writable'
    if rgb:
        r, g, b = (vols[f'signal_{i}'] for i in range(3))
        for n, d in zip(RGB_NAMES, (np.maximum(np.maximum(r, g), b), r, g, b)):
            atomic_nrrd(p, os.path.join(dest, n), d, header)
    else:
        atomic_nrrd(p, os.path.join(dest, 'volume.nrrd'), next(iter(vols.values())), header)
    os.unlink(path)
    return 'written'


def run(a):
    os.environ.setdefault('SIGNAL_THRESHOLD', str(a.signal_threshold))
    import process_h5j_simple as p
    import warp_fields
    patch_h5j_info(p)
    items = [json.loads(l) for l in open(os.path.join(a.run_dir, 'batches', a.batch + '.jsonl'))]
    specs = json.load(open(os.path.join(a.run_dir, 'plan.json')))['templates']
    fields = {}
    for pair in {(i['source'], i['target']) for i in items if i['source'] != i['target']}:
        npy, js = warp_fields.field_paths(*pair, directory=a.warp_dir)
        if not os.path.exists(npy) and a.extra_warp_dir:       # one-off maps baked into the pipeline-out volume
            npy, js = warp_fields.field_paths(*pair, directory=a.extra_warp_dir)
        local = os.path.join(a.scratch, os.path.basename(npy))
        if not os.path.exists(local):            # one copy per pod onto its Longhorn scratch, then memory-mapped
            t = time.time()
            shutil.copyfile(js, local[:-4] + '.json'); shutil.copyfile(npy, local + '.part'); os.replace(local + '.part', local)
            print(f'copied {os.path.basename(npy)} in {time.time() - t:.0f} s', flush=True)
        f = warp_fields.WarpField(local)
        t = specs[pair[1]]
        if not f.matches(t['dims'], t['voxdims']):
            raise SystemExit(f'{local}: grid {f.dims} {f.voxdims} != KB {pair[1]} {t["dims"]} {t["voxdims"]}')
        fields[pair] = f
    out = os.path.join(a.run_dir, 'results', a.batch + '.jsonl')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    done = {json.loads(l)['stem'] for l in open(out)} if os.path.exists(out) else set()
    with open(out, 'a') as fh:
        for item in items:
            if item['stem'] in done:
                continue
            t = time.time()
            try:
                res = load_one(p, item, fields, specs, a) if os.path.exists(
                    os.path.join(a.processing, item['stem'] + '.h5j')) else 'gone: H5J no longer in Processing'
            except Exception as e:
                traceback.print_exc()
                res = f'failed: {type(e).__name__}: {str(e)[:200]}'
            fh.write(json.dumps({'stem': item['stem'], 'dir': item['dir'], 'result': res, 'secs': round(time.time() - t)}) + '\n')
            fh.flush()
            print(item['stem'], res, flush=True)


def report(a):
    plan_ = json.load(open(os.path.join(a.run_dir, 'plan.json')))
    c, bad = Counter(), []
    for p in glob.glob(os.path.join(a.run_dir, 'results', '*.jsonl')):
        for l in open(p):
            r = json.loads(l)
            c[r['result'].split(':')[0]] += 1
            if not r['result'].startswith(('written', 'exists')):
                bad.append(r)
    s = {'plan': plan_['counts'], 'results': c, 'skipped_in_plan': plan_['skipped'][:2000], 'failed': bad[:2000]}
    json.dump(s, open(os.path.join(a.run_dir, 'summary.json'), 'w'), indent=1)
    print(json.dumps({'plan': plan_['counts'], 'results': c}, indent=1))
    for r in bad[:50]:
        print(r['stem'], r['result'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['plan', 'run', 'report'])
    ap.add_argument('--processing', default='/processing')
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--batch')
    ap.add_argument('--batch-size', type=int, default=25)
    ap.add_argument('--warp-dir', default=os.environ.get('WARP_FIELD_DIR', '/warp'))
    ap.add_argument('--extra-warp-dir', default=os.environ.get('EXTRA_WARP_DIR', '/out/warp'))
    ap.add_argument('--scratch', default='/scratch')
    ap.add_argument('--channel', default=os.environ.get('CHANNEL', '2'))
    ap.add_argument('--signal-threshold', type=int, default=int(os.environ.get('SIGNAL_THRESHOLD', '16')))
    ap.add_argument('--overwrite', action='store_true')
    ap.add_argument('--neo4j', default=os.environ.get('NEO4J_URL', 'http://kbw.kbw:7474'))
    a = ap.parse_args()
    {'plan': plan, 'run': run, 'report': report}[a.cmd](a)


if __name__ == '__main__':
    main()
