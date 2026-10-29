"""The worker's write guard: VFB/DATA image folders and VFB/PRIVATE upload folders, the four output names only."""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from derive.common import check_share_target


@pytest.mark.parametrize('root,rel,name', [
    ('data', 'VFB/i/0010/1567/VFB_00101567', 'thumbnail.png'),
    ('private', 'VFBu_8uvHb4t6/VFB_00101567', 'thumbnailT.png'),
    ('private', 'VFBu_a-b_c123/VFB_00200000', 'volume.wlz'),
    ('private', 'VFBu_e2eSwc0001/VFB_00101567', 'volume.obj'),
])
def test_allowed(root, rel, name):
    check_share_target(root, rel, name)


@pytest.mark.parametrize('root,rel,name', [
    ('data', 'VFB/i/0010/1567/VFB_00101567', 'volume.nrrd'),
    ('private', 'VFBu_8uvHb4t6/VFB_00101567', 'volume.nrrd'),
    ('private', 'VFBu_8uvHb4t6/VFB_00101567', 'volume.swc'),
    ('private', 'UPLOAD/VFB_00101567', 'thumbnail.png'),
    ('private', 'VFBu_x/../VFB_00101567', 'thumbnail.png'),
    ('private', 'VFBu_8uvHb4t6', 'thumbnail.png'),
    ('private', 'VFB/i/0010/1567/VFB_00101567', 'thumbnail.png'),
    ('data', 'VFBu_8uvHb4t6/VFB_00101567', 'thumbnail.png'),
    ('other', 'VFB/i/0010/1567/VFB_00101567', 'thumbnail.png'),
])
def test_refused(root, rel, name):
    with pytest.raises(PermissionError):
        check_share_target(root, rel, name)
