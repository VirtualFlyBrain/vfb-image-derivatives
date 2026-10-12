"""volume.wlz from volume.nrrd, equivalent to Bound.py -> Fiji nrrd2tif -> WlzExtFFConvert -> WlzThreshold
-> WlzSetVoxelSize, without the TIFF step and without writing intermediates to the share.

Boundary markers (Robbie1977/Bound, `Bound.py 3 in out`): voxels [0,0,0] and [max,max,max] are raised to
>= 3 so they survive `WlzThreshold -v2 -H` and the object spans the whole volume (VFB cannot handle an
offset corner)."""
import os, subprocess, nrrd
from .common import WOOLZ_BIN, atomic_copy

MARKER = 3


def header_voxel_args(nrrd_path):
    """-x -y -z exactly as CreateWLZ derived them: the 'space directions:' header line, `cut -c 20-`,
    brackets/commas -> spaces, minus signs removed, items 0/4/8, first 9 characters of each."""
    with open(nrrd_path, 'rb') as f:
        for raw in f:
            line = raw.decode('latin-1').rstrip('\n')
            if line == '':
                break
            if 'space directions:' in line:
                rest = line[19:]
                for ch in '(),':
                    rest = rest.replace(ch, ' ')
                v = rest.replace('-', '').split()
                return [f'-x{v[0][:9]}', f'-y{v[4][:9]}', f'-z{v[8][:9]}']
    raise ValueError('no space directions in header')


def _run(args, out=None):
    if out:
        with open(out, 'wb') as f:
            subprocess.run(args, stdout=f, stderr=subprocess.PIPE, check=True)
    else:
        subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def make_wlz(src_nrrd, out_wlz, scratch, data=None, header=None):
    if data is None:
        data, header = nrrd.read(src_nrrd)
    data = data.copy()
    for corner in ((0, 0, 0), tuple(s - 1 for s in data.shape)):
        if data[corner] < MARKER:
            data[corner] = MARKER
    hdr = {k: v for k, v in header.items() if k not in ('encoding', 'type', 'dimension', 'sizes', 'endian')}
    if 'space' in hdr:
        hdr.pop('space dimension', None)
    bounded = os.path.join(scratch, 'bounded.nrrd')
    nrrd.write(bounded, data, header=dict(hdr, encoding='raw'))
    w1 = os.path.join(scratch, 'bounded.wlz'); w2 = os.path.join(scratch, 'th.wlz'); w3 = os.path.join(scratch, 'out.wlz')
    _run([f'{WOOLZ_BIN}/WlzExtFFConvert', '-f', 'nrrd', '-F', 'wlz', '-o', w1, bounded])
    if not os.path.getsize(w1):
        raise RuntimeError('WlzExtFFConvert produced an empty file')
    _run([f'{WOOLZ_BIN}/WlzThreshold', '-v2', '-H', w1], out=w2)
    _run([f'{WOOLZ_BIN}/WlzSetVoxelSize', *header_voxel_args(src_nrrd), w2], out=w3)
    atomic_copy(w3, out_wlz)
    for f in (bounded, w1, w2, w3):
        try: os.unlink(f)
        except OSError: pass


def wlz_facts(path):
    """`WlzFacts` text for comparisons (bounding box, voxel size, counts)."""
    return subprocess.run([f'{WOOLZ_BIN}/WlzFacts', path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
