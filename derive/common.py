import os, re, tempfile

WOOLZ_BIN = os.environ.get('WOOLZ_BIN', '/opt/MouseAtlas/bin')
FILE_MODE = 0o777   # everything on the image share is 777


def atomic_write_bytes(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.tmp-', dir=d)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


# The only places the worker may write: VFB/DATA VFB/i/<4>/<4>/VFB_<8>/ (id chars 4-8, 8-12, template id) and
# VFB/PRIVATE VFBu_<id>/VFB_<8>/ (user uploads: the upload NBLAST job writes volume.nrrd/.swc/volume_man.obj there,
# this worker their thumbnails and WLZ), and only these names.
SHARE_DIRS = {'data': re.compile(r'^VFB/i/[^/]{4}/[^/]{4}/VFB_[^/]{8}$'),
              'private': re.compile(r'^VFBu_[A-Za-z0-9_-]{4,40}/VFB_[^/]{8}$')}
SHARE_DIR = SHARE_DIRS['data']
SHARE_NAMES = {'thumbnail.png', 'thumbnailT.png', 'volume.wlz', 'volume.obj'}


def check_share_target(root, rel, name):
    pat = SHARE_DIRS.get(root)
    if pat is None or not pat.match(rel) or name not in SHARE_NAMES:
        raise PermissionError(f'refusing to write {root}:{rel}/{name} (outside VFB/i/xxxx/xxxx/VFB_xxxxxxxx/ '
                              f'and VFBu_<id>/VFB_xxxxxxxx/)')


def atomic_copy(src, dst):
    with open(src, 'rb') as f:
        data = f.read()
    if not data:
        raise ValueError(f'{src} is empty')
    atomic_write_bytes(dst, data)


def template_path(data_root, template_id):
    """VFB/i/<id[4:8]>/<id[8:12]>/<id>/template.nrrd — the layout CreateThumbnail used."""
    return os.path.join(data_root, 'VFB', 'i', template_id[4:8], template_id[8:12], template_id, 'template.nrrd')
