"""tools/rule_sources.py: the extractors, and the rules held to the reviewed upstream snapshots (offline)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import rule_sources as rs  # noqa: E402
from makepst.sections import PESTCHEK_SOURCE_VERSION  # noqa: E402
from makepst.pestpp import PESTPP_REGISTRY_VERSION  # noqa: E402


def snapshot(name):
    with open(os.path.join(ROOT, 'tools', 'snapshots', f'{name}.json'), encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------- the code against the snapshots
def test_pestpp_registry_matches_the_reviewed_source():
    """Editing makepst/pestpp.py by hand, or recording a newer source without carrying it in, fails here."""
    snap = snapshot('pestpp')
    assert snap['version'] == PESTPP_REGISTRY_VERSION
    assert rs.compare_pestpp(snap, rs.registry_view(), 'snapshot', 'pestpp.py', hand_choices=True) == []


def test_pestchek_keywords_are_all_in_the_schema():
    snap = snapshot('pestchek')
    assert snap['version'] == PESTCHEK_SOURCE_VERSION
    known, ignored = rs.schema_view()
    assert set(snap['keywords']) - known - ignored == set()
    assert ignored <= set(snap['keywords'])            # an exclusion pestchek no longer reads is stale


# ---------------------------------------------------------------------- PEST++ extractor
CPP = '''
PestppOptions::ARG_STATUS PestppOptions::assign_value_by_key(string key, const string org_value)
{
    if (key=="MAX_N_SUPER"){
        convert_ip(value, max_n_super);
    }
    else if ((key == "IES_PAR_EN") || (key == "IES_PARAMETER_ENSEMBLE"))
    {
        ies_par_csv = org_value;
    }
    else if (key == "SVD_PACK")
    {
        if (value == "REDSVD") svd_pack = REDSVD;
        else if ((value == "EIGEN") || (value == "JACOBI")) svd_pack = EIGEN;
    }
    else if (key == "OPT_DIRECTION")
    {
        string v;
        convert_ip(value, v);
        if (v == "MAX") opt_direction = -1;
        else if (v == "MIN") opt_direction = 1;
    }
    else if (key == "DER_FORGIVE")
    {
        der_forgive = pest_utils::parse_string_arg_to_bool(value);
    }
    else if (key == "LAMBDAS")
    {
        tokenize(value, lambda_tok, ",");
        for (const auto &l : lambda_tok) base_lambda_vec.push_back(convert_cp<double>(l));
    }
    else if (key == "MOU_PSO_INERTIA")
    {
        vector<string> tok;
        tokenize(value, tok, ",");
        double v;
        for (const auto& t : tok) { convert_ip(t, v); mou_pso_inertia.push_back(v); }
    }
    else if (key == "MAT_INV")
    {
        cout << "++MAT_INV is deprecated and no longer supported...ignoring" << endl;
    }
}
PestppOptions::ARG_STATUS ControlInfo::assign_value_by_key(const string key, const string org_value)
{
    if (key == "NOPTMAX") convert_ip(value, noptmax);
}
'''
HEADER = '''
    int max_n_super;
    string ies_par_csv;
'''


def test_pestpp_extractor():
    got = rs.extract_pestpp({'cpp': CPP, 'h': HEADER, 'version': '#define PESTPP_VERSION "9.9.9";'})
    assert got['version'] == '9.9.9'
    assert got['options'] == {'max_n_super': 'int', 'ies_par_en': 'str', 'svd_pack': 'str', 'opt_direction': 'str',
                              'der_forgive': 'bool', 'lambdas': 'list[float]', 'mou_pso_inertia': 'list[float]',
                              'mat_inv': 'str'}                      # noptmax belongs to ControlInfo
    assert got['aliases'] == {'ies_parameter_ensemble': 'ies_par_en'}
    assert got['choices'] == {'opt_direction': ['max', 'min'], 'svd_pack': ['eigen', 'jacobi', 'redsvd']}
    assert got['deprecated'] == ['mat_inv']


def test_pestpp_comparison_names_each_change():
    old = rs.extract_pestpp({'cpp': CPP, 'h': HEADER, 'version': '#define PESTPP_VERSION "9.9.9";'})
    cpp = CPP.replace('"MAX_N_SUPER"', '"MAX_N_SUPER_ITER"').replace('|| (key == "IES_PARAMETER_ENSEMBLE")', '')
    new = rs.extract_pestpp({'cpp': cpp, 'h': HEADER + 'double max_n_super_iter;\n',
                             'version': '#define PESTPP_VERSION "9.9.10";'})
    assert rs.compare_pestpp(old, new, 'snapshot', 'source') == [
        'version: snapshot 9.9.9 -> source 9.9.10',
        'option added in source: max_n_super_iter (int)',
        'option not in source: max_n_super',
        'alias ies_parameter_ensemble: ies_par_en in snapshot, None in source']


# ---------------------------------------------------------------------- pestchek extractor
FORTRAN = """\
C     a comment with INDEX(CLINE,'not_a_keyword')
        NN = INDEX(CLINE,'run_slow_fac')
        VARTEXT='jcowarnthresh'
        II=INDEX(CLINE,'orr_not_first')
2106        FORMAT(A,A,': RUN_SLOW_FAC must be greater ',
     +      'than 1.2.')
10      FORMAT(1X,A)
"""


def test_pestchek_extractor(tmp_path):
    (tmp_path / 'pestchek.F').write_text(FORTRAN)
    (tmp_path / 'cheksub.F').write_text("        AVAR='HARDSTOPHOURS'\n")
    (tmp_path / 'version.inc').write_text("       aversion='99.1'\n")
    got = rs.extract_pestchek(str(tmp_path))
    assert got['version'] == '99.1'
    assert got['keywords'] == ['hardstophours', 'jcowarnthresh', 'orr_not_first', 'run_slow_fac']
    assert got['messages'] == ['...: RUN_SLOW_FAC must be greater than 1.2.']    # layout-only formats skipped


def test_pestchek_run_reports_a_new_release(tmp_path, capsys, monkeypatch):
    for name, text in (('pestchek.F', FORTRAN), ('cheksub.F', ''), ('version.inc', "aversion='99.1'\n")):
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(rs, 'SNAPSHOTS', str(tmp_path / 'snap'))
    assert rs.main(['pestchek', '--source', str(tmp_path), '--write']) == 1       # 99.1 is not the pinned version
    (tmp_path / 'pestchek.F').write_text(FORTRAN + "        NN = INDEX(CLINE,'brand_new_var')\n")
    (tmp_path / 'version.inc').write_text("aversion='99.2'\n")
    capsys.readouterr()
    assert rs.main(['pestchek', '--source', str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert 'version: snapshot 99.1 -> source 99.2' in out and 'keyword added: brand_new_var' in out
    assert 'keyword pestchek reads is not in the schema: brand_new_var' in out


def test_unreadable_source_is_not_drift(tmp_path, capsys):
    assert rs.main(['pestchek', '--source', str(tmp_path / 'missing')]) == 2
