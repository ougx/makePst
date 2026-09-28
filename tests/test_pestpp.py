"""`++` options against the PEST++ registry: unknown -> warning (kept), bad value or duplicate -> error."""
import os
import shutil
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from makepst import Pst, from_text, read_pst  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.excel import load_table  # noqa: E402
from makepst.pestpp import ALIASES, PESTPP_OPTIONS, canonical, check_pestpp, value_problem  # noqa: E402

MINIMAL = os.path.join(ROOT, 'examples', 'minimal')
BUILD = ['build', 'model.pst', 'regul', '--set_ctl_csv', 'control.csv', '--add_pargp_csv', 'pargp.csv',
         '--add_par_csv', 'par.csv', '--add_obs_csv', 'obs.csv', '--add_io_csv', 'io.csv',
         '--no_manifest', '--no_dump_tpl']


def found(pst):
    return [(f.severity, f.where, f.message) for f in check_pestpp(pst)]


def with_pp(*pairs):
    p = Pst()
    p.add_pp(pd.DataFrame(pairs, columns=['PP_VAR', 'VAL']))
    return p


@pytest.fixture
def minimal(tmp_path, monkeypatch):
    shutil.copytree(MINIMAL, tmp_path / 'm')
    monkeypatch.chdir(tmp_path / 'm')
    return tmp_path / 'm'


# ---------------------------------------------------------------------- the registry
def test_registry_is_consistent():
    assert not set(ALIASES) & set(PESTPP_OPTIONS)                 # an alias is never also a primary name
    assert set(ALIASES.values()) <= set(PESTPP_OPTIONS)
    assert len(PESTPP_OPTIONS) + len(ALIASES) == 284              # every key in PEST++ 5.2.29's ++ parser
    assert PESTPP_OPTIONS['ies_num_reals']['type'] is int and PESTPP_OPTIONS['ies_num_reals']['min'] == 1
    assert PESTPP_OPTIONS['ies_lambda_mults']['type'] == list[float]
    assert 'pestpp-ies' in PESTPP_OPTIONS['ies_num_reals']['programs']


def test_names_are_case_insensitive_and_follow_aliases():
    assert canonical('IES_NUM_REALS') == 'ies_num_reals'
    assert canonical('ies_parameter_ensemble') == 'ies_par_en'
    assert canonical('da_num_reals') == 'ies_num_reals'           # pestpp-da takes every ies_ option as da_
    assert canonical('da_parameter_ensemble') == 'ies_par_en'
    assert canonical('da_parameter_cycle_table') == 'da_parameter_cycle_table'
    assert canonical('ies_num_real') is None


@pytest.mark.parametrize('name, value, problem', [
    ('ies_num_reals', '500', None),
    ('ies_num_reals', '50.5', 'must be an integer (is "50.5")'),
    ('ies_num_reals', '0', 'must be 1 or greater (is "0")'),
    ('ies_init_lam', '-10', None),                                # negative is meaningful to pestpp-ies
    ('overdue_giveup_fac', '1.5e0', None),
    ('overdue_giveup_fac', '1.5d0', 'must be a number (is "1.5d0")'),   # PEST++ parses with C++ streams
    ('ies_enforce_bounds', 'TRUE', None),
    ('ies_enforce_bounds', '0', None),
    ('ies_enforce_bounds', 't', 'must be true or false (is "t")'),   # PEST++ would silently read false
    ('ies_enforce_bounds', 'yes', 'must be true or false (is "yes")'),
    ('ies_lambda_mults', '0.1,1,10', None),
    ('ies_lambda_mults', '0.1,x', 'must be a comma-separated list of numbers (is "0.1,x")'),
    ('ies_n_iter_mean', '2,1.5', 'must be a comma-separated list of integers (is "2,1.5")'),
    ('svd_pack', 'REDSVD', None),
    ('svd_pack', 'lapack', 'must be one of redsvd/eigen/jacobi/propack (is "lapack")'),
    ('ies_par_en', 'prior.csv', None),
    ('ies_par_en', '', 'has no value'),
])
def test_value_problems(name, value, problem):
    assert value_problem(name, value) == problem


# ---------------------------------------------------------------------- findings
def test_unknown_option_is_a_warning_with_a_suggestion_and_is_kept(minimal):
    pd.DataFrame({'PP_VAR': ['ies_num_reals', 'ies_num_real'], 'VAL': [500, 500]}).to_excel('book.xlsx', 'PP',
                                                                                           index=False)
    main(BUILD + ['--add_pp_xls', 'book.xlsx,PP'])
    p = read_pst('model.pst')
    assert p.pestpp == [('ies_num_reals', '500'), ('ies_num_real', '500')]      # preserved, not rejected
    built = Pst()
    built.add_pp(load_table('book.xlsx,PP'))
    [(sev, where, msg)] = found(built)
    assert (sev, where) == ('warning', 'PP!A3')
    assert msg.startswith('unknown PEST++ option "ies_num_real"; did you mean "ies_num_reals"?')


def test_invalid_value_is_an_error_at_its_cell():
    assert found(with_pp(('ies_num_reals', 12.5))) == [
        ('error', 'pestpp options', '++ies_num_reals must be an integer (is "12.5")')]


def test_duplicate_through_an_alias_is_an_error(tmp_path):
    pd.DataFrame({'PP_VAR': ['ies_par_en', 'max_run_fail', 'IES_PARAMETER_ENSEMBLE'],
                  'VAL': ['a.csv', 3, 'b.csv']}).to_csv(tmp_path / 'pp.csv', index=False)
    p = Pst()
    p.add_pp(load_table(str(tmp_path / 'pp.csv')))
    assert found(p) == [('error', 'pp.csv:4', '++IES_PARAMETER_ENSEMBLE is already set as "ies_par_en" at pp.csv:2: '
                                              'PEST++ stops on a duplicate option, even through an alias')]


def test_control_variable_given_as_a_pestpp_option():
    [(sev, _, msg)] = found(with_pp(('noptmax', 3)))
    assert sev == 'warning' and msg.startswith('"noptmax" is a control variable, not a PEST++ option')


def test_forgive_unknown_args_turns_unknown_options_into_notes():
    [(sev, _, msg)] = found(with_pp(('forgive_unknown_args', 'true'), ('ies_future_option', 1)))
    assert sev == 'info' and 'forgive_unknown_args(true) lets PEST++ carry on' in msg


def test_deprecated_option_is_a_note():
    assert found(with_pp(('mat_inv', 'jtqj'))) == [('info', 'pestpp options', '++mat_inv is deprecated; PEST++ ignores it')]


def test_control_file_locations_are_line_numbers():
    text = open(os.path.join(ROOT, 'tests', 'data', 'demo.pst')).read().rstrip('\n')
    n = text.count('\n') + 1
    p = from_text(text + '\n++ies_num_reals(50)\n++ies_num_realz(50)\n')
    assert ('warning', f'line {n + 2}') in [(s, w) for s, w, _ in found(p)]


# ---------------------------------------------------------------------- commands
def test_build_reports_but_still_writes_and_validate_fails(minimal, capsys):
    pd.DataFrame({'PP_VAR': ['ies_num_reals'], 'VAL': ['lots']}).to_csv('pp.csv', index=False)
    main(BUILD + ['--add_pp_csv', 'pp.csv'])
    out = capsys.readouterr().out
    assert 'ERROR   pp.csv:2: ++ies_num_reals must be an integer (is "lots")' in out
    assert read_pst('model.pst').pestpp == [('ies_num_reals', 'lots')]
    with pytest.raises(SystemExit):
        main(['validate', 'model.pst'])
    assert 'ERROR   line ' in capsys.readouterr().out
