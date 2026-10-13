from janelia.load import templates_for, rel_dir, h5j_key, choose_folder


def test_spaces():
    assert templates_for('JRC2018_FEMALE_20x_HR') == ('JRC2018F', 'JRC2018U')
    assert templates_for('JRC2018_VNC_MALE_40x_DS') == ('JRCVNC2018M', 'JRCVNC2018U')
    assert templates_for('JRC2018_Unisex_20x_HR') == ('JRC2018U', 'JRC2018U')
    assert templates_for('JFRC2010_20x') == ('JFRC2', 'JRC2018U')
    for bad in ('JFRC2010_63x', 'JFRC2010', 'FemaleVNCSymmetric2017_20x', 'primary', '', None):
        assert templates_for(bad) is None


def test_folders():
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/i/0010/13n5/VFB_00101567/') == 'VFB/i/0010/13n5/VFB_00101567'
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/t/001/') is None
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/i/0010/13n5/VFB_00101567/../x/') is None
    assert h5j_key('Janelia-20x-Brain-2475746072908005474-20171117_19_A1-REG_JRC2018_FEMALE_20x_HR') == '20x-Brain-2475746072908005474'
    regs = {'VFB_00101567': [('a', None), ('b', 'Missing Image')]}
    assert choose_folder(regs, 'VFB_00101567') == ('b', None)
    assert choose_folder(regs, 'VFB_00200000')[0] is None


def test_patched_info_uses_plan_templates():
    import types
    import numpy as np
    from janelia.load import patch_h5j_info
    infos = {'a': 'JFRC2010_20x', 'b': 'JRC2018_FEMALE_20x_HR', 'c': 'FemaleVNCSymmetric2017_20x'}
    fake = types.SimpleNamespace(read_h5j_info=lambda path: {
        'alignment_space': infos[path], 'voxel_size': np.array([0.62, 0.62, 0.62]),
        'source_template': 'JRC2018U', 'target_template': 'JRC2018U'})
    patch_h5j_info(fake)
    a = fake.read_h5j_info('a')
    assert (a['source_template'], a['target_template']) == ('JFRC2', 'JRC2018U')
    assert list(a['voxel_size']) == [0.622088] * 3
    b = fake.read_h5j_info('b')
    assert (b['source_template'], list(b['voxel_size'])) == ('JRC2018F', [0.62] * 3)
    try:
        fake.read_h5j_info('c')
        assert False
    except ValueError:
        pass
