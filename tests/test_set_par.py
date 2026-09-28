"""`makepst set` and line-ending preservation.

Both exist because of the same failure mode: an edit that looks complete but leaves a coupled field behind.
Setting a parent's value without its tied children stops PEST_HP from starting; moving a value without its
preferred-value target lets the regularisation pull it straight back; and a round trip that flips CRLF to LF
stops the result comparing byte-for-byte with the file it came from.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import read_pst  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.writer import write_pst  # noqa: E402
from make_fixture import PST  # noqa: E402


def val(pst, name, col='PARVAL1'):
    row = pst.par.loc[pst.par['PARNME'] == name]
    return float(row[col].iloc[0])


def eq(pst, label):
    return str(pst.prior.loc[pst.prior['PINME'] == label, 'EQ'].iloc[0])


# ---------------------------------------------------------------------- set_par
def test_sets_fields_by_glob():
    p = read_pst(PST)
    report = p.set_par({'sy1_*': {'PARVAL1': 0.25, 'PARLBND': 0.1}})
    assert sorted(report['parameters']) == ['sy1_cc01', 'sy1_cc02']
    assert val(p, 'sy1_cc01') == 0.25 and val(p, 'sy1_cc01', 'PARLBND') == 0.1
    assert val(p, 'sy1_cc02') == 0.25


def test_unknown_pattern_raises():
    p = read_pst(PST)
    with pytest.raises(ValueError, match='no parameter matches'):
        p.set_par({'nosuchpar*': {'PARVAL1': 1.0}})


def test_unknown_column_raises():
    p = read_pst(PST)
    with pytest.raises(ValueError, match='not parameter columns'):
        p.set_par({'sy1_cc01': {'PARVAL9': 1.0}})


def test_tied_child_follows_its_parent():
    p = read_pst(PST)
    parent, child = val(p, 'hk1_cc01'), val(p, 'hk1_cc02')
    report = p.set_par({'hk1_cc01': {'PARVAL1': 100.0}})
    assert report['tied_rescaled'] == ['hk1_cc02']
    assert val(p, 'hk1_cc02') == pytest.approx(child * 100.0 / parent)
    # the ratio PEST maintains is unchanged, which is the point
    assert val(p, 'hk1_cc02') / 100.0 == pytest.approx(child / parent)


def test_tied_child_keeps_its_bounds_unless_the_new_value_leaves_them():
    p = read_pst(PST)
    lo, hi = val(p, 'hk1_cc02', 'PARLBND'), val(p, 'hk1_cc02', 'PARUBND')
    p.set_par({'hk1_cc01': {'PARVAL1': 60.0}})               # child stays inside [1, 300]
    assert (val(p, 'hk1_cc02', 'PARLBND'), val(p, 'hk1_cc02', 'PARUBND')) == (lo, hi)

    p = read_pst(PST)
    p.set_par({'hk1_cc01': {'PARVAL1': 250.0}})              # child would land above 300
    assert val(p, 'hk1_cc02') <= val(p, 'hk1_cc02', 'PARUBND')
    assert val(p, 'hk1_cc02') >= val(p, 'hk1_cc02', 'PARLBND')


def test_no_sync_ties_leaves_the_child_behind():
    p = read_pst(PST)
    child = val(p, 'hk1_cc02')
    p.set_par({'hk1_cc01': {'PARVAL1': 100.0}}, sync_ties=False)
    assert val(p, 'hk1_cc02') == child


def test_preferred_value_moves_with_the_parameter():
    p = read_pst(PST)
    p.set_par({'sy1_cc02': {'PARVAL1': 0.25}})
    assert float(eq(p, 'sy1_cc02').split('=')[1]) == pytest.approx(-0.60206, abs=1e-5)   # log10(0.25)
    p.set_par({'rchss': {'PARVAL1': 0.5}})                                               # linear form
    assert float(eq(p, 'rchss').split('=')[1]) == pytest.approx(0.5)


def test_relationships_are_left_alone():
    """Homogeneity / preferred difference state a relationship, so one parameter's value says nothing about it."""
    p = read_pst(PST)
    before = eq(p, 'sy1_cc01')
    report = p.set_par({'sy1_cc02': {'PARVAL1': 0.25}})
    assert eq(p, 'sy1_cc01') == before
    assert report['prior_retargeted'] == ['sy1_cc02']


def test_no_sync_prior_leaves_the_target_behind():
    p = read_pst(PST)
    before = eq(p, 'sy1_cc02')
    p.set_par({'sy1_cc02': {'PARVAL1': 0.25}}, sync_prior=False)
    assert eq(p, 'sy1_cc02') == before


def test_a_retargeted_file_still_validates(tmp_path):
    p = read_pst(PST)
    p.set_par({'sy1_*': {'PARVAL1': 0.25}})
    out = str(tmp_path / 'out.pst')
    write_pst(p, out, dump_tpl=False)
    assert read_pst(out) is not None


# ---------------------------------------------------------------------- line endings
def crlf_lf(path):
    raw = open(path, 'rb').read()
    return raw.count(b'\r\n'), raw.count(b'\n') - raw.count(b'\r\n')


def test_round_trip_keeps_crlf(tmp_path):
    src = str(tmp_path / 'crlf.pst')
    with open(src, 'wb') as f:
        f.write(open(PST, 'rb').read().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    out = str(tmp_path / 'out.pst')
    write_pst(read_pst(src), out, dump_tpl=False)
    crlf, lf = crlf_lf(out)
    assert lf == 0 and crlf > 0


def test_round_trip_keeps_lf(tmp_path):
    # the checked-out fixture is CRLF where git converts line endings (Windows CI)
    src = str(tmp_path / 'lf.pst')
    with open(src, 'wb') as f:
        f.write(open(PST, 'rb').read().replace(b'\r\n', b'\n'))
    out = str(tmp_path / 'out.pst')
    write_pst(read_pst(src), out, dump_tpl=False)
    crlf, lf = crlf_lf(out)
    assert crlf == 0 and lf > 0


def test_eol_can_be_forced(tmp_path):
    out = str(tmp_path / 'forced.pst')
    write_pst(read_pst(PST), out, dump_tpl=False, eol='\r\n')
    crlf, lf = crlf_lf(out)
    assert lf == 0 and crlf > 0


# ---------------------------------------------------------------------- the CLI
def test_cli_set(tmp_path, capsys):
    out = str(tmp_path / 'cli.pst')
    main(['set', PST, out, '--par', 'sy1_*:parval1=0.25,parlbnd=0.1', '--no_manifest'])
    p = read_pst(out)
    assert val(p, 'sy1_cc01') == 0.25 and val(p, 'sy1_cc02', 'PARLBND') == 0.1
    assert float(eq(p, 'sy1_cc02').split('=')[1]) == pytest.approx(-0.60206, abs=1e-5)
    assert 'parameters set' in capsys.readouterr().out


def test_cli_set_rejects_a_malformed_spec(tmp_path):
    with pytest.raises(SystemExit):
        main(['set', PST, str(tmp_path / 'x.pst'), '--par', 'sy1_cc01', '--no_manifest'])


def test_cli_set_forces_eol(tmp_path):
    out = str(tmp_path / 'cli_crlf.pst')
    main(['set', PST, out, '--par', 'sy1_cc01:parval1=0.25', '--eol', 'crlf', '--no_manifest'])
    crlf, lf = crlf_lf(out)
    assert lf == 0 and crlf > 0
