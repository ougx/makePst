"""`makepst log` (the ledger of manifests) and `makepst bundle` (a run zipped for reproduction)."""
import os
import shutil
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from makepst.cli import main  # noqa: E402
from makepst.provenance import find_manifests, mentions  # noqa: E402

MINIMAL = os.path.join(ROOT, 'examples', 'minimal')


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The tutorial run: build, dump, update, parrep, each leaving a manifest."""
    shutil.copytree(MINIMAL, tmp_path / 'm')
    monkeypatch.chdir(tmp_path / 'm')
    main(['build', 'model.pst', 'regul', '--set_ctl_csv', 'control.csv', '--add_pargp_csv', 'pargp.csv',
          '--add_par_csv', 'par.csv', '--add_obs_csv', 'obs.csv', '--add_io_csv', 'io.csv'])
    main(['dump', 'model.pst', 'model.xlsx'])
    main(['update', 'model.xlsx', '--par', 'model.par', '--res', 'model.res', '--out', 'model2.xlsx',
          '--backend', 'openpyxl'])
    main(['parrep', 'model.pst', 'model.par', 'final.pst', '--set', 'noptmax=0'])
    return tmp_path / 'm'


# ---------------------------------------------------------------------- log
def test_log_lists_every_manifest_in_order(project, capsys):
    main(['log'])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [ln.split()[2] for ln in lines] == ['build', 'dump', 'update', 'parrep']
    assert 'model.pst <- control.csv, pargp.csv, par.csv, obs.csv, io.csv  [4 par 4 obs 2 prior]' in lines[0]
    assert 'model2.xlsx <- model.xlsx, model.par, model.res  [11 rows in 3 sheets]' in lines[2]
    assert all('makepst ' in ln for ln in lines)


def test_log_filters(project, capsys):
    main(['log', '--file', 'model.par'])
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['update', 'parrep']
    main(['log', '--command', 'dump', '--command', 'build'])
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['build', 'dump']
    main(['log', '--last', '1'])
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['parrep']
    sha = find_manifests(['.'])[0]['output']['sha256']
    main(['log', '--file', sha[:12]])                    # a hash prefix finds the runs that used that file
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['build', 'dump', 'parrep']
    main(['log', '--file', 'absent.xlsx'])
    assert 'no manifests found' in capsys.readouterr().out


def test_log_check_reports_changed_files(project, capsys):
    (project / 'model.par').write_text('single point\nhk1 1 1 0\n')
    main(['log', '--check'])
    status = {ln.split()[2]: ln.split()[-1] for ln in capsys.readouterr().out.splitlines()}
    assert status == {'build': 'unchanged', 'dump': 'unchanged', 'update': 'changed', 'parrep': 'changed'}


def test_mentions():
    data = {'sources': [{'path': r'C:\x\Book.xlsm', 'sha256': 'abcdef0123'}], 'output': {'path': '/y/case.pst'}}
    assert mentions(data, 'book.xlsm') and mentions(data, 'case.pst') and mentions(data, 'ABCDEF')
    assert not mentions(data, 'other.pst')


# ---------------------------------------------------------------------- bundle
def test_bundle_default_contents(project, capsys):
    main(['bundle', 'model.pst'])
    out = capsys.readouterr().out
    names = zipfile.ZipFile(project / 'model.zip').namelist()
    assert names == ['model.pst', 'model.pst.manifest.json', 'model.tpl', 'model.in', 'heads.ins', 'flow.ins', 'model.py']
    assert 'written model.zip: 7 files' in out
    m = find_manifests([str(project / 'model.zip.manifest.json')])[0]
    assert m['command'] == 'bundle' and m['output']['files'] == 7 and m['output']['missing'] == []
    assert {os.path.basename(s['path']): s['role'] for s in m['sources']}['model.py'] == 'model command'


def test_bundle_sources_outputs_extra_and_list(project, capsys):
    main(['bundle', 'model.pst', 'run.zip', '--sources', '--outputs', '--extra', '*.res'])
    names = set(zipfile.ZipFile(project / 'run.zip').namelist())
    assert {'heads.out', 'flow.out', 'control.csv', 'par.csv', 'model.res'} <= names
    main(['bundle', 'model.pst', '--list'])
    out = capsys.readouterr().out
    assert 'would add' in out and '(nothing written)' in out and not (project / 'model.pst.zip').exists()


def test_bundle_reports_missing_files(project, capsys):
    os.remove(project / 'model.in')
    main(['bundle', 'model.pst', 'run.zip'])
    out = capsys.readouterr().out
    assert 'MISSING    model input        model.in' in out and '1 missing' in out
    assert 'model.in' not in zipfile.ZipFile(project / 'run.zip').namelist()
    with pytest.raises(SystemExit):
        main(['bundle', 'model.pst', 'run2.zip', '--strict'])


# ---------------------------------------------------------------------- record
def _edit(path, old, new):
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new, 1))


def test_record_a_new_file_edited_by_hand(project, capsys):
    text = (project / 'final.pst').read_bytes().replace(b'rch  none', b'rch  log ')
    (project / 'manual.pst').write_bytes(text)
    capsys.readouterr()
    main(['record', 'manual.pst', '--from', 'final.pst', '-m', 'rch log-transformed', '-m', 'see memo',
          '--source', 'model.res'])
    out = capsys.readouterr().out
    assert 'par: 1 changed' in out and 'manifest written to' in out
    m = find_manifests([str(project / 'manual.pst.manifest.json')])[0]
    assert m['command'] == 'record' and m['note'] == 'rch log-transformed\nsee memo'
    assert m['changes'] == 'par: 1 changed' and m['output']['npar'] == 4
    assert [(os.path.basename(s['path']), s['role']) for s in m['sources']] == \
        [('final.pst', 'edited from'), ('model.res', 'reference')]
    main(['log', '--last', '1', '--check'])
    line = capsys.readouterr().out.strip()
    assert 'record     manual.pst <- final.pst, model.res' in line
    assert line.endswith('"rch log-transformed / see memo"  unchanged')
    main(['log', '--file', 'final.pst'])                 # the hand edit is part of final.pst's history
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['parrep', 'record']


def test_record_an_edit_in_place_keeps_the_earlier_manifest(project, capsys):
    before = find_manifests([str(project / 'final.pst.manifest.json')])[0]['output']['sha256']
    _edit(project / 'final.pst', b'rch  none', b'rch  log ')
    main(['record', 'final.pst', '-m', 'rch log-transformed', '--comment'])
    out = capsys.readouterr().out
    assert 'comment added to final.pst' in out and 'earlier manifest kept as' in out
    lines = (project / 'final.pst').read_bytes().splitlines(keepends=True)
    assert lines[0].strip() == b'pcf' and lines[1].endswith(b'rch log-transformed' + lines[0][3:])
    kept = [f for f in os.listdir(project) if f.startswith('final.pst.') and f != 'final.pst.manifest.json']
    assert len(kept) == 1 and kept[0].endswith('.manifest.json')
    m = find_manifests([str(project / 'final.pst.manifest.json')])[0]
    assert m['previous']['manifest'].endswith(kept[0]) and m['sources'] == []
    assert m['previous']['command'] == 'parrep' and m['previous']['sha256'] == before
    main(['log', '--file', 'final.pst', '--check'])
    status = [(ln.split()[2], ln.split()[-1]) for ln in capsys.readouterr().out.splitlines()]
    assert status == [('parrep', 'changed'), ('record', 'unchanged')]      # the ledger shows both steps
    main(['log', '--file', before[:12]])          # the version before the edit leads to the edit
    assert [ln.split()[2] for ln in capsys.readouterr().out.splitlines()] == ['parrep', 'record']
    main(['provenance', 'final.pst'])


def test_note_is_kept_by_every_command_that_writes_a_manifest(project, capsys):
    main(['set', 'final.pst', 'set.pst', '--par', 'rch:parval1=0.002', '--note', 'rch from the new recharge study',
          '--note', 'check run'])
    main(['dump', 'set.pst', 'set.xlsx', '--note', 'for review'])
    m = find_manifests([str(project / 'set.pst.manifest.json')])[0]
    assert m['note'] == 'rch from the new recharge study\ncheck run'
    assert find_manifests([str(project / 'set.xlsx.manifest.json')])[0]['note'] == 'for review'
    assert 'note' not in find_manifests([str(project / 'final.pst.manifest.json')])[0]
    capsys.readouterr()
    main(['log', '--last', '2'])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith('"rch from the new recharge study / check run"')
    assert lines[1].endswith('"for review"')


def test_record_needs_a_message_and_an_existing_file(project):
    with pytest.raises(SystemExit):
        main(['record', 'final.pst'])
    with pytest.raises(SystemExit, match='not found'):
        main(['record', 'absent.pst', '-m', 'x'])
    with pytest.raises(SystemExit, match='the file itself'):
        main(['record', 'final.pst', '--from', 'final.pst', '-m', 'x'])
