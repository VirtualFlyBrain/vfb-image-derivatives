# vfb-image-derivatives

Makes the derived files for every image folder in the VFB image store
(`VFB/i/<xxxx>/<yyyy>/<VFB_template>/` and uploads `VFBu_<id>/<VFB_template>/`):

| File | Made from | Replaces (Jenkins) |
|---|---|---|
| `thumbnail.png`, `thumbnailT.png` | `volume.nrrd` + the template's `template.nrrd` (NRRDtools `create_mip` / `colorize_image_stack` / `merge_images`, ImageMagick for the alpha) | CreateThumbnail |
| `volume.wlz` | `volume.nrrd` with corner markers (`[0,0,0]`, `[max,max,max]` ≥ 3) → `WlzExtFFConvert -f nrrd` → `WlzThreshold -v2 -H` → `WlzSetVoxelSize` | CreateBoundedImageForWLZ → CreateTIFFforWLZ → CreateWLZ |
| `volume.obj` | link to `volume_man.obj` when that exists, else a point cloud (`derive/pointcloud.py`, a vectorised `maxProjVol.py` — byte-identical without jitter, see `tests/`) | CreatePointCloudOBJ |
| — | legacy `volume_bounded*` intermediates removed | Clean_bounded_images |

Each `volume.nrrd` is read from the share once; outputs are written as `<name>.tmp-*` and renamed into place.
Upload folders (`--root private`, workflow parameter `private=true`) get the same three outputs from the
`volume.nrrd` / `volume_man.obj` the upload NBLAST job writes; the legacy-intermediate clean never deletes there.

Run on the VFB cluster as the Argo workflow `image-derivatives` (talosconfig `apps/pipeline`):
`scan` (one pod per shard) → `plan` → `derive` (batches in parallel, `vfb-batch` priority) → `report`.

```
python -m derive.scan --root data --shard 0010 --out RUN/folders/0010.jsonl.gz
python -m derive.plan --run-dir RUN --state-dir STATE            # or --sample 50 for a validation set
python -m derive.worker --run-dir RUN --batch 00000 [--validate] [--clean]
python -m derive.report --run-dir RUN --state-dir STATE
python -m derive.compare --run-dir RUN                           # after a --validate run
```
