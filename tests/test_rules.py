"""The pestchek-derived rules in makepst/rules.py and the static template / instruction checks."""
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import read_pst  # noqa: E402
from makepst.checks import instruction_problems, template_problems  # noqa: E402
from makepst.rules import check_control, check_groups, check_observations, check_parameters, check_prior  # noqa: E402
from make_fixture import PST  # noqa: E402


def msgs(findings, severity=None):
    return [f.message for f in findings if severity is None or f.severity == severity]


def has(findings, text, severity='error'):
    return any(text in m for m in msgs(findings, severity))


def fixture():
    return read_pst(PST)


# ---------------------------------------------------------------------- parameters
def test_fixture_parameters_are_clean():
    assert msgs(check_parameters(fixture()), 'error') == []


def test_parameter_rules():
    p = fixture()
    i = p.par.set_index('PARNME')
    p.par.loc[i.index.get_loc('hk1_cc01'), 'PARCHGLIM'] = 'relative'        # log must be factor
    p.par.loc[i.index.get_loc('hk1_cc03'), 'PARCHGLIM'] = 'absolute(7)'     # no absparmax(7)
    p.par.loc[i.index.get_loc('hk1_cc06'), 'PARCHGLIM'] = 'bogus'
    p.par.loc[i.index.get_loc('sy1_cc01'), 'PARUBND'] = 0.01               # equal to lower
    p.par.loc[i.index.get_loc('rch2002'), 'PARLBND'] = -0.5                # factor-limited, opposite signs
    p.par.loc[i.index.get_loc('rch2002'), 'PARTRANS'] = 'none'
    p.par.loc[i.index.get_loc('rch2002'), 'PARCHGLIM'] = 'factor'
    p.par.loc[i.index.get_loc('rchss'), 'SCALE'] = 0
    p.par.loc[i.index.get_loc('hk1_cc02'), 'TIETO'] = 'hk1_cc02'          # tied to itself
    p.par.loc[i.index.get_loc('dg00025'), 'PARGP'] = 'none'
    f = check_parameters(p)
    assert has(f, 'log-transformed parameters must be factor-limited')
    assert has(f, 'absparmax(n)= value') and has(f, 'n = 7')
    assert has(f, 'PARCHGLIM must be relative, factor or absolute(n): hk1_cc06')
    assert has(f, 'upper bound equal to or below the lower bound: sy1_cc01')
    assert has(f, 'factor-limited parameters need bounds of the same sign')
    assert has(f, 'SCALE must not be zero: rchss')
    assert has(f, 'tied to itself: hk1_cc02')
    assert has(f, 'group "none" is reserved')


def test_parameter_increment_warnings_and_all_fixed():
    p = fixture()
    p.pargp.loc[p.pargp['PARGPNME'] == 'hk', 'DERINC'] = 2.0                # relative 200 %: increment > range / 3.2
    f = check_parameters(p)
    assert has(f, 'derivative increment larger than a third', 'warning')
    p = fixture()
    p.par['PARTRANS'] = 'fixed'
    assert has(check_parameters(p), 'no parameters to estimate')
    p = fixture()
    p.par.loc[p.par['PARNME'] == 'hk1_cc03', 'PARVAL1'] = 0
    p.par.loc[p.par['PARNME'] == 'hk1_cc03', 'PARTRANS'] = 'none'
    p.par.loc[p.par['PARNME'] == 'hk1_cc03', 'PARLBND'] = -1
    f = check_parameters(p)
    assert has(f, 'increment will be zero', 'warning')


# ---------------------------------------------------------------------- groups
def test_group_rules():
    p = fixture()
    p.pargp.loc[0, 'INCTYP'] = 'weird'
    p.pargp.loc[1, 'FORCEN'] = 'always_5'          # with parabolic -> incompatible
    p.pargp.loc[2, 'DERINCLB'] = -1
    p.pargp = pd.concat([p.pargp, p.pargp.iloc[[0]]], ignore_index=True)     # duplicate name
    f = check_groups(p)
    assert has(f, 'INCTYP must be one of')
    assert has(f, 'DERMTHD must be parabolic/best_fit/outside_pts with switch/always_2/always_3')
    assert has(f, 'DERINCLB must not be negative')
    assert has(f, 'duplicate group names: hk')
    assert has(check_groups(fixture()), 'log-transformed parameters with a non-relative increment type: rch', 'warning')


# ---------------------------------------------------------------------- observations / prior
def test_observation_rules():
    p = fixture()
    assert has(check_observations(p), 'zero weight in groups: headss', 'info')
    p.obsgp_order = list(p.obsgp) + ['unused', 'extra']
    assert has(check_observations(p), 'belong to observation groups: unused, extra (dropped on write)', 'warning')
    p.obs.loc[0, 'OBSNME'] = 'dum'
    assert has(check_observations(p), '"dum" is not allowed')
    p = fixture()
    p.prior = p.prior.iloc[0:0]
    p.obs['OBGNME'] = 'head'
    assert has(check_observations(p), 'regularisation mode needs at least one observation group')
    p.pestmode = 'prediction'
    assert has(check_observations(p), 'exactly one observation in group "predict" (0 found)')


def test_prior_rules():
    p = fixture()
    p.prior.loc[0, 'WEIGHT'] = -1
    p.prior.loc[1, 'PINME'] = 'qbs_2010'                                     # same as an observation
    p.prior.loc[2, 'EQ'] = '1.0 * log(sy1_cc01) + 1.0 * log(sy1_cc01) = 0'   # parameter twice
    p.prior.loc[3, 'EQ'] = '0.0 * log(sy1_cc02) = 0'                        # all-zero factors
    p.prior.loc[4, 'OBGNME'] = 'predict'
    f = check_prior(p)
    assert has(f, 'weight must not be negative')
    assert has(f, 'label is also an observation name: qbs_2010')
    assert has(f, 'referenced more than once')
    assert has(f, 'every parameter factor is zero')
    assert has(f, 'cannot belong to group "predict"')


# ---------------------------------------------------------------------- control data
def test_control_rules():
    p = fixture()
    assert msgs(check_control(p), 'error') == []
    assert has(check_control(p), 'MAXSING (50) exceeds the number of adjustable parameters (8)', 'warning')
    p.control.update({'rlamfac': 0.5, 'phiratsuf': 1.5, 'facparmax': 1, 'noptmax': -3, 'uptestmin': 71,
                      'uptestlim': 2, 'eigthresh': 1.0, 'phimaccept': 4.0, 'relparstp': 5, 'relparmax': 3,
                      'lastrun': 2, 'jacupdate': 5, 'maxcompdim': 100, 'doaui': 'aui', 'wfmin': 10, 'wfmax': 1})
    f = check_control(p)
    for text in ('RLAMFAC must have an absolute value greater than one', 'PHIRATSUF must be between zero and one',
                 'FACPARMAX must be greater than one', 'NOPTMAX must be -2 or greater',
                 'UPTESTMIN must be between 3 and 70', 'UPTESTLIM must be between 3 and 150',
                 'EIGTHRESH must be zero or more and less than one', 'PHIMACCEPT must be greater than PHIMLIM',
                 'RELPARSTP must be less than RELPARMAX', 'LASTRUN must be 0 or 1',
                 'JACUPDATE must be zero when MAXCOMPDIM', 'automatic user intervention is not permitted',
                 'WFMIN must be less than WFMAX'):
        assert has(f, text), text
    p = fixture()
    p.control['phimaccept'] = 6.5                                            # >= 1.2 * phimlim (5)
    assert has(check_control(p), 'PHIMACCEPT must be less than 1.2 times PHIMLIM')
    p = fixture()
    p.control['noptmax'] = 0
    assert has(check_control(p), 'NOPTMAX is 0', 'warning')


def test_tied_parval_outside_its_own_bounds():
    """PEST_HP refuses to start on this, and it is exactly what a parent moved by hand leaves behind."""
    p = fixture()
    i = list(p.par['PARNME']).index('hk1_cc02')                  # tied to hk1_cc01
    assert not has(check_parameters(p), 'tied PARVAL1 outside its own bounds')
    p.par.loc[i, 'PARVAL1'] = 5000.0                             # its bounds are 1 - 300
    assert has(check_parameters(p), 'tied PARVAL1 outside its own bounds')
    p.par.loc[i, 'PARUBND'] = 6000.0                             # bounds rescaled with it: quiet again
    assert not has(check_parameters(p), 'tied PARVAL1 outside its own bounds')


def test_version_sensitive_pestpp_option():
    p = fixture()
    assert not has(check_control(p), 'not accepted by every PEST++ build', 'info')
    p.pestpp.append(('glm_hp_lambdas', 'true'))
    assert has(check_control(p), '++glm_hp_lambdas is not accepted by every PEST++ build', 'info')


def test_pest_hp_variables_and_memory_warning():
    p = fixture()
    assert has(check_control(p), 'PEST_HP-only variables (plain PEST needs /hpstart): WIN_MRUN_HOURS, RRFSAVE, UPTESTMIN', 'info')
    p.control['rrfsave'] = 'norrfsave'
    del p.control['win_mrun_hours']
    assert has(check_control(p), 'PEST_HP-only variables (plain PEST needs /hpstart): UPTESTMIN', 'info')
    del p.control['uptestmin']
    assert not has(check_control(p), 'PEST_HP-only', 'info')
    assert not has(check_control(p), 'saves significant memory', 'warning')     # 8 adjustable parameters
    big = pd.concat([p.par.assign(PARNME=p.par['PARNME'] + f'_{i}') for i in range(40)], ignore_index=True)
    p.par = big                                                                  # 320 adjustable
    assert has(check_control(p), '320 adjustable parameters: setting ICOV, ICOR and IEIG to 0', 'warning')
    p.control.update({'icov': 0, 'icor': 0, 'ieig': 0})
    assert not has(check_control(p), 'saves significant memory', 'warning')


# ---------------------------------------------------------------------- template / instruction syntax
def test_template_problems(tmp_path):
    t = tmp_path / 'a.tpl'
    t.write_text('ptf ~\n~~ ~ k2 ~\t~k3~ ~\tk4 ~\n')
    problems = template_problems(t)
    assert any("'' is narrower than 3" in m for m in problems)          # ~~ : nothing fits in it
    assert any("tab character inside the parameter space for 'k4'" in m for m in problems)
    assert not any("'k2'" in m or "'k3'" in m for m in problems)


def test_instruction_problems(tmp_path):
    i = tmp_path / 'a.ins'
    i.write_text('pif @\n'
                 '& !x!\n'                       # continuation on the first line
                 'l0 !y!\n'                      # l must be positive
                 'w !z!\n'                       # line must begin with l, marker or &
                 'l1 t0 [q]0:5 (r)7:3 [dum]1:2\n'
                 'l1 !unbalanced\n'
                 'l1 @marker !a! zzz\n'
                 '@@ !b!\n')
    problems = instruction_problems(i)
    joined = '\n'.join(problems)
    for text in ('first instruction line cannot begin', 'integer after "l" must be positive',
                 'must begin with "l", a marker or "&"', 'integer after "t" must be positive',
                 'columns n1:n2 must be positive and increasing in [q]0:5',
                 'columns n1:n2 must be positive and increasing in (r)7:3',
                 '"dum" is only allowed for non-fixed', '"!" not balanced', "illegal instruction 'zzz'",
                 'marker has zero length'):
        assert text in joined, text
    good = tmp_path / 'good.ins'
    good.write_text('pif @\n@head@ w !h1! [h2]5:12 (h3)14:20\nl1 !dum! !h4!\n& t30 !h5!\n')
    assert instruction_problems(good) == []
    bad = tmp_path / 'bad.ins'
    bad.write_text('pif !\nl1 !x!\n')
    assert instruction_problems(bad) == ["illegal marker delimiter '!'"]
