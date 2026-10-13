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


# The only place the worker may write on the share: VFB/i/<4>/<4>/VFB_<8>/ (id chars 4-8, 8-12, template id),
# and only these names.
SHARE_DIR = re.compile(r'^VFB/i/[^/]{4}/[^/]{4}/VFB_[^/]{8}$')
SHARE_NAMES = {'thumbnail.png', 'thumbnailT.png', 'volume.wlz', 'volume.obj'}


def check_share_target(root, rel, name):
    if root != 'data' or not SHARE_DIR.match(rel) or name not in SHARE_NAMES:
        raise PermissionError(f'refusing to write {root}:{rel}/{name} (outside VFB/i/xxxx/xxxx/VFB_xxxxxxxx/)')


def atomic_copy(src, dst):
    with open(src, 'rb') as f:
        data = f.read()
    if not data:
        raise ValueError(f'{src} is empty')
    atomic_write_bytes(dst, data)


def template_path(data_root, template_id):
    """VFB/i/<id[4:8]>/<id[8:12]>/<id>/template.nrrd — the layout CreateThumbnail used."""
    return os.path.join(data_root, 'VFB', 'i', template_id[4:8], template_id[8:12], template_id, 'template.nrrd')
