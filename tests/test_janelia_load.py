from janelia.load import templates_for, rel_dir, h5j_key, choose_folder


def test_spaces():
    assert templates_for('JRC2018_FEMALE_20x_HR') == ('JRC2018F', 'JRC2018U')
    assert templates_for('JRC2018_VNC_MALE_40x_DS') == ('JRCVNC2018M', 'JRCVNC2018U')
    assert templates_for('JRC2018_Unisex_20x_HR') == ('JRC2018U', 'JRC2018U')
    for bad in ('JFRC2010_20x', 'FemaleVNCSymmetric2017_20x', 'primary', '', None):
        assert templates_for(bad) is None


def test_folders():
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/i/0010/13n5/VFB_00101567/') == 'VFB/i/0010/13n5/VFB_00101567'
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/t/001/') is None
    assert rel_dir('http://www.virtualflybrain.org/data/VFB/i/0010/13n5/VFB_00101567/../x/') is None
    assert h5j_key('Janelia-20x-Brain-2475746072908005474-20171117_19_A1-REG_JRC2018_FEMALE_20x_HR') == '20x-Brain-2475746072908005474'
    regs = {'VFB_00101567': [('a', None), ('b', 'Missing Image')]}
    assert choose_folder(regs, 'VFB_00101567') == ('b', None)
    assert choose_folder(regs, 'VFB_00200000')[0] is None
