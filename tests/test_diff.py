"""makepst diff: semantic comparison of control files / workbooks."""
import os
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from makepst import read_pst, write_pst  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.diff import compare  # noqa: E402
from make_fixture import BOOK, PST  # noqa: E402


def changed():
    p = read_pst(PST)
    p.par.loc[p.par['PARNME'] == 'hk1_cc01', 'PARUBND'] = 500          # bound moved
    p.par.loc[p.par['PARNME'] == 'rchss', 'PARTRANS'] = 'fixed'         # transform changed
    p.par = p.par[p.par['PARNME'] != 'dg00025']                         # parameter removed
    p.obs.loc[p.obs['OBSNME'] == 'qbs_2010', 'WEIGHT'] = 0.5            # weight changed
    p.obs = pd.concat([p.obs, pd.DataFrame([['new_obs', 1.0, 1.0, 'head']], columns=p.obs.columns)])
    p.control['noptmax'] = 0
    p.control.pop('uptestmin')
    p.pestpp.append(('lambdas', '1,10'))
    p.comments.append('tr14 based tr13')
    p.ins.append(('extra.ins', 'extra.out'))
    return p


def test_identical_is_empty():
    a, b = read_pst(PST), read_pst(PST)
    d = compare(a, b)
    assert d.empty and d.summary() == 'no differences' and d.to_text() == 'no differences'


def test_changes_are_reported():
    old, new = read_pst(PST), changed()
    new.normalize()
    d = compare(old, new)
    par = d.par.set_index(['PARNME', 'column'])
    assert par.loc[('hk1_cc01', 'PARUBND'), 'old'] == '300' and par.loc[('hk1_cc01', 'PARUBND'), 'new'] == '500'
    assert par.loc[('rchss', 'PARTRANS'), 'new'] == 'fixed'
    assert d.par[d.par['change'] == 'removed']['PARNME'].tolist() == ['dg00025']
    assert d.obs[d.obs['change'] == 'added']['OBSNME'].tolist() == ['new_obs']
    assert d.obs.set_index(['OBSNME', 'column']).loc[('qbs_2010', 'WEIGHT'), 'new'] == '0.5'
    ctl = d.control.set_index('NAME')
    assert ctl.loc['noptmax', 'change'] == 'changed' and ctl.loc['noptmax', 'new'] == '0'
    assert ctl.loc['uptestmin', 'change'] == 'removed'
    assert d.pestpp.iloc[0].tolist() == ['lambdas', 'changed', '0.1,1,10,100', '1,10']
    assert d.comments.iloc[0].tolist() == ['added', 'tr14 based tr13']
    assert d.io.iloc[0].tolist() == ['ins', 'added', 'extra.ins', 'extra.out']
    # rchss became fixed, so normalize() dropped its regularisation equation
    assert d.prior[d.prior['change'] == 'removed']['PINME'].tolist() == ['rchss']
    assert 'par: 1 removed' in d.summary() or 'par:' in d.summary()
    text = d.to_text(max_rows=2)
    assert '== par (' in text and '... ' in text


def test_tolerance():
    old, new = read_pst(PST), read_pst(PST)
    new.par['PARVAL1'] = new.par['PARVAL1'].astype(float) * (1 + 1e-7)
    assert len(compare(old, new).par) == 11
    assert compare(old, new, rtol=1e-6).empty


def test_tied_parameter_ratio_change_is_reported():
    old, new = read_pst(PST), read_pst(PST)
    child = new.par['PARNME'] == 'hk1_cc02'
    new.par.loc[child, 'PARVAL1'] *= 1.1

    ratio = compare(old, new).par
    ratio = ratio[ratio['column'] == 'tied factor']
    assert ratio[['PARNME', 'change']].values.tolist() == [['hk1_cc02', 'changed']]


def test_tied_parameter_ratio_is_unchanged_when_parent_and_child_scale_together():
    old, new = read_pst(PST), read_pst(PST)
    both = new.par['PARNME'].isin(['hk1_cc01', 'hk1_cc02'])
    new.par.loc[both, 'PARVAL1'] *= 2

    d = compare(old, new)
    assert not (d.par['column'] == 'tied factor').any()


def test_cli_and_workbook(tmp_path, capsys):
    new = tmp_path / 'new.pst'
    write_pst(changed().normalize(), str(new), dump_tpl=False)
    with pytest.raises(SystemExit):                          # differences -> exit 1
        main(['diff', PST, str(new), '--xlsx', str(tmp_path / 'd.xlsx'), '--max_rows', '1'])
    out = capsys.readouterr().out
    assert '== par (' in out and 'noptmax' in out and '... ' in out
    sheets = pd.ExcelFile(tmp_path / 'd.xlsx').sheet_names
    assert 'SUMMARY' in sheets and 'PAR' in sheets and 'PESTPP' in sheets
    main(['diff', BOOK, PST])                                # workbook == the golden built from it
    assert 'no differences' in capsys.readouterr().out
