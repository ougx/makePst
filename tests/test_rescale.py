"""`makepst rescale` / Pst.rescale_par and the SCALE-aware fill_parval.

Rescaling is only useful if nothing the model or the objective function sees moves: every model input value
(PARVAL1 * SCALE + OFFSET) and every prior-information residual must be the same number before and after.
Those two invariants are what these tests pin down; the rest is selection and reporting.
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import read_pst  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.model_files import scaled_values  # noqa: E402
from makepst.pst import _terms  # noqa: E402
from make_fixture import DATA, PST  # noqa: E402


def row(pst, name):
    return pst.par.loc[pst.par['PARNME'] == name].iloc[0]


def residuals(pst, model_values):
    """Each prior equation's residual when the model sees `model_values` ({name: value})."""
    par = pst.par.set_index('PARNME')
    internal = {n: (v - float(par.at[n, 'OFFSET'])) / float(par.at[n, 'SCALE']) for n, v in model_values.items()}
    out = {}
    for r in pst.prior.itertuples():
        lhs, _, rhs = r.EQ.partition('=')
        total = sum(s * float(c or 1) * (np.log10(internal[n.lower()]) if lg else internal[n.lower()])
                    for s, c, n, lg in _terms(lhs))
        out[r.PINME] = total - float(rhs)
    return out


# ---------------------------------------------------------------------- invariants
def test_model_sees_the_same_values():
    p = read_pst(PST)
    before = scaled_values(p.par)
    report = p.rescale_par()
    assert len(report['rescaled']) == p.npar and not report['skipped']
    assert (p.par['PARVAL1'] == 1.0).all()
    after = scaled_values(p.par)
    assert all(after[n] == pytest.approx(before[n], rel=1e-12) for n in before)
    # bounds follow the value; OFFSET is untouched
    dg = row(p, 'dg00025')
    assert (dg.PARLBND, dg.PARUBND, dg.SCALE, dg.OFFSET) == pytest.approx((0.9, 1.1, 1000, -1000))


def test_prior_residuals_are_unchanged():
    p = read_pst(PST)
    start = scaled_values(p.par)
    moved = {n: v * 1.7 if v else 0.3 for n, v in start.items()}       # anywhere, not just the start
    before = [residuals(p, v) for v in (start, moved)]
    report = p.rescale_par()
    assert sorted(report['prior_rewritten']) == sorted(p.prior['PINME'])
    after = [residuals(p, v) for v in (start, moved)]
    for b, a in zip(before, after):
        assert a == pytest.approx(b, abs=1e-9)


def test_rewritten_equations():
    p = read_pst(PST)
    p.rescale_par()
    eq = dict(zip(p.prior['PINME'], p.prior['EQ']))
    assert eq['hk1_cc03'] == f'1.0 * log(hk1_cc03) = {2 - np.log10(123.8187):.11g}'   # log: constant moves
    assert eq['rchss'] == '0.3428223 * rchss = 0.35'                                   # linear: coefficient


def test_undo_restores_the_file():
    original = read_pst(PST)
    p = read_pst(PST)
    p.rescale_par()
    p.rescale_par(undo=True)
    for c in ('PARVAL1', 'PARLBND', 'PARUBND', 'SCALE', 'OFFSET'):
        assert p.par[c].astype(float).values == pytest.approx(original.par[c].astype(float).values, rel=1e-10)
    assert p.prior['EQ'].tolist() == original.prior['EQ'].tolist()


def test_negative_value_swaps_bounds_and_zero_is_skipped():
    p = read_pst(PST)
    i = p.par.index[p.par['PARNME'] == 'rchss'][0]
    p.par.loc[i, ['PARVAL1', 'PARLBND', 'PARUBND']] = [-2.0, -4.0, -1.0]
    j = p.par.index[p.par['PARNME'] == 'dg00025'][0]
    p.par.loc[j, 'PARVAL1'] = 0.0
    before = scaled_values(p.par)
    report = p.rescale_par()
    assert report['skipped'] == {'dg00025': 'PARVAL1 is zero'}
    r = row(p, 'rchss')
    assert (r.PARVAL1, r.PARLBND, r.PARUBND, r.SCALE) == pytest.approx((1, 0.5, 2, -2))
    assert scaled_values(p.par)['rchss'] == pytest.approx(before['rchss'])


# ---------------------------------------------------------------------- selection
def test_selection_by_pattern_group_and_fixed():
    p = read_pst(PST)
    assert read_pst(PST).rescale_par(patterns='hk*')['rescaled'] == [f'hk1_cc0{i}' for i in range(1, 7)]
    report = p.rescale_par(patterns=['hk*', 'rch*'], groups='hk', include_fixed=False)
    assert report['rescaled'] == ['hk1_cc01', 'hk1_cc02', 'hk1_cc03', 'hk1_cc06']   # both filters; no fixed
    assert row(p, 'hk1_cc04').PARVAL1 == pytest.approx(0.001007841)


def test_tied_child_follows_its_parent():
    p = read_pst(PST)
    report = p.rescale_par(patterns='hk1_cc01')
    assert report['rescaled'] == ['hk1_cc01', 'hk1_cc02']
    assert row(p, 'hk1_cc02').SCALE == pytest.approx(90.26792)


def test_unmatched_selection_raises():
    with pytest.raises(ValueError, match='no parameter matches'):
        read_pst(PST).rescale_par(patterns='nosuch*')
    with pytest.raises(ValueError, match='no parameter in groups'):
        read_pst(PST).rescale_par(groups='nosuch')


def test_unparseable_equation_changes_nothing():
    p = read_pst(PST)
    p.prior.loc[0, 'EQ'] = '1.0 * hk1_cc01 * hk1_cc03 = 1'
    before = p.par.copy()
    with pytest.raises(ValueError, match='cannot parse prior equation'):
        p.rescale_par()
    assert p.par.equals(before)


# ---------------------------------------------------------------------- what cannot be converted
def test_warns_about_parameter_unit_settings():
    p = read_pst(PST)
    p.pestpp.append(('base_jacobian_filename', 'old.jcb'))
    notes = ' | '.join(p.rescale_par()['warnings'])
    assert 'parameter group rch: INCTYP absolute' in notes
    assert "absolute(n) change limits" in notes and 'dg00025' in notes
    assert 'base_jacobian' in notes
    assert not read_pst(PST).rescale_par(patterns='hk*')['warnings']


def test_generated_prior_rows_become_explicit():
    p = read_pst(PST)
    p.prior = p.prior.iloc[0:0]
    p.par['PRIOR'], p.par['WEIGHT'] = None, None
    for name, prior, weight in (('hk1_cc03', 100, 1), ('hk1_cc01', 'hk1_cc03', 2)):
        p.par.loc[p.par['PARNME'] == name, ['PRIOR', 'WEIGHT']] = [prior, weight]
    p._build_prior()
    assert p._auto_labels == {'hk1_cc01', 'hk1_cc03'}
    before = residuals(p, scaled_values(p.par))
    report = p.rescale_par(patterns='hk*')
    assert sorted(report['prior_frozen']) == ['hk1_cc01', 'hk1_cc03']
    assert not p._auto_labels and p.par['PRIOR'].isna().all()
    assert residuals(p, scaled_values(p.par)) == pytest.approx(before, abs=1e-9)
    assert not [f for f in p.validate().errors if f.where == 'prior information']


# ---------------------------------------------------------------------- fill_parval through SCALE / OFFSET
def test_fill_parval_converts_par_files_from_another_scale(tmp_path):
    ref = read_pst(PST)
    ref.fill_parval(os.path.join(DATA, 'demo.par'))              # written under the file's own scales
    p = read_pst(PST)
    p.rescale_par()
    p.fill_parval(os.path.join(DATA, 'demo.par'))                # written before the rescale
    got, want = scaled_values(p.par), scaled_values(ref.par)
    assert all(got[n] == pytest.approx(want[n], rel=1e-12) for n in want)

    # and back: a .par from the rescaled run into the original file
    par = tmp_path / 'scaled.par'
    par.write_text('single point\n' + ''.join(
        f'{r.PARNME} {r.PARVAL1!r} {float(r.SCALE)!r} {float(r.OFFSET)!r}\n' for r in p.par.itertuples()))
    back = read_pst(PST)
    back.fill_parval(str(par))
    assert back.par['PARVAL1'].astype(float).values == pytest.approx(ref.par['PARVAL1'].astype(float).values)


def test_fill_parval_same_scale_copies_exactly():
    p = read_pst(PST)
    p.fill_parval(os.path.join(DATA, 'demo.par'))
    assert row(p, 'hk1_cc01').PARVAL1 == 99.4755 and row(p, 'dg00025').PARVAL1 == 2000


# ---------------------------------------------------------------------- command line
def test_cli_rescale_and_undo(tmp_path, capsys):
    out, back = str(tmp_path / 'r.pst'), str(tmp_path / 'b.pst')
    main(['rescale', PST, out, '--par', 'hk*,sy*', '--exclude_fixed', '--no_manifest'])
    assert '6 parameters rescaled to 1' in capsys.readouterr().out
    r = read_pst(out)
    assert row(r, 'sy1_cc01').PARVAL1 == 1 and row(r, 'sy1_cc01').SCALE == pytest.approx(0.15)
    assert row(r, 'hk1_cc05').PARVAL1 == 5                                        # fixed, excluded
    main(['rescale', out, back, '--undo', '--no_manifest'])
    b, o = read_pst(back), read_pst(PST)
    assert b.par['PARVAL1'].astype(float).values == pytest.approx(o.par['PARVAL1'].astype(float).values)
    assert (b.par['SCALE'].astype(float) == 1).all()
