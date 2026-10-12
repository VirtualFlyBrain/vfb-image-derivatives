"""thumbnail.png / thumbnailT.png, the same calls CreateThumbnail made:
create_thumbnail.py <template.nrrd> <volume.nrrd> thumbnail.png --cache --add_colorbar_padding --max_scale
then `convert thumbnail.png -alpha copy -channel Alpha -level 2,12% thumbnailT.png`."""
import os, subprocess, sys
from .common import atomic_copy

NRRDTOOLS = os.environ.get('NRRDTOOLS', '/opt/NRRDtools')
if NRRDTOOLS not in sys.path:
    sys.path.insert(0, NRRDTOOLS)


def template_mip(template_nrrd, cache_dir):
    from create_mip import create_mip
    tid = os.path.basename(os.path.dirname(template_nrrd))
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f'template_mip_{tid}.png')
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(template_nrrd):
        tmp = f'{out}.tmp-{os.getpid()}.png'
        create_mip(template_nrrd, tmp, False, True)
        os.chmod(tmp, 0o666)
        os.replace(tmp, out)
    return out


def make_thumbnails(template_nrrd, volume_nrrd, out_dir, scratch, cache_dir):
    from colorize_image_stack import colorize_image_stack
    from merge_images import merge_images
    mip = template_mip(template_nrrd, cache_dir)
    sig = os.path.join(scratch, 'signal.png'); thumb = os.path.join(scratch, 'thumbnail.png')
    thumb_t = os.path.join(scratch, 'thumbnailT.png')
    colorize_image_stack(volume_nrrd, sig, False, True, True)
    merge_images(mip, sig, thumb)
    subprocess.run(['convert', thumb, '-alpha', 'copy', '-channel', 'Alpha', '-level', '2,12%', thumb_t],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    atomic_copy(thumb, os.path.join(out_dir, 'thumbnail.png'))
    atomic_copy(thumb_t, os.path.join(out_dir, 'thumbnailT.png'))
    for f in (sig, thumb, thumb_t):
        try: os.unlink(f)
        except OSError: pass
