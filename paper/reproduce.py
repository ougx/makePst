"""Regenerate the numbers in the manuscript's demonstration section and Table 1.

    python paper/reproduce.py <workbook.xlsx> [work_dir]

The workbook is the calibration workbook (sheets CONTROL, PARGP, PAR_*, OBS_*, IO, PPcntl,
PPglm). All outputs go to work_dir (default: a temporary folder). Nothing in the project
folder is touched.
"""
import contextlib
import io
import os
import platform
import shutil
import sys
import tempfile
import time
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.simplefilter('ignore')

from makepst import __version__, read_pst, to_workbook, update_workbook  # noqa: E402
from makepst.cli import main  # noqa: E402
from makepst.diff import compare  # noqa: E402

PAR_SHEETS = ['PAR_HK', 'PAR_VK', 'PAR_SS', 'PAR_SY', 'PAR_SFR', 'PAR_Other', 'PAR_GHBdh', 'PAR_RCH']


def timed(fn):
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        result = fn()
    return result, time.perf_counter() - t0


def run(book, work):
    book = os.path.abspath(book)
    pst = os.path.join(work, 'demo.pst')
    args = [pst, 'regul', '--set_ctl_xls', f'{book},CONTROL', '--add_pargp_xls', f'{book},PARGP']
    for s in PAR_SHEETS:
        args += ['--add_par_xls', f'{book},{s}']
    args += ['--add_obs_xls', f'{book},OBS_*', '--add_io_xls', f'{book},IO',
             '--add_pp_xls', f'{book},PPcntl', '--add_pp_xls', f'{book},PPglm', '--no_dump_tpl']

    _, t_build = timed(lambda: main(args))
    p, t_read = timed(lambda: read_pst(pst))
    dump = os.path.join(work, 'demo_dump.xlsx')
    _, t_dump = timed(lambda: to_workbook(p, dump))
    rebuilt = os.path.join(work, 'demo_rebuilt.pst')
    _, t_rebuild = timed(lambda: main(['build', dump, '--out', rebuilt, '--no_dump_tpl', '--no_manifest']))
    q = read_pst(rebuilt)
    d, t_diff = timed(lambda: compare(p, q))
    identical = open(pst).read() == open(rebuilt).read()

    # results back: a .par (values * 1.01) and a .res (residual -0.5) derived from the control file
    par_file, res_file = os.path.join(work, 'demo.par'), os.path.join(work, 'demo.res')
    with open(par_file, 'w') as f:
        f.write('single point\n')
        for r in p.par.itertuples():
            f.write(f'{r.PARNME} {float(r.PARVAL1) * 1.01:.11g} 1.0 0.0\n')
    with open(res_file, 'w') as f:
        f.write('Name Group Measured Modelled Residual Weight\n')
        for r in p.obs.itertuples():
            f.write(f'{r.OBSNME} {r.OBGNME} {float(r.OBSVAL)} {float(r.OBSVAL) + 0.5} -0.5 {float(r.WEIGHT)}\n')
    copy = os.path.join(work, 'demo_copy' + os.path.splitext(book)[1])
    shutil.copy(book, copy)
    upd, t_update = timed(lambda: update_workbook(copy, par=par_file, res=res_file, backend='openpyxl'))

    print(f'makepst {__version__}; {platform.platform()}; Python {platform.python_version()}')
    print(f'counts: {p.counts}')
    print(f'control file: {os.path.getsize(pst) / 1e6:.2f} MB, {sum(1 for _ in open(pst))} lines')
    print(f'build {t_build:.1f} s (read {t_read:.2f} s) | dump {t_dump:.1f} s | rebuild {t_rebuild:.1f} s | '
          f'diff {t_diff:.2f} s | update (openpyxl) {t_update:.1f} s')
    print(f'round trip: {d.summary()}; byte-identical: {identical}')
    print('update rows: ' + ', '.join(f'{k} {v["rows"]}' for k, v in upd['sheets'].items()))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    work = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix='makepst-paper-')
    os.makedirs(work, exist_ok=True)
    run(sys.argv[1], work)
    print(f'work folder: {work}')
