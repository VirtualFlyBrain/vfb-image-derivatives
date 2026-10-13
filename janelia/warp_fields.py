# Vendored from VirtualFlyBrain/Management Janelia/JaneliaWorkstationAPI/warp_fields.py (master c90cc13) for the Argo janelia-load job;
# keep in step with that copy. The loader (janelia/load.py) uses its decode/warp/write helpers only.
#!/usr/bin/env python3
"""Pre-computed target-grid -> source-space coordinate maps for warping whole volumes.

Why this exists
---------------
`process_h5j_simple.align_volume_to_template` used to push every voxel of the target
template (119 M for JRC2018U, 325 M for JRCVNC2018U) through `navis.xform_brain` for
every image: 2-11 us a point, so minutes of transform per channel. That work is identical
for every image that shares a (source, target) template pair, and the Janelia backlog only
ever uses four pairs (JRC2018F/M -> JRC2018U, JRCVNC2018F/M -> JRCVNC2018U).

This module computes each map once, with exactly the same navis call, and stores it as a
memory-mapped float32 `.npy` of shape (Z, Y, X, 3): for every target voxel, the source
coordinate in microns. `warp_volume` then turns an image into template space with one
`scipy.ndimage.map_coordinates` per Z chunk and no navis at all. Z-major so that a chunk
of Z slices is one contiguous read and consecutive output voxels walk the source volume
along its fastest axis (measured: 2.5x faster per chunk than (X, Y, Z)). Same idea as the baked
BANC fields in VFB_connectomics_import (images/transforms.py), but a different shape:
there a parametric elastix hop was sampled onto a coarse grid and interpolated per point;
here the deformation is already dense (the Saalfeld H5 fields are 0.44 um) and the
consumer is a regular grid, so the map is stored at target-voxel resolution and applied
directly.

Where the maps live
-------------------
`$WARP_FIELD_DIR`, else `<flybrains data home>/janelia_warp_fields` (next to the H5
transforms they are derived from; `~/flybrain-data` unless `$FLYBRAINS_DATA` is set).
`get_field` builds a missing map on first use, behind a file lock so parallel workers do
not race. Sizes: 1.4 GB per source template on the JRC2018U grid, 3.9 GB on the
JRCVNC2018U grid. Memory-mapped, so N workers share one copy through the page cache.

A sidecar `.json` records the grid the map was built for; `get_field` refuses a map whose
grid does not match what the caller asks for rather than silently warping onto the wrong
voxels.
"""
import fcntl
import json
import os
import time

import numpy as np
from scipy import ndimage

DEFAULT_CHUNK_Z = 20

try:  # optional: ~2.6x faster than scipy's generic map_coordinates, bit-identical output
    import numba

    @numba.njit(cache=True, nogil=True)
    def _trilinear_u8(vol, coords, out):
        """coords (N, 3) float32 source pixel x, y, z; vol (Z, Y, X) uint8; out (N,) uint8.

        Mirrors scipy.ndimage.map_coordinates(order=1, mode='constant', cval=0): a
        neighbour outside the volume contributes 0, result rounded to nearest.
        """
        Z, Y, X = vol.shape
        for i in range(coords.shape[0]):
            x = coords[i, 0]
            y = coords[i, 1]
            z = coords[i, 2]
            if not (x > -1.0 and x < X and y > -1.0 and y < Y and z > -1.0 and z < Z):
                out[i] = 0
                continue
            x0 = int(np.floor(x)); y0 = int(np.floor(y)); z0 = int(np.floor(z))
            fx = x - x0; fy = y - y0; fz = z - z0
            acc = 0.0
            for dz in range(2):
                zz = z0 + dz
                if zz < 0 or zz >= Z:
                    continue
                wz = fz if dz == 1 else 1.0 - fz
                for dy in range(2):
                    yy = y0 + dy
                    if yy < 0 or yy >= Y:
                        continue
                    wy = fy if dy == 1 else 1.0 - fy
                    for dx in range(2):
                        xx = x0 + dx
                        if xx < 0 or xx >= X:
                            continue
                        wx = fx if dx == 1 else 1.0 - fx
                        acc += wz * wy * wx * vol[zz, yy, xx]
            v = int(acc + 0.5)
            out[i] = 255 if v > 255 else v
except ImportError:  # pragma: no cover
    numba = None
    _trilinear_u8 = None


def backend():
    """'numba' when importable (or WARP_BACKEND says so), else 'scipy'."""
    want = os.environ.get('WARP_BACKEND', 'auto').lower()
    if want == 'scipy' or (want == 'auto' and _trilinear_u8 is None):
        return 'scipy'
    if _trilinear_u8 is None:
        raise RuntimeError('WARP_BACKEND=numba but numba is not installed')
    return 'numba'


def field_dir():
    d = os.environ.get('WARP_FIELD_DIR')
    if d:
        return d
    home = os.environ.get('FLYBRAINS_DATA') or os.path.expanduser('~/flybrain-data')
    return os.path.join(home, 'janelia_warp_fields')


def field_paths(source_template, target_template, directory=None):
    d = directory or field_dir()
    stem = os.path.join(d, f'{target_template}_grid_to_{source_template}_um')
    return stem + '.npy', stem + '.json'


class WarpField:
    """Memory-mapped (Z, Y, X, 3) float32: target voxel -> source coordinate in microns."""

    def __init__(self, npy_path, mmap=True):
        json_path = os.path.splitext(npy_path)[0] + '.json'
        with open(json_path) as fh:
            self.meta = json.load(fh)
        self.coords = np.load(npy_path, mmap_mode='r' if mmap else None)
        self.path = npy_path
        if list(self.coords.shape) != list(self.meta['shape']):
            raise ValueError(f'{npy_path}: array shape {self.coords.shape} does not match '
                             f'sidecar {self.meta["shape"]} - truncated build?')
        if self.meta.get('layout') != 'zyx':
            raise ValueError(f'{npy_path}: unsupported layout {self.meta.get("layout")!r}; '
                             f'delete it and let it rebuild')
        self.dims = [int(v) for v in self.meta['dims']]
        self.voxdims = [float(v) for v in self.meta['voxdims']]
        self.source = self.meta['source']
        self.target = self.meta['target']

    def __repr__(self):
        return (f'<WarpField {self.target} grid {tuple(self.dims)} @ {self.voxdims} '
                f'-> {self.source} um, {os.path.basename(self.path)}>')

    def matches(self, dims, voxdims, tol=1e-6):
        return (list(map(int, dims)) == self.dims
                and np.allclose(voxdims, self.voxdims, atol=tol, rtol=0))


def bake(source_template, target_template, dims, voxdims, npy_path,
         chunk_z=DEFAULT_CHUNK_Z, verbose=True):
    """Sample navis' (target -> source) transform on the full target grid, once.

    This is the loop `align_volume_to_template` ran per image, unchanged: the same
    coordinates, the same `xform_brain(source=target, target=source, affine_fallback=True)`
    call. Only the destination differs - a memmapped .npy written to a temp name and
    renamed into place, so a half-built map is never mistaken for a finished one.
    """
    import navis
    import flybrains

    W, H, D = (int(v) for v in dims)
    tx, ty, tz = (float(v) for v in voxdims)
    os.makedirs(os.path.dirname(npy_path), exist_ok=True)
    tmp = npy_path + '.building'
    out = np.lib.format.open_memmap(tmp, mode='w+', dtype=np.float32, shape=(D, H, W, 3))

    x_coords = np.arange(W) * tx
    y_coords = np.arange(H) * ty
    t0 = time.time()
    nan_count = 0
    if verbose:
        print(f'warp_fields: baking {target_template} grid {(W, H, D)} @ {voxdims} -> '
              f'{source_template}: {W * H * D:,} points', flush=True)
    for z0 in range(0, D, chunk_z):
        z1 = min(z0 + chunk_z, D)
        z_coords = np.arange(z0, z1) * tz
        Z, Y, X = np.meshgrid(z_coords, y_coords, x_coords, indexing='ij')
        pts = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
        src = navis.xform_brain(pts, source=target_template, target=source_template,
                                affine_fallback=True)
        src = np.asarray(src, dtype=np.float64).reshape(z1 - z0, H, W, 3)
        nan_count += int(np.isnan(src).any(axis=-1).sum())
        out[z0:z1] = src.astype(np.float32)
        if verbose:
            el = time.time() - t0
            print(f'  z {z0:4d}-{z1 - 1:4d}  {el:6.0f} s  '
                  f'eta {el / z1 * (D - z1):5.0f} s', flush=True)
    out.flush()
    del out
    seconds = round(time.time() - t0, 1)
    meta = dict(source=source_template, target=target_template,
                dims=[W, H, D], voxdims=[tx, ty, tz], shape=[D, H, W, 3], layout='zyx',
                dtype='float32', units='um', chunk_z=chunk_z,
                nan_voxels=nan_count, built=time.strftime('%Y-%m-%d %H:%M:%S'),
                build_seconds=seconds, navis=navis.__version__,
                flybrains=flybrains.__version__,
                note='source coordinates (um) for every target voxel; divide by the source '
                     'image voxel size to get pixel coordinates. NaN = navis returned no '
                     'coordinate; warp_volume maps those voxels to 0.')
    with open(os.path.splitext(npy_path)[0] + '.json', 'w') as fh:
        json.dump(meta, fh, indent=1)
    os.replace(tmp, npy_path)
    if verbose:
        print(f'warp_fields: built {npy_path} in {seconds / 60:.1f} min '
              f'({os.path.getsize(npy_path) / 1e9:.2f} GB, {nan_count:,} NaN voxels)',
              flush=True)
    return npy_path


def get_field(source_template, target_template, dims, voxdims, directory=None,
              bake_if_missing=True, mmap=True, verbose=True):
    """Load the map for this pair and grid, building it first if it does not exist.

    Serialised on a lock file so that several workers hitting the same missing map build
    it once and the rest wait; the check is repeated after taking the lock.
    """
    npy_path, _ = field_paths(source_template, target_template, directory)
    if not os.path.exists(npy_path):
        if not bake_if_missing:
            raise FileNotFoundError(f'warp field not found: {npy_path} (set $WARP_FIELD_DIR '
                                    f'or allow baking)')
        os.makedirs(os.path.dirname(npy_path), exist_ok=True)
        with open(npy_path + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if not os.path.exists(npy_path):
                    bake(source_template, target_template, dims, voxdims, npy_path,
                         verbose=verbose)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    field = WarpField(npy_path, mmap=mmap)
    if not field.matches(dims, voxdims):
        raise ValueError(
            f'{npy_path} was built for grid dims={field.dims} voxdims={field.voxdims} but '
            f'this run wants dims={list(dims)} voxdims={list(voxdims)}. The template spec '
            f'has changed; delete the file (and its .json) to rebuild it.')
    if field.source != source_template or field.target != target_template:
        raise ValueError(f'{npy_path}: sidecar says {field.target} -> {field.source}, '
                         f'expected {target_template} -> {source_template}')
    return field


def warp_volume(volume_zyx, field, voxel_size_xyz, chunk_z=DEFAULT_CHUNK_Z, order=1):
    """Resample a (Z, Y, X) source stack onto the field's target grid -> (X, Y, Z).

    Numerically the same as the old per-image loop: trilinear `map_coordinates`,
    `mode='constant', cval=0`, source pixel = source micron / voxel size. The division is
    done in float32 (the map's own precision; measured against the float64 loop: a
    handful of voxels per million differ by one grey level). Voxels whose map is NaN
    (navis had no coordinate) are sent to pixel -1, i.e. sampled as 0, rather than being
    cast from NaN to an undefined integer index.
    """
    W, H, D = field.dims
    vox = np.asarray(voxel_size_xyz, dtype=np.float32).reshape(1, 1, 1, 3)
    out_zyx = np.empty((D, H, W), dtype=volume_zyx.dtype)
    use_numba = (backend() == 'numba' and order == 1 and volume_zyx.dtype == np.uint8)
    if use_numba:
        volume_zyx = np.ascontiguousarray(volume_zyx)
    for z0 in range(0, D, chunk_z):
        z1 = min(z0 + chunk_z, D)
        src = field.coords[z0:z1] / vox
        bad = np.isnan(src).any(axis=-1)
        if bad.any():
            src[bad] = -1.0
        if use_numba:
            flat = np.ascontiguousarray(src.reshape(-1, 3))
            _trilinear_u8(volume_zyx, flat, out_zyx[z0:z1].reshape(-1))
        else:
            out_zyx[z0:z1] = ndimage.map_coordinates(
                volume_zyx, [src[..., 2], src[..., 1], src[..., 0]],
                order=order, mode='constant', cval=0)
    return np.ascontiguousarray(out_zyx.transpose(2, 1, 0))


def main():
    """Build (or verify) the maps for the pairs the Janelia backlog needs.

        python warp_fields.py [--dir DIR] [--pairs JRC2018F:JRC2018U,...]

    Template specs are looked up the same way the pipeline does (VFB KB, flybrains
    fallback) via `process_h5j_simple.get_vfb_template_specs`.
    """
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', default=None, help='override $WARP_FIELD_DIR')
    ap.add_argument('--pairs', default='JRC2018F:JRC2018U,JRC2018M:JRC2018U,'
                                       'JRCVNC2018F:JRCVNC2018U,JRCVNC2018M:JRCVNC2018U')
    ap.add_argument('--chunk-z', type=int, default=DEFAULT_CHUNK_Z)
    args = ap.parse_args()

    import flybrains
    from process_h5j_simple import get_vfb_template_specs, ensure_transforms
    ensure_transforms()
    for pair in args.pairs.split(','):
        source, target = pair.split(':')
        specs = get_vfb_template_specs(target)
        if specs is None:
            t = getattr(flybrains, target)
            specs = {'dims': list(t.dims), 'voxdims': list(t.voxdims)}
        f = get_field(source, target, specs['dims'], specs['voxdims'], directory=args.dir)
        print(f'{pair}: {f}  ({f.meta.get("nan_voxels", "?")} NaN voxels, built '
              f'{f.meta.get("built")})')


if __name__ == '__main__':
    main()
