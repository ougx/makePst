"""Run with:  python -m pytest tests -q   (from the project folder; needs tr13.pst / tr13.xlsm there)."""
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from makepst import Pst, from_text, load_table, read_pst, to_text, to_workbook, update_workbook  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.sections import CONTROL, REGUL  # noqa: E402

REF_PST = os.path.join(ROOT, 'tr13.pst')
REF_XLSM = os.path.join(ROOT, 'tr13.xlsm')
needs_ref = pytest.mark.skipif(not (os.path.exists(REF_PST) and os.path.exists(REF_XLSM)),
                               reason='reference tr13.pst / tr13.xlsm not present')


# ---------------------------------------------------------------------- sections
def test_control_parse_render_roundtrip():
    text = [
        'restart   regularisation',
        '1068      41747     12        185       12                # comment',
        '18        7         single    point     1         0         0',
        '10        2         0.3       0.03      10  999  lamforgive derforgive win_mrun_hours=1.1   uptestmin=70',
        '3         3         0.001                         absparmax(1)=0.1 absparmax(2)=20',
        '0.1                           noaui',
        '10        0.01      3         3         0.001     3         0         0         -1',
        '1         1         1          1 jcosave verboserec jcosaveitn reisaveitn parsaveitn  rrfsave',
    ]
    v = CONTROL.parse(text)
    assert v['pestmode'] == 'regularisation' and v['npar'] == 1068 and v['nobsgp'] == 12
    assert v['jacupdate'] == 999 and v['lamforgive'] == 'lamforgive' and v['win_mrun_hours'] == 1.1
    assert v['absparmax'] == 'absparmax(1)=0.1 absparmax(2)=20'
    assert v['doaui'] == 'noaui' and 'noptswitch' not in v
    assert v['ires'] == 1 and v['jcosave'] == 'jcosave' and 'parsaverun' not in v
    out = CONTROL.render(v).splitlines()[1:]
    assert CONTROL.parse(out) == v


def test_regul_defaults_derive_phimaccept():
    v = REGUL.values({'phimlim': 2.0})
    assert v['phimaccept'] == pytest.approx(2.1)
    assert '2.1' in REGUL.render({'phimlim': 2.0})


# ---------------------------------------------------------------------- model
def small_pst(mode='estimation'):
    p = Pst(mode)
    p.add_pargp(pd.DataFrame({'PARGPNME': ['hk', 'unused'], 'INCTYP': 'relative', 'DERINC': 0.1,
                              'DERINCLB': 0, 'FORCEN': 'switch', 'DERINCMUL': 2, 'DERMTHD': 'parabolic'}))
    p.add_par(pd.DataFrame({'PARNME': ['K1', 'k2', 'k3', 'k4'], 'PARTRANS': ['log', 'tied', 'fixed', 'none'],
                            'PARCHGLIM': 'factor', 'PARVAL1': [1.23456789012, 2, 3, 4], 'PARLBND': 0.1,
                            'PARUBND': 10, 'PARGP': 'hk', 'SCALE': 1, 'OFFSET': 0, 'DERCOM': 1,
                            'TIETO': [None, 'k1', None, None],
                            'PRIOR': [2.0, None, None, 'k1'], 'WEIGHT': [1, None, None, 3]}))
    p.add_obs(pd.DataFrame({'OBSNME': ['o1', 'o2'], 'OBSVAL': [1.5, 2.5], 'WEIGHT': [1, 0], 'OBGNME': ['g', 'h']}))
    p.add_io(pd.DataFrame([['cmd', 'run.bat', None], ['tpl', 'a.tpl', 'a.in'], ['ins', 'b.ins', 'b.out']]))
    p.add_pp(pd.DataFrame([['lambdas', '0.1,1,10'], ['n', 3.0], ['blank', None]]))
    p.add_comment(comment='hello')
    return p


def test_write_read_small():
    p = small_pst('regul')
    p.par.loc[3, 'PARTRANS'] = 'log'            # k4 = k1 needs both in log(), as k1 is log-transformed
    text = to_text(p.normalize())
    q = from_text(text)
    assert q.pestmode == 'regularisation' and q.comments == ['hello']
    assert list(q.par['PARNME']) == ['k1', 'k2', 'k3', 'k4']
    assert q.par['PARVAL1'][0] == pytest.approx(1.23456789012, rel=1e-10)
    assert q.par.set_index('PARNME').loc['k2', 'TIETO'] == 'k1'
    assert list(q.pargp['PARGPNME']) == ['hk']                       # unused group dropped
    assert q.obsgp == ['g', 'h', 'regulhk']                          # prior groups listed
    assert q.pestpp == [('lambdas', '0.1,1,10'), ('n', '3')]
    assert list(q.prior['EQ']) == ['1.0 * log(k1) = 0.30102999566', '1.0 * log(k4) - 1.0 * log(k1) = 0']
    assert to_text(q) == text


def test_equation_params_scientific_notation():
    from makepst.pst import equation_params
    assert equation_params('1.0 * log(k1) = 1e-3') == ['k1']
    assert equation_params('2.5E+02 * k1 - 1.0 * k2 = -1e5') == ['k1', 'k2']
    assert equation_params('-1.0*log(hk1_cc01)+1.0*log(hk1_cc03)=0') == ['hk1_cc01', 'hk1_cc03']


def test_prior_with_scientific_constant_survives_validation():
    p = small_pst('regul')
    p.add_prior(pd.DataFrame({'PINME': ['sci'], 'EQ': ['1.0 * log(k1) = 1e-3'], 'WEIGHT': [1], 'OBGNME': ['r']}))
    p.validate()
    assert 'sci' in set(p.prior['PINME'])


def test_add_tied_marks_parameter_tied():
    p = small_pst()
    p.par['PARTRANS'] = ['log', 'log', 'fixed', 'none']      # nothing pre-marked as tied
    p.par = p.par.drop(columns='TIETO')
    p.add_tied(pd.DataFrame([['K4', 'k1'], ['k1', 'k1'], ['ghost', 'k1']]))
    par = p.par.set_index('PARNME')
    assert par.loc['k4', 'PARTRANS'] == 'tied' and par.loc['k4', 'TIETO'] == 'k1'
    assert par.loc['k1', 'PARTRANS'] == 'log'                # self-tie ignored
    p.validate()
    assert 'k4  k1' in to_text(p)


def test_estimation_mode_ignores_prior_columns():
    p = small_pst('estimation')
    assert 'prior information' not in to_text(p) and p.nprior == 0


def _errors(p):
    return '\n'.join(str(f) for f in p.validate().errors)


def test_validate_errors():
    p = small_pst()
    p.par.loc[1, 'TIETO'] = 'nope'
    assert 'tied to a parameter that does not exist: k2' in _errors(p)
    with pytest.raises(ValueError, match='does not exist'):
        to_text(p)
    p = small_pst()
    p.par.loc[3, 'PARGP'] = 'ghost'
    assert 'used but not defined: ghost' in _errors(p)
    p = small_pst()
    p.add_obs(pd.DataFrame({'OBSNME': ['o1'], 'OBSVAL': [1], 'WEIGHT': [1], 'OBGNME': ['g']}))
    assert 'observations: duplicate names: o1' in _errors(p)


def test_validate_does_not_change_the_tables():
    p = small_pst()
    p.par.loc[1, 'TIETO'] = 'k3'
    before = p.par.copy()
    report = p.validate()
    pd.testing.assert_frame_equal(p.par, before)
    assert [f.kind for f in report.fixes_available if f.kind == 'tied_to_fixed'] == ['tied_to_fixed']


def test_tied_to_fixed_becomes_fixed(capsys):
    p = small_pst()
    p.par.loc[1, 'TIETO'] = 'k3'
    assert 'tied parameters have fixed targets' in _errors(p)
    p.normalize()
    assert p.par.loc[1, 'PARTRANS'] == 'fixed'


def test_dangling_prior_dropped(capsys):
    p = small_pst('regul')
    p.add_prior(pd.DataFrame({'PINME': ['bad'], 'EQ': ['1.0 * log(k3) = 1'], 'WEIGHT': [1], 'OBGNME': ['r']}))
    assert "references fixed, tied or missing parameters: ['bad']" in _errors(p)
    p.normalize()
    assert 'bad' not in set(p.prior['PINME'])
    assert 'fixed, tied or missing' in capsys.readouterr().out


def test_out_of_bounds_is_a_warning_and_still_written():
    p = small_pst()
    p.par.loc[0, 'PARVAL1'] = 100
    report = p.validate()
    assert report.ok and any('outside bounds: k1' in str(f) for f in report.warnings)
    with pytest.warns(UserWarning, match='outside their bounds'):
        assert 'k1' in to_text(p)


def test_control_sheet_and_computed_ignored():
    p = Pst()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter('always')
        p.set_control(pd.DataFrame({'LINE': [1, 1, 2], 'NAME': ['noptmax', 'npar', 'bogus'],
                                    'DEFAULT': [None] * 3, 'VALUE': [7, 99, 1]}))
    assert p.control == {'noptmax': 7} and len(w) == 2


# ---------------------------------------------------------------------- reference data
@needs_ref
def test_reference_identity():
    p = read_pst(REF_PST)
    text = to_text(p)
    assert to_text(from_text(text)) == text


@needs_ref
def test_reference_build_matches(tmp_path):
    ref = read_pst(REF_PST)
    p = Pst()
    p.set_control(load_table(f'{REF_XLSM},CONTROL'))
    p.pestmode = 'regularisation'                # the command line mode wins over the CONTROL sheet
    p.add_pargp(load_table(f'{REF_XLSM},PARGP'))
    for s in 'PAR_HK PAR_VK PAR_SS PAR_SY PAR_SFR PAR_Other PAR_GHBdh PAR_RCH'.split():
        p.add_par(load_table(f'{REF_XLSM},{s}'))
    for s in 'OBS_HSS OBS_HEAD OBS_DHDT OBS_QSS OBS_FLOW OBS_LEAK OBS_LAKE'.split():
        p.add_obs(load_table(f'{REF_XLSM},{s}'))
    p.add_io(load_table(f'{REF_XLSM},IO'))
    for s in ('PPcntl', 'PPglm'):
        p.add_pp(load_table(f'{REF_XLSM},{s}'))
    new = from_text(to_text(p))

    assert new.counts == ref.counts and new.obsgp == ref.obsgp
    assert new.tpl == ref.tpl and new.ins == ref.ins and new.cmd == ref.cmd and new.pestpp == ref.pestpp
    # the reference was written with 6 significant digits; jacupdate was dropped by the old script
    for a, b, key, num in ((ref.par, new.par, 'PARNME', 'PARVAL1'), (ref.obs, new.obs, 'OBSNME', 'OBSVAL')):
        a, b = a.set_index(key).sort_index(), b.set_index(key).sort_index()
        assert list(a.index) == list(b.index)
        assert np.allclose(a[num].astype(float), b[num].astype(float), rtol=5e-6)
    assert {k: v for k, v in new.control.items() if k != 'jacupdate'} == ref.control
    assert new.control['jacupdate'] == 999


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(['--version'])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    from makepst import __version__
    assert f'makepst {__version__}' in captured.out + captured.err


def test_pestpp_regex_extended():
    text = (
        "pcf\n"
        "* control data\n"
        "restart estimation\n"
        "1 1 1 0 1\n"
        "1 1 single point 1 0 0\n"
        "1.0 1.0 0.001 0.001 1.0 10\n"
        "0.1 1.0 1.0\n"
        "10 0.01 3 3 0.001 1 0 0 -1\n"
        "1 1 1 1 1 1 1 1 1 1 1 1\n"
        "* parameter groups\n"
        "g relative 0.01 0 switch 2 parabolic\n"
        "* parameter data\n"
        "p1 log factor 1 0.1 10 g 1 0 1\n"
        "* observation groups\n"
        "g\n"
        "* observation data\n"
        "o1 1 1 g\n"
        "* model command line\n"
        "cmd\n"
        "* model input/output\n"
        "a.tpl a.in\n"
        "++ies_parameter_csv(file.csv)\n"
        "++svd.truncation(0.01)\n"
    )
    p = from_text(text)
    assert p.pestpp == [('ies_parameter_csv', 'file.csv'), ('svd.truncation', '0.01')]


def test_load_table_xls_engine_selection(tmp_path, monkeypatch):
    import pandas as pd
    called_engines = []

    def mock_read_excel(path, sheet, engine=None):
        called_engines.append(engine)
        return pd.DataFrame({'COL': [1]})

    monkeypatch.setattr(pd, 'read_excel', mock_read_excel)
    load_table('sample.xls,Sheet1')
    assert called_engines == [None]

    called_engines.clear()
    load_table('sample.xlsx,Sheet1')
    assert called_engines == ['openpyxl']

