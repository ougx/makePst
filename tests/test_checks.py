"""makepst validate: table checks, template/instruction cross-checks, the instruction interpreter."""
import os
import shutil
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from makepst import from_text, read_pst, write_pst  # noqa: E402
from makepst.checks import (InstructionError, check_files, check_sections, check_tables,  # noqa: E402
                            instruction_names, run_instructions, template_names)
from makepst.cli import main  # noqa: E402
from make_fixture import BOOK, PST  # noqa: E402

MINIMAL = os.path.join(ROOT, 'examples', 'minimal')


def errors(findings):
    return [f.message for f in findings if f.severity == 'error']


def warnings_(findings):
    return [f.message for f in findings if f.severity == 'warning']


@pytest.fixture
def minimal(tmp_path, monkeypatch):
    shutil.copytree(MINIMAL, tmp_path / 'm')
    monkeypatch.chdir(tmp_path / 'm')
    main(['build', 'model.pst', 'regul', '--set_ctl_csv', 'control.csv', '--add_pargp_csv', 'pargp.csv',
          '--add_par_csv', 'par.csv', '--add_obs_csv', 'obs.csv', '--add_io_csv', 'io.csv',
          '--no_manifest', '--no_dump_tpl'])
    return tmp_path / 'm'


# ---------------------------------------------------------------------- end to end
def test_minimal_example_is_clean(minimal, capsys):
    main(['validate', 'model.pst', '--outputs'])
    out = capsys.readouterr().out
    assert '0 errors, 0 warnings' in out
    assert 'heads.ins: read 3 of 3 observations' in out and 'flow.ins: read 1 of 1' in out


def test_validate_exit_code(minimal, capsys):
    os.remove('flow.ins')
    with pytest.raises(SystemExit):
        main(['validate', 'model.pst'])
    assert 'flow.ins: instruction file not found' in capsys.readouterr().out
    (minimal / 'flow.ins').write_text('pif @\n@gauge@ !q_gauge!\n')
    main(['validate', 'model.pst'])                      # clean again


def test_validate_strict_on_warnings(minimal, capsys):
    p = read_pst('model.pst')
    p.cmd = ['missing.bat']                              # a warning, not an error
    write_pst(p, 'model.pst', dump_tpl=False)
    main(['validate', 'model.pst', '--quiet'])
    out = capsys.readouterr().out
    assert 'missing.bat not found' in out and '0 errors, 1 warnings' in out and 'INFO' not in out
    with pytest.raises(SystemExit):
        main(['validate', 'model.pst', '--strict'])


def test_validate_paths_relative_to_run_directory(minimal, tmp_path, monkeypatch, capsys):
    """Control file in a subfolder, paths relative to the model root, run from the root."""
    root = tmp_path / 'root'
    (root / 'pest').mkdir(parents=True)
    for f in ('model.tpl', 'heads.ins', 'flow.ins', 'heads.out', 'flow.out', 'model.py'):
        shutil.copy(minimal / f, root / 'pest' / f)
    p = read_pst('model.pst')
    p.tpl = [('pest/' + a, b) for a, b in p.tpl]
    p.ins = [('pest/' + a, b) for a, b in p.ins]
    write_pst(p, str(root / 'pest' / 'case.pst'), dump_tpl=False)
    monkeypatch.chdir(root)
    main(['validate', 'pest/case.pst', '--quiet'])                  # would fail if resolved from pest/
    out = capsys.readouterr().out
    assert '0 errors' in out
    main(['validate', 'pest/case.pst'])
    assert f'resolved relative to {root}' in capsys.readouterr().out
    with pytest.raises(SystemExit):                                   # an explicit --base_dir is obeyed
        main(['validate', 'pest/case.pst', '--base_dir', 'pest'])


def test_validate_workbook_target(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):                      # fixture workbook points at PEST\*.tpl that don't exist
        main(['validate', BOOK])
    out = capsys.readouterr().out
    assert 'template file not found' in out
    assert 'cited in no template' not in out             # no template readable: name check not meaningful


def test_unknown_section_is_informational_because_it_is_preserved(tmp_path):
    path = tmp_path / 'future.pst'
    path.write_text('pcf\n* future section\nopaque data\n')
    findings = check_sections(path)
    assert [(f.severity, f.message) for f in findings] == [
        ('info', 'unknown section preserved verbatim: * future section')]


# ---------------------------------------------------------------------- tables
def small():
    with open(PST) as f:
        return from_text(f.read())


def test_table_checks():
    p = small()
    assert errors(check_tables(p)) == []
    p.par.loc[0, 'PARVAL1'] = 1e6                        # above PARUBND
    p.par.loc[1, 'TIETO'] = 'ghost'
    p.par.loc[2, 'PARGP'] = 'nogroup'
    p.obs.loc[0, 'WEIGHT'] = -1
    p.obs.loc[1, 'OBSNME'] = 'this_name_is_much_too_long_for_pest'
    p.prior.loc[0, 'EQ'] = '1.0 * log(hk1_cc01) - 1.0 * hk1_cc03 = 0'     # hk1_cc03 is log-transformed
    p.prior.loc[1, 'EQ'] = 'nonsense'
    f = check_tables(p)
    e, w = errors(f), warnings_(f)
    assert any('outside bounds: hk1_cc01' in m for m in e)
    assert any('does not exist: hk1_cc02' in m for m in e)
    assert any('used but not defined: nogroup' in m for m in e)
    assert any('negative weight' in m for m in e)
    assert any('log() use does not match PARTRANS: hk1_cc03' in m for m in e)
    assert any('malformed equation' in m for m in e)
    assert any('longer than 20 characters' in m for m in w)


def test_table_checks_duplicates_and_dercom():
    p = small()
    p.par.loc[1, 'PARNME'] = 'hk1_cc01'
    p.par.loc[3, 'DERCOM'] = 2
    e = errors(check_tables(p))
    assert any('duplicate names: hk1_cc01' in m for m in e)
    assert any('DERCOM exceeds' in m for m in e)


# ---------------------------------------------------------------------- templates / instructions
def test_template_and_instruction_names(tmp_path):
    tpl = tmp_path / 'a.tpl'
    tpl.write_text('ptf $\nk $  HK1 $ and $hk2$\nrow $ Rch $\n')
    assert template_names(tpl) == ['hk1', 'hk2', 'rch']
    ins = tmp_path / 'a.ins'
    ins.write_text('pif @\n@head (!) [x]@ w !H1! [h2]5:12 (h3)14:20\nl1 !dum! !H4!\n')
    assert instruction_names(ins) == ['h1', 'h2', 'h3', 'h4']         # marker text with ! [ ( ignored, dum dropped
    bad = tmp_path / 'bad.tpl'
    bad.write_text('nothing here\n')
    with pytest.raises(ValueError, match='ptf'):
        template_names(bad)


def test_file_cross_checks(tmp_path):
    p = small()
    p.tpl = [('a.tpl', 'a.in')]
    p.ins = [('a.ins', 'a.out'), ('b.ins', 'b.out')]
    p.cmd = ['run.bat']
    (tmp_path / 'a.tpl').write_text('ptf ~\n~hk1_cc01~ ~hk1_cc02~ ~ghost~\n')
    (tmp_path / 'a.ins').write_text('pif @\nl1 !mr1158636_197611! !mr1158636_197705! !mr1158636_197705!\n')
    (tmp_path / 'b.ins').write_text('pif @\nl1 !mr1158636_197611! !nobody!\n')
    f = check_files(p, str(tmp_path))
    e, w = errors(f), warnings_(f)
    assert any('not in the control file: ghost' in m for m in e)
    assert any('cited in no template file: hk1_cc03' in m for m in e)
    assert any('read more than once: mr1158636_197705' in m for m in e)
    assert any('not in the control file: nobody' in m for m in e)
    assert any('more than one instruction file: mr1158636_197611' in m for m in e)
    assert any('read by no instruction file' in m and 'qbs_2010' in m for m in e)
    assert any('run.bat not found' in m for m in w)


# ---------------------------------------------------------------------- interpreter
def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return str(tmp_path / name)


def test_run_instructions_basic(tmp_path):
    out = write(tmp_path, 'o.txt', 'header\n  a  1.5  2.5\nwell3 = 7.25 units\nfixed 123.4567\n')
    # l2: line 2 of the file; the line starts with blanks so the first w lands before 'a', the second before 1.5
    ins = write(tmp_path, 'i.ins', 'pif @\nl2 w w !x! !y!\n@well3@ @=@ !z!\nl1 [f]7:14 (g)7:9\n')
    assert run_instructions(ins, out) == {'x': 1.5, 'y': 2.5, 'z': 7.25, 'f': 123.4567, 'g': 123.4567}


def test_run_instructions_w_gotcha(tmp_path):
    out = write(tmp_path, 'o.txt', 'well h_w2  98.5\n')
    ins = write(tmp_path, 'i.ins', 'pif @\nl1 w !h!\n')             # w skips 'well' only; reads h_w2
    with pytest.raises(InstructionError, match="read 'h_w2', not a number"):
        run_instructions(ins, out)
    ins = write(tmp_path, 'i.ins', 'pif @\nl1 w w !h!\n')
    assert run_instructions(ins, out) == {'h': 98.5}


def test_run_instructions_errors_and_continuation(tmp_path):
    out = write(tmp_path, 'o.txt', 'a 1\nb 2\n')
    ins = write(tmp_path, 'i.ins', 'pif @\n@zzz@ !x!\n')
    with pytest.raises(InstructionError, match='primary marker'):
        run_instructions(ins, out)
    ins = write(tmp_path, 'i.ins', 'pif @\nl3 !x!\n')
    with pytest.raises(InstructionError, match='beyond end'):
        run_instructions(ins, out)
    ins = write(tmp_path, 'i.ins', 'pif @\n@a@\n& !x!\nl1 @b@ !dum! \n')
    assert run_instructions(ins, out) == {'x': 1.0}
    ins = write(tmp_path, 'i.ins', 'pif @\nl1 t3 !x!\n')
    assert run_instructions(ins, out) == {'x': 1.0}


def test_semi_fixed_token_extends_beyond_columns(tmp_path):
    out = write(tmp_path, 'o.txt', 'val=  12345.678  end\n')
    ins = write(tmp_path, 'i.ins', 'pif @\nl1 (v)8:10\n')
    assert run_instructions(ins, out) == {'v': 12345.678}
