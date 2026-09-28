import os

import numpy as np
import pandas as pd
import pytest

from makepst import Pst, balance_weights, discrepancy_weights, equal_shares, read_pst, read_res, scale_weights
from makepst.cli import main


HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
PST = os.path.join(DATA, 'demo.pst')
RES = os.path.join(DATA, 'demo.res')


def synthetic():
    pst = Pst()
    pst.obs = pd.DataFrame({
        'OBSNME': ['a1', 'a2', 'b1', 'zero'],
        'OBSVAL': [1.0, 1.0, 1.0, 1.0],
        'WEIGHT': [1.0, 2.0, 1.0, 0.0],
        'OBGNME': ['a', 'a', 'b', 'b'],
    })
    residuals = pd.DataFrame({'RESIDUAL': [1.0, 1.0, 1.0, 1.0]},
                             index=pd.Index(['a1', 'a2', 'b1', 'zero'], name='NAME'))
    return pst, residuals


def test_equal_balancing_preserves_total_and_zero_weights():
    pst, residuals = synthetic()
    report = balance_weights(pst, residuals, {'a': 1, 'b': 1})
    out = report.set_index('OBGNME')
    assert out.loc['a', 'NEW_PHI'] == pytest.approx(out.loc['b', 'NEW_PHI'])
    assert out['NEW_PHI'].sum() == pytest.approx(6.0)
    weights = pst.obs.set_index('OBSNME')['WEIGHT']
    assert weights.loc['a2'] / weights.loc['a1'] == pytest.approx(2.0)
    assert weights.loc['zero'] == 0


def test_target_shares_are_normalized():
    pst, residuals = synthetic()
    report = balance_weights(pst, residuals, {'a': 3, 'b': 1})
    out = report.set_index('OBGNME')
    assert out.loc['a', 'NEW_PCT'] == pytest.approx(75.0)
    assert out.loc['b', 'NEW_PCT'] == pytest.approx(25.0)
    assert out.loc['a', 'TARGET_PCT'] == pytest.approx(75.0)


def test_target_zero_disables_group_even_with_clip_floor():
    pst, residuals = synthetic()
    balance_weights(pst, residuals, {'a': 1, 'b': 0}, clip=(0.1, 10.0))
    weights = pst.obs.set_index('OBSNME')['WEIGHT']
    assert weights.loc['b1'] == 0


def test_clipped_balancing_reaches_an_achievable_target():
    pst = Pst()
    pst.obs = pd.DataFrame({
        'OBSNME': ['a1', 'a2', 'b1'],
        'OBSVAL': [1.0, 1.0, 1.0],
        'WEIGHT': [1.0, 2.0, 1.0],
        'OBGNME': ['a', 'a', 'b'],
    })
    residuals = pd.DataFrame({'RESIDUAL': [1.0, 1.0, 1.0]}, index=['a1', 'a2', 'b1'])
    report = balance_weights(pst, residuals, {'a': 7, 'b': 5}, clip=(0.0, 1.6))
    out = report.set_index('OBGNME')
    assert out.loc['a', 'NEW_PHI'] == pytest.approx(3.5, rel=1e-8)
    assert out.loc['b', 'NEW_PHI'] == pytest.approx(2.5, rel=1e-8)
    assert out.loc['a', 'N_CLIPPED_HIGH'] == 1
    assert pst.obs['WEIGHT'].max() <= 1.6


def test_clipped_target_can_fail():
    pst, residuals = synthetic()
    with pytest.raises(ValueError, match='not achievable'):
        balance_weights(pst, residuals, {'a': 100, 'b': 1}, clip=(0.0, 0.1))


def test_direct_factor_without_residuals_and_clip():
    pst, _ = synthetic()
    report = scale_weights(pst, {'a': 2}, clip=(0.0, 1.5))
    weights = pst.obs.set_index('OBSNME')['WEIGHT']
    assert weights.loc['a1'] == pytest.approx(1.5)
    assert weights.loc['a2'] == pytest.approx(1.5)
    assert weights.loc['zero'] == 0
    assert report.loc[0, 'N_CLIPPED_HIGH'] == 2
    assert np.isnan(report.loc[0, 'OLD_PHI'])


def test_covariance_and_special_groups_rejected():
    pst, residuals = synthetic()
    pst.obs_cov = {'a': 'a.cov'}
    with pytest.raises(ValueError, match='covariance-backed'):
        balance_weights(pst, residuals, {'a': 1})


def test_cli_target_table_and_report(tmp_path):
    targets = tmp_path / 'targets.csv'
    targets.write_text('OBGNME,TARGET_SHARE\nhead,2\nqbs,1\nlhss,1\n', encoding='utf-8')
    out = tmp_path / 'balanced.pst'
    report = tmp_path / 'report.csv'
    main(['reweight', PST, str(out), '--res', RES, '--targets', str(targets),
          '--report', str(report), '--no_manifest'])
    assert out.exists() and report.exists()
    assert read_pst(str(out)).obs['WEIGHT'].gt(0).sum() == 4
    table = pd.read_csv(report).set_index('OBGNME')
    assert table.loc['head', 'TARGET_PCT'] == pytest.approx(50.0)


def test_cli_equal_requires_result_and_dry_run_writes_nothing(tmp_path):
    with pytest.raises(SystemExit, match='requires --res or --obs_csv'):
        main(['reweight', PST, '--equal', '--dry_run', '--no_manifest'])
    out = tmp_path / 'not-written.pst'
    main(['reweight', PST, str(out), '--res', RES, '--equal', '--dry_run'])
    assert not out.exists()
    assert not (tmp_path / 'not-written.pst.manifest.json').exists()


def test_direct_factor_with_residuals_reports_phi():
    pst, residuals = synthetic()
    report = scale_weights(pst, {'a': 2}, residuals=residuals).set_index('OBGNME')
    assert report.loc['a', 'OLD_PHI'] == pytest.approx(5.0)
    assert report.loc['a', 'NEW_PHI'] == pytest.approx(20.0)
    assert pst.obs.set_index('OBSNME')['WEIGHT'].loc['a2'] == pytest.approx(4.0)


def test_cli_factor_with_residuals(tmp_path):
    out = tmp_path / 'scaled.pst'
    main(['reweight', PST, str(out), '--res', RES, '--factor', 'head=2', '--no_manifest'])
    assert out.exists()


def test_integer_weights_become_float():
    pst, residuals = synthetic()
    pst.obs['WEIGHT'] = [1, 2, 1, 0]
    balance_weights(pst, residuals, {'a': 1, 'b': 1})
    assert pst.obs['WEIGHT'].dtype == float


def test_equal_shares_skip_zero_phi_and_special_groups():
    pst, residuals = synthetic()
    pst.obs.loc[len(pst.obs)] = ['c1', 1.0, 1.0, 'c']
    pst.obs.loc[len(pst.obs)] = ['r1', 1.0, 1.0, 'regul_x']
    residuals.loc['c1', 'RESIDUAL'] = 0.0
    shares, skipped = equal_shares(pst, residuals)
    assert shares == {'a': 1.0, 'b': 1.0}
    assert 'zero current phi' in skipped['c']
    assert 'not a measurement group' in skipped['regul_x']


def test_residuals_only_needed_for_selected_groups():
    pst, residuals = synthetic()
    residuals = residuals.drop('a1')
    report = balance_weights(pst, residuals.drop('a2'), {'b': 1})
    assert report.loc[0, 'NEW_PHI'] == pytest.approx(1.0)
    with pytest.raises(ValueError, match='a1'):
        balance_weights(pst, residuals, {'a': 1, 'b': 1})


def test_nonfinite_residual_is_named():
    pst, residuals = synthetic()
    residuals.loc['b1', 'RESIDUAL'] = np.inf
    with pytest.raises(ValueError, match='b1'):
        balance_weights(pst, residuals, {'a': 1, 'b': 1})


def test_group_names_are_stripped_in_both_modes():
    pst, residuals = synthetic()
    pst.obs['OBGNME'] = ['a ', 'a ', ' B', ' B']
    balance_weights(pst, residuals, {'a': 1, 'b': 1})
    scale_weights(pst, {'a': 2})


def test_discrepancy_group_sets_phi_to_weighted_count():
    pst, residuals = synthetic()
    report, skipped = discrepancy_weights(pst, residuals, by='group')
    out = report.set_index('OBGNME')
    assert out.loc['a', 'NEW_PHI'] == pytest.approx(2.0)
    assert out.loc['b', 'NEW_PHI'] == pytest.approx(1.0)
    assert out.loc['a', 'TARGET_PCT'] == pytest.approx(200.0 / 3)
    weights = pst.obs.set_index('OBSNME')['WEIGHT']
    assert weights.loc['a2'] / weights.loc['a1'] == pytest.approx(2.0)
    assert weights.loc['zero'] == 0
    assert skipped == {}


def test_discrepancy_group_skips_zero_phi_group():
    pst, residuals = synthetic()
    residuals.loc['b1', 'RESIDUAL'] = 0.0
    report, skipped = discrepancy_weights(pst, residuals, by='group')
    assert report['OBGNME'].tolist() == ['a']
    assert 'zero current phi' in skipped['b']
    assert pst.obs.set_index('OBSNME')['WEIGHT'].loc['b1'] == 1.0


def test_discrepancy_obs_caps_contribution_and_never_raises_weights():
    pst, residuals = synthetic()
    residuals['RESIDUAL'] = [0.5, 1.0, 0.0, 1.0]
    report, _ = discrepancy_weights(pst, residuals, by='obs')
    weights = pst.obs.set_index('OBSNME')['WEIGHT']
    assert weights.loc['a1'] == 1.0          # 1/|r| = 2 is above the original weight
    assert weights.loc['a2'] == pytest.approx(1.0)
    assert weights.loc['b1'] == 1.0          # zero residual keeps its weight
    assert weights.loc['zero'] == 0
    out = report.set_index('OBGNME')
    assert out.loc['a', 'N_REDUCED'] == 1
    assert out.loc['b', 'N_REDUCED'] == 0
    assert np.isnan(out.loc['a', 'BASE_FACTOR'])


def test_discrepancy_obs_with_clip_and_bad_mode():
    pst, residuals = synthetic()
    report, _ = discrepancy_weights(pst, residuals, by='obs', clip=(0.0, 0.5))
    assert pst.obs['WEIGHT'].max() <= 0.5
    assert report.set_index('OBGNME').loc['a', 'N_CLIPPED_HIGH'] == 2
    with pytest.raises(ValueError, match="'group' or 'obs'"):
        discrepancy_weights(pst, residuals, by='both')


def test_cli_discrepancy_modes(tmp_path):
    res = read_res(RES)
    out = tmp_path / 'group.pst'
    report = tmp_path / 'group.csv'
    main(['reweight', PST, str(out), '--res', RES, '--discrepancy', 'group',
          '--report', str(report), '--no_manifest'])
    table = pd.read_csv(report)
    assert np.allclose(table['NEW_PHI'], table['N_WEIGHTED_NEW'])

    out = tmp_path / 'obs.pst'
    main(['reweight', PST, str(out), '--res', RES, '--discrepancy', 'obs', '--no_manifest'])
    new = read_pst(str(out)).obs.set_index('OBSNME')['WEIGHT']
    old = read_pst(PST).obs.set_index('OBSNME')['WEIGHT']
    assert (new <= old).all()
    weighted = new[new > 0]
    contribution = (weighted * res.loc[weighted.index, 'RESIDUAL']) ** 2
    assert (contribution <= 1.0 + 1e-6).all()

    with pytest.raises(SystemExit, match='requires --res or --obs_csv'):
        main(['reweight', PST, '--discrepancy', 'obs', '--dry_run'])
