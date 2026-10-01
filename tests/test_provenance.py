"""Every producing command leaves a <output>.manifest.json that identifies its inputs."""
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import __version__  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.provenance import sha256  # noqa: E402
from make_fixture import BOOK, BUILD_ARGS, DATA, PST  # noqa: E402


def manifest(path):
    with open(str(path) + '.manifest.json') as f:
        return json.load(f)


def test_build_manifest(tmp_path):
    out = tmp_path / 'demo.pst'
    args = list(BUILD_ARGS)
    args[0] = str(out)
    main(args)
    m = manifest(out)
    assert m['makepst'] == __version__ and m['command'] == 'build' and m['argv'][1] == str(out)
    assert m['created'][:4] == '2026' or len(m['created']) >= 19
    assert m['python'] and m['pandas'] and m['openpyxl'] and m['platform'] and m['cwd']
    src = {os.path.basename(s['path']): s for s in m['sources']}
    assert list(src) == ['demo.xlsx']                                # one entry, many sheets
    assert src['demo.xlsx']['sha256'] == sha256(BOOK)
    assert src['demo.xlsx']['sheets'] == ['CONTROL', 'PARGP', 'PAR_HK', 'PAR_SY', 'PAR_RCH',
                                          'OBS_HEAD', 'OBS_FLOW', 'IO', 'PPcntl']
    assert m['output']['sha256'] == sha256(out) and m['output']['npar'] == 11
    assert m['output']['pestmode'] == 'regularisation'


def test_build_manifest_records_parval_and_csv(tmp_path):
    out = tmp_path / 'x.pst'
    args = list(BUILD_ARGS)
    args[0] = str(out)
    main(args + ['--fill_parval', os.path.join(DATA, 'demo.par')])
    roles = {os.path.basename(s['path']): s.get('role') for s in manifest(out)['sources']}
    assert roles == {'demo.xlsx': 'control', 'demo.par': 'parval'}
    assert 'sheets' not in [s for s in manifest(out)['sources'] if s['path'].endswith('.par')][0]


def test_no_manifest_flag(tmp_path):
    out = tmp_path / 'y.pst'
    args = list(BUILD_ARGS)
    args[0] = str(out)
    main(args + ['--no_manifest'])
    assert out.exists() and not (tmp_path / 'y.pst.manifest.json').exists()


def test_parrep_manifest(tmp_path):
    out = tmp_path / 'rep.pst'
    main(['parrep', PST, os.path.join(DATA, 'demo.3.par.csv'), str(out), '--real', 'best', '--set', 'noptmax=0'])
    m = manifest(out)
    assert m['command'] == 'parrep' and m['realization'] == 'best' and m['set'] == ['noptmax=0']
    roles = {os.path.basename(s['path']): s['role'] for s in m['sources']}
    assert roles == {'demo.pst': 'base control file', 'demo.3.par.csv': 'parameter values'}
    assert m['output']['sha256'] == sha256(out)


def test_parrep_from_workbook_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / 'rep.pst'
    main(['parrep', BOOK, os.path.join(DATA, 'demo.par'), str(out)])
    m = manifest(out)
    assert m['build_args'][0].endswith('demo.pst') and m['build_args'][1] == 'regul'
    assert m['build_args'][3].endswith('demo.xlsx,CONTROL')            # resolved to the workbook's path
    book = [s for s in m['sources'] if s['path'].endswith('demo.xlsx')][0]
    assert book['sheets'][0] == 'BUILD' and 'PAR_HK' in book['sheets']


def test_dump_manifest(tmp_path):
    book = tmp_path / 'd.xlsx'
    main(['dump', PST, str(book)])
    m = manifest(book)
    assert m['command'] == 'dump' and m['sources'][0]['sha256'] == sha256(PST)
    assert m['output']['sha256'] == sha256(book) and m['build_command'].startswith('makepst build')


@pytest.mark.parametrize('options', [[], ['--split', '--no_manifest']])
def test_dump_default_workbook(tmp_path, monkeypatch, options):
    source_dir = tmp_path / 'inputs'
    source_dir.mkdir()
    source = source_dir / 'case.pst'
    shutil.copy(PST, source)
    monkeypatch.chdir(tmp_path)

    main(['dump', os.path.join('inputs', 'case.pst')] + options)

    book = source.with_suffix('.xlsx')
    assert book.exists()
    assert not (tmp_path / 'case.xlsx').exists()
    if '--no_manifest' in options:
        assert not os.path.exists(str(book) + '.manifest.json')
    else:
        m = manifest(book)
        assert m['sources'][0]['sha256'] == sha256(source)
        assert m['output']['sha256'] == sha256(book)
        assert 'case.xlsx' in m['build_command']


def test_update_manifest(tmp_path):
    book = tmp_path / 'u.xlsx'
    shutil.copy(BOOK, book)
    out = tmp_path / 'u2.xlsx'
    main(['update', str(book), '--par', os.path.join(DATA, 'demo.par'), '--res', os.path.join(DATA, 'demo.res'),
          '--out', str(out), '--sheet', 'PAR_*,OBS_HEAD', '--backend', 'openpyxl'])
    m = manifest(out)
    assert m['command'] == 'update' and m['output']['backend'] == 'openpyxl'
    assert {s: v['rows'] for s, v in m['output']['sheets'].items()} == {
        'PAR_HK': 6, 'PAR_SY': 2, 'PAR_RCH': 3, 'OBS_HEAD': 4, 'PHI': 5}
    assert all(v['formula_cells_skipped'] == 0 and v['cells_refused'] == 0 for v in m['output']['sheets'].values())
    assert m['sheet'] == ['PAR_*', 'OBS_HEAD'] and m['par_cols'] == 'PARVAL1'
    roles = {os.path.basename(s['path']): s['role'] for s in m['sources']}
    assert roles == {'u.xlsx': 'workbook', 'demo.par': 'parameter values', 'demo.res': 'residuals'}
    assert not (tmp_path / 'u.xlsx.manifest.json').exists()       # manifest sits beside --out
