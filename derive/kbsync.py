"""Keep kbw's image properties and 'Missing Image' blocks in step with the files (replaces Jenkins CheckImagesExist).

  python -m derive.kbsync --run-dir /out/runs/<run> [--edges edges.json.gz] [--neo4j http://kbw.kbw:7474] [--write]

Reads the run's scan (<run-dir>/folders/*.jsonl.gz) plus the files the derive step reported writing
(<run-dir>/results/*.jsonl), and every alignment edge
  (i:Individual)<-[:depicts]-(:Individual)-[r:in_register_with]->(:Template) with r.folder   (all datasets)
either from --edges (the JSON of a tx/commit export) or live from --neo4j (NEO4J_USER / NEO4J_PASSWORD).

Per edge (folder http://www.virtualflybrain.org/data/VFB/i/... -> scanned dir VFB/i/...):
  nrrd volume.nrrd, swc volume.swc, thumbnail thumbnail.png, wlz volume.wlz, obj volume_man.obj else volume.obj:
    set to [folder + name] when the file exists, removed when it does not; only differences are written.
  block: thumbnail.png or thumbnailT.png missing -> r.block=['Missing Image'] (+ r.missing_since);
         both present and r.block=['Missing Image'] -> block removed.
  An Individual's ['New Images'] block is removed once all its edges were checked, so present alignments go live
  and only the missing ones stay blocked.
Never touched: edges or individuals with any other block value (curator blocks), folders outside a scanned shard,
edges with more than one folder (listed in the report).
Without --write nothing is written: <run-dir>/kbsync/changes.json and summary.json show what would change.
"""
import argparse, base64, glob, gzip, json, os, sys, time, urllib.request
from collections import Counter, defaultdict

PREFIXES = ('http://www.virtualflybrain.org/data/', 'https://www.virtualflybrain.org/data/',
            'http://virtualflybrain.org/data/', 'https://virtualflybrain.org/data/')
PROPS = {'nrrd': 'volume.nrrd', 'swc': 'volume.swc', 'thumbnail': 'thumbnail.png', 'wlz': 'volume.wlz'}
AUTO_EDGE = ['Missing Image']
AUTO_NODE = ['New Images']
EDGE_QUERY = ('MATCH (i:Individual)<-[:depicts]-(:Individual)-[r:in_register_with]->(:Template) WHERE exists(r.folder) '
              'RETURN id(r), i.short_form, r.folder, r.block, i.block, r.nrrd, r.swc, r.thumbnail, r.wlz, r.obj')


def load_scan(run_dir):
    files, shards = {}, set()
    for p in glob.glob(os.path.join(run_dir, 'folders', '*.jsonl.gz')):
        with gzip.open(p, 'rt') as f:
            for line in f:
                r = json.loads(line)
                if r['root'] == 'data':
                    files[r['dir']] = set(r['files'])
                    shards.add('/'.join(r['dir'].split('/')[:3]))
    # outputs written by this run's derive step
    for p in glob.glob(os.path.join(run_dir, 'results', '*.jsonl')):
        for line in open(p):
            r = json.loads(line)
            root, _, d = r['key'].partition(':')
            if root == 'data' and d in files:
                for out, res in r.get('results', {}).items():
                    if str(res).startswith(('ok', 'written', 'linked')):
                        files[d].update({'thumb': ['thumbnail.png', 'thumbnailT.png'], 'wlz': ['volume.wlz'],
                                         'obj': ['volume.obj']}.get(out, []))
    return files, shards


class Neo:
    def __init__(self, url):
        self.url = url.rstrip('/') + '/db/neo4j/tx/commit'
        tok = base64.b64encode(f"{os.environ.get('NEO4J_USER', 'neo4j')}:{os.environ['NEO4J_PASSWORD']}".encode()).decode()
        self.h = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': 'Basic ' + tok}

    def run(self, statements):
        body = json.dumps({'statements': [{'statement': s, 'parameters': p, 'resultDataContents': ['row']}
                                          for s, p in statements]}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, body, self.h), timeout=3600) as r:
            d = json.load(r)
        if d.get('errors'):
            raise RuntimeError(d['errors'])
        return d['results']


def load_edges(a):
    if a.edges:
        d = json.load(gzip.open(a.edges, 'rt') if a.edges.endswith('.gz') else open(a.edges))
        return [x['row'] for x in d['results'][0]['data']]
    return [x['row'] for x in Neo(a.neo4j).run([(EDGE_QUERY, {})])[0]['data']]


def plan(edges, files, shards, today):
    c = Counter(); sets, removes, block_add, block_del = [], defaultdict(list), [], []
    multi, unknown_ind = [], set()
    node_new = {}
    for rid, ind, folders, rblock, iblock, *cur in edges:
        c['edges'] += 1
        if iblock and iblock != AUTO_NODE:
            c['skip_curator_node_block'] += 1; continue
        if iblock == AUTO_NODE:
            node_new.setdefault(ind, True)
        if rblock and rblock != AUTO_EDGE:
            c['skip_curator_edge_block'] += 1; continue
        if len(folders) != 1:
            c['skip_multi_folder'] += 1; multi.append([rid, ind, folders]); unknown_ind.add(ind); continue
        folder = folders[0]
        rel = next((folder[len(p):] for p in PREFIXES if folder.startswith(p)), None)
        rel = rel.rstrip('/') if rel else None
        if not rel or '/'.join(rel.split('/')[:3]) not in shards:
            c['skip_unscanned_folder'] += 1; unknown_ind.add(ind); continue
        have = files.get(rel)
        if have is None:
            c['folder_missing'] += 1; have = set()
        current = dict(zip(['nrrd', 'swc', 'thumbnail', 'wlz', 'obj'], cur))
        want = {k: [folder + n] if n in have else None for k, n in PROPS.items()}
        want['obj'] = [folder + 'volume_man.obj'] if 'volume_man.obj' in have else [folder + 'volume.obj'] if 'volume.obj' in have else None
        new = {}
        for k, v in want.items():
            if v != current[k]:
                if v is None:
                    removes[k].append(rid); c[f'remove_{k}'] += 1
                else:
                    new[k] = v; c[f'set_{k}'] += 1
        if new:
            sets.append({'id': rid, 'props': new})
        present = 'thumbnail.png' in have and 'thumbnailT.png' in have
        if not present and not rblock:
            block_add.append(rid); c['block_add'] += 1
        elif present and rblock == AUTO_EDGE:
            block_del.append(rid); c['block_remove'] += 1
        elif not present:
            c['still_blocked'] += 1
    unblock_nodes = sorted(i for i in node_new if i not in unknown_ind)
    c['node_new_images_remove'] = len(unblock_nodes)
    c['node_new_images_kept'] = len(node_new) - len(unblock_nodes)
    return c, {'set': sets, 'remove': dict(removes), 'block_add': block_add, 'block_remove': block_del,
               'unblock_nodes': unblock_nodes, 'multi_folder': multi[:1000], 'today': today}


def write(neo, ch, batch=5000):
    def chunks(xs):
        for i in range(0, len(xs), batch):
            yield xs[i:i + batch]
    for rows in chunks(ch['set']):
        neo.run([('UNWIND $rows AS row MATCH ()-[r:in_register_with]->() WHERE id(r) = row.id '
                  'AND (r.block IS NULL OR r.block = $auto) SET r += row.props', {'rows': rows, 'auto': AUTO_EDGE})])
    for k, ids in ch['remove'].items():
        assert k in ('nrrd', 'swc', 'thumbnail', 'wlz', 'obj')
        for part in chunks(ids):
            neo.run([(f'UNWIND $ids AS rid MATCH ()-[r:in_register_with]->() WHERE id(r) = rid '
                      f'AND (r.block IS NULL OR r.block = $auto) REMOVE r.{k}', {'ids': part, 'auto': AUTO_EDGE})])
    for part in chunks(ch['block_add']):
        neo.run([('UNWIND $ids AS rid MATCH ()-[r:in_register_with]->() WHERE id(r) = rid AND r.block IS NULL '
                  'SET r.block = $auto, r.missing_since = [$today]', {'ids': part, 'auto': AUTO_EDGE, 'today': ch['today']})])
    for part in chunks(ch['block_remove']):
        neo.run([('UNWIND $ids AS rid MATCH ()-[r:in_register_with]->() WHERE id(r) = rid AND r.block = $auto '
                  'REMOVE r.block, r.missing_since', {'ids': part, 'auto': AUTO_EDGE})])
    for part in chunks(ch['unblock_nodes']):
        neo.run([('UNWIND $ids AS sf MATCH (i:Individual {short_form: sf}) WHERE i.block = $auto REMOVE i.block',
                  {'ids': part, 'auto': AUTO_NODE})])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--edges', help='tx/commit JSON export of EDGE_QUERY (else read live from --neo4j)')
    ap.add_argument('--neo4j', default=os.environ.get('NEO4J_URL', 'http://kbw.kbw:7474'))
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()
    t0 = time.time()
    files, shards = load_scan(a.run_dir)
    edges = load_edges(a)
    c, ch = plan(edges, files, shards, time.strftime('%Y-%m-%d'))
    out = os.path.join(a.run_dir, 'kbsync'); os.makedirs(out, exist_ok=True)
    json.dump(ch, open(os.path.join(out, 'changes.json'), 'w'))
    if a.write:
        write(Neo(a.neo4j), ch); c['written'] = 1
    c['seconds'] = round(time.time() - t0)
    json.dump(dict(c), open(os.path.join(out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(dict(sorted(c.items())), indent=1))


if __name__ == '__main__':
    main()
