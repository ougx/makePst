"""PEST_HP control variables: kept through every round trip, checked for what PEST can read."""
import json
import os
import shutil
import sys
import warnings

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import Pst, from_text, read_pst, to_text  # noqa: E402
from makepst.checks import validate  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.diff import compare  # noqa: E402
from makepst.pestpp import PESTPP_REGISTRY_VERSION  # noqa: E402
from makepst.rules import INTEGER, REAL, TEXT, check_control  # noqa: E402
from makepst.sections import ALL_SECTIONS, FIELD_SECTION, PESTCHEK_SOURCE_VERSION  # noqa: E402
from make_fixture import PST  # noqa: E402

DATA = os.path.join(HERE, 'data')
LINE4 = 'lamforgive win_mrun_hours=1.5 uptestmin=70'


def read(path):
    with open(path) as f:
        return f.read()


def hp_text(extra4='', extra6='', extra7=''):
    text = read(PST)
    text = text.replace(LINE4, f'{LINE4} {extra4}'.rstrip())
    text = text.replace('0.1        noaui\n', f'0.1        noaui {extra6}'.rstrip() + '\n')
    return text.replace('-1  # noptmax', f'-1 {extra7}  # noptmax')


def errors(pst, text):
    return any(text in f.message for f in check_control(pst) if f.severity == 'error')


def test_every_pest_hp_variable_round_trips():
    text = hp_text('run_slow_fac=5 run_abandon_fac = 3 uptestlim=100', 'jcowarnthresh=1e20 jcozerothresh=1e25',
                   'softstophours=24')
    pst = from_text(text)
    c = pst.control
    assert (c['run_slow_fac'], c['run_abandon_fac'], c['uptestlim']) == (5, 3, 100)      # spaces around '=' too
    assert (c['jcowarnthresh'], c['jcozerothresh'], c['softstophours']) == (1e20, 1e25, 24)
    out = to_text(pst)
    assert 'run_slow_fac=5 run_abandon_fac=3 win_mrun_hours=1.5 uptestmin=70 uptestlim=100' in out
    assert 'noaui      jcowarnthresh=1e+20 jcozerothresh=1e+25' in out
    assert 'softstophours=24' in out.splitlines()[10]                                  # the noptmax line
    assert to_text(from_text(out)) == out


def test_unknown_control_tokens_are_kept_on_their_line():
    """A newer PEST_HP variable must survive a round trip, as an unknown `++` option does."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')                                  # no longer a read-time warning
        pst = from_text(hp_text('newhpvar=7 newflag', 'later_var = 2'))
    assert pst.control_line == {'newhpvar': ('control data', 4), 'newflag': ('control data', 4),
                                'later_var': ('control data', 6)}
    out = to_text(pst)
    lines = out.splitlines()
    assert lines[7].endswith('uptestmin=70 newhpvar=7 newflag')
    assert lines[9].endswith('later_var=2')
    assert to_text(from_text(out)) == out
    found = [f for f in validate(pst) if 'is not a control variable' in f.message]
    assert [(f.severity, f.where) for f in found] == [('warning', 'control data line 4')] * 2 + \
        [('warning', 'control data line 6')]
    assert f'"newhpvar=7" is not a control variable of PEST {PESTCHEK_SOURCE_VERSION}' in found[0].message
    assert 'kept as written' in found[0].message


def test_unknown_token_suggests_the_known_name():
    pst = from_text(hp_text('win_mrun_hour=2'))
    msg = next(f.message for f in validate(pst) if 'is not a control variable' in f.message)
    assert 'did you mean "win_mrun_hours"?' in msg


def test_unknown_tokens_survive_dump_and_build(tmp_path):
    for f in os.listdir(DATA):
        shutil.copy(os.path.join(DATA, f), tmp_path)
    (tmp_path / 'hp.pst').write_text(hp_text('newhpvar=7 newflag'))
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        main(['dump', 'hp.pst', 'hp.xlsx'])
        ctl = pd.read_excel('hp.xlsx', 'CONTROL').set_index('NAME')
        assert ctl.loc['newhpvar', 'LINE'] == 'control data line 4' and ctl.loc['newhpvar', 'VALUE'] == 7
        main(['build', 'hp.xlsx'])
        assert len(compare(read_pst('hp.pst'), read_pst('NEW.pst')).control) == 0
        assert 'uptestmin=70 newhpvar=7 newflag' in (tmp_path / 'NEW.pst').read_text()
        main(['parrep', 'hp.pst', 'demo.par', 'set.pst', '--set', 'newhpvar=9'])   # the file gave it a line
        assert 'newhpvar=9 newflag' in (tmp_path / 'set.pst').read_text()
    finally:
        os.chdir(cwd)


def test_control_sheet_needs_a_line_for_an_unknown_variable():
    p = Pst()
    with pytest.warns(UserWarning, match="unknown control variable 'newhpvar' ignored: give the line"):
        p.set_control({'newhpvar': 7})
    assert 'newhpvar' not in p.control
    sheet = pd.DataFrame([('control data', 'noptmax', 5, 3), ('control data, line 4', 'newhpvar', None, 7)],
                         columns=['LINE', 'NAME', 'DEFAULT', 'VALUE'])
    p.set_control(sheet)
    assert p.control['newhpvar'] == 7 and p.control_line['newhpvar'] == ('control data', 4)


def test_values_must_be_readable_by_pest():
    p = read_pst(PST)
    assert not [f for f in check_control(p) if f.severity == 'error']
    p.control.update({'uptestmin': 7.5, 'facparmax': 'abc', 'dpoint': 'pointy', 'precis': 'quad',
                      'noptmax': '1d0', 'phiredstp': '1.0d-2', 'absparmax': 'absparmax(11)=3 absparmax(x)=1'})
    for text in ('UPTESTMIN must be an integer (is "7.5")', 'FACPARMAX must be a number (is "abc")',
                 'DPOINT must be nopoint or point (is "pointy")', 'PRECIS must be double or single (is "quad")',
                 'NOPTMAX must be an integer (is "1d0")', 'ABSPARMAX(n): n must be 1 to 10',
                 'ABSPARMAX must be given as absparmax(n)=value (is "absparmax(x)=1")'):
        assert errors(p, text), text
    assert not errors(p, 'PHIREDSTP')                                   # Fortran reads 1.0d-2


def test_pest_hp_value_rules():
    p = read_pst(PST)
    p.control.update({'run_slow_fac': 1.1, 'run_abandon_fac': 1.1, 'jcowarnthresh': -1, 'hardstophours': 0,
                      'softstophours': 2, 'reg2measrat': 1, 'zerosenval': 2e30, 'orr_not_first': 'orr_not_first'})
    for text in ('RUN_SLOW_FAC must be 1.2 or greater', 'RUN_ABANDON_FAC must be zero or 1.2 or greater',
                 'JCOWARNTHRESH must be zero or greater', 'HARDSTOPHOURS must be greater than zero',
                 'either HARDSTOPHOURS or SOFTSTOPHOURS, but not for both',
                 'REG2MEASRAT must be zero or more and less than one',
                 'ZEROSENVAL must have an absolute value less than 1E30',
                 'ZEROSENVAL must not be supplied unless', '"orr_not_first" needs observation re-referencing'):
        assert errors(p, text), text
    p = read_pst(PST)
    p.control.update({'run_abandon_fac': 0, 'jcowarnthresh': 1e20, 'jcozerothresh': 1e10})
    assert errors(p, 'JCOZEROTHRESH must exceed JCOWARNTHRESH')
    assert not errors(p, 'RUN_ABANDON_FAC')
    p.control.update({'jcozerothresh': 1e25, 'obsreref': 'obsreref_10', 'orr_not_first': 'orr_not_first',
                      'zerosenval': -1.11e29})
    assert not [f for f in check_control(p) if f.severity == 'error']


def test_obsreref_with_a_pause_is_a_flag_not_a_positional_value():
    pst = from_text(read(PST).replace('double     point      1          0          0',
                                             'double     point      1          0          0   obsreref_10 orr_not_first'))
    assert pst.control['obsreref'] == 'obsreref_10' and pst.control['orr_not_first'] == 'orr_not_first'
    assert pst.control_line == {}
    assert 'obsreref_10 orr_not_first' in to_text(pst)


def test_every_control_variable_has_a_type():
    flags = {k for sec in ALL_SECTIONS for k in sec.flags}
    untyped = set(FIELD_SECTION) - INTEGER - REAL - TEXT - flags
    assert untyped <= {'npar', 'nobs', 'npargp', 'nprior', 'nobsgp', 'ntplfle', 'ninsfle'}   # computed


def test_rule_sources_are_reported(tmp_path, capsys):
    shutil.copy(PST, tmp_path / 'demo.pst')
    with pytest.raises(SystemExit):                                     # the fixture's model files are elsewhere
        main(['validate', str(tmp_path / 'demo.pst')])
    assert (f"rules from PEST {PESTCHEK_SOURCE_VERSION}'s pestchek and PEST++ {PESTPP_REGISTRY_VERSION}'s options"
            in capsys.readouterr().out)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        main(['dump', 'demo.pst', 'demo_out.xlsx'])
    finally:
        os.chdir(cwd)
    m = json.loads(read(tmp_path / 'demo_out.xlsx.manifest.json'))
    assert m['checked_against'] == {'pestchek': PESTCHEK_SOURCE_VERSION, 'pestpp': PESTPP_REGISTRY_VERSION}
