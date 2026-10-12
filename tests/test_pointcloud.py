"""Parity: derive.pointcloud vs the original NRRDtools/maxProjVol.py (vendored verbatim as
maxProjVol_orig.py), with the random jitter set to 0 in both."""
import os, sys, numpy as np, nrrd, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from derive.pointcloud import point_cloud

ORIG = open(os.path.join(os.path.dirname(__file__), 'maxProjVol_orig.py')).read()
ORIG = ORIG.replace('random.uniform(-1, 1)', '0.0').replace('np.float(', 'float(')


def run_original(path):
    argv = sys.argv
    sys.argv = ['maxProjVol.py', path]
    try:
        exec(compile(ORIG, 'maxProjVol_orig', 'exec'), {'__name__': '__main__'})
    finally:
        sys.argv = argv
    return open(path.replace('.nrrd', '.obj')).read()


def volumes():
    rng = np.random.default_rng(1)
    zz, yy, xx = np.meshgrid(np.arange(22), np.arange(30), np.arange(40), indexing='ij')
    sphere = (((xx - 20) ** 2 + (yy - 15) ** 2 + (zz - 11) ** 2) < 64).transpose(2, 1, 0)
    yield 'binary', (sphere * 255).astype(np.uint8)
    grey = np.zeros((40, 30, 22), np.uint8)
    for _ in range(25):
        x, y, z = rng.integers(0, [40, 30, 22]); grey[x, y, z:z + 4] = rng.integers(20, 256)
    grey[5:9, 5:9, 3:6] = rng.integers(0, 256, (4, 4, 3))
    yield 'grey', grey
    yield 'edges', np.pad(np.full((3, 3, 3), 200, np.uint8), ((0, 37), (0, 27), (19, 0)))


@pytest.mark.parametrize('name,data', list(volumes()))
def test_parity(tmp_path, name, data):
    hdr = {'space directions': np.diag([0.5189161, 0.5189161, 1.0]), 'space': 'left-posterior-superior'}
    p = str(tmp_path / f'{name}.nrrd')
    nrrd.write(p, data, header=hdr)
    orig = run_original(p)
    mine = point_cloud(data, [0.5189161, 0.5189161, 1.0], jitter=False)
    assert mine.count('\nv ') == orig.count('\nv ') and mine.count('\nv ') > 0
    assert mine == orig
