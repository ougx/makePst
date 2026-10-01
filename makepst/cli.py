"""Command line: build (Excel/CSV -> pst), dump (pst -> Excel), update (values -> existing workbook),
parrep (.par values -> pst), set / rescale (parameter edits -> pst), init (starter workbook),
validate (pestchek-style checks), diff (compare two),
reweight (observation weights -> pst), tempchek (templates -> model input files), inschek (model output files -> observation values),
provenance / log / bundle (manifests: verify, list as a ledger, zip a run)."""
import argparse
import os
import shlex
import sys

import pandas as pd

from . import __version__
from .excel import expand_spec, load_table, to_workbook, update_workbook
from .pestpp import check_pestpp
from .provenance import Manifest, check_manifest
from .pst import Pst, _to_internal, read_obs_ensemble, read_par, read_res
from .reader import read_pst
from .reweight import balance_weights, discrepancy_weights, equal_shares, scale_weights
from .writer import write_pst

TABLES = ('pargp', 'par', 'tied', 'prior', 'obs', 'obsgp', 'io', 'pp', 'comment')
WORKBOOK_EXT = ('.xlsx', '.xlsm', '.xls')


def _spec_parts(spec):
    """'book.xlsx,SHEET' -> (book.xlsx, SHEET); 'file.csv' -> (file.csv, None)."""
    if ',' in spec:
        path, sheet = spec.rsplit(',', 1)
        return path, sheet
    return spec, None


def _build_parser(p):
    p.add_argument('pstfile', nargs='?',
                   help='control file to write (default: source table path with .pst extension), '
                        'or a workbook whose BUILD sheet holds the command')
    p.add_argument('mode', nargs='?', default=None,
                   help='estimation | regularisation | prediction | pareto (default: CONTROL sheet, else estimation)')
    p.add_argument('--set_ctl_xls', metavar='BOOK,SHEET', help='CONTROL sheet: NAME / VALUE columns')
    p.add_argument('--set_ctl_csv', metavar='CSV')
    for t in TABLES:
        p.add_argument(f'--add_{t}_xls', action='append', default=[], metavar='BOOK,SHEET',
                       help='sheet name may be a glob, e.g. book.xlsm,PAR_*' if t == 'par' else argparse.SUPPRESS)
        p.add_argument(f'--add_{t}_csv', action='append', default=[], metavar='CSV')
    p.add_argument('--add_comment', action='append', default=[], metavar='TEXT')
    p.add_argument('--fill_parval', metavar='FILE',
                   help='take PARVAL1 from a PEST .par file or a PESTPP-IES case.N.par.csv')
    p.add_argument('--real', metavar='NAME', help='realization for --fill_parval from an ensemble (default base)')
    p.add_argument('--ss', action='store_true', help='steady state: drop ss*/sy* parameters and groups')
    p.add_argument('--no_dump_tpl', action='store_true', help="don't write dump.tpl next to the pst")
    p.add_argument('--no_manifest', action='store_true', help="don't write <pst>.manifest.json")
    p.add_argument('--out', metavar='PST', help='output control file path (with a workbook: default is the name in the BUILD sheet)')
    p.add_argument('--dry_run', action='store_true',
                   help="report as `makepst validate` would on the control file this builds; write nothing")
    fmt_ = p.add_mutually_exclusive_group()
    fmt_.add_argument('--v2', action='store_true', help='write a PEST++ version-2 file (external csv tables beside it)')
    fmt_.add_argument('--v1', action='store_true', help='write the classic format (default for tables)')


def build_pst(args, manifest=None):
    """The Pst described by parsed `build` arguments (nothing written)."""
    def table(spec, role):
        if manifest is not None:
            path, sheet = _spec_parts(spec)
            manifest.add_source(path, sheet, role)
        return load_table(spec)

    control = None
    if args.set_ctl_xls:
        control = table(args.set_ctl_xls, 'control')
    elif args.set_ctl_csv:
        control = table(args.set_ctl_csv, 'control')

    pst = Pst(ss=args.ss)
    if control is not None:
        pst.set_control(control)
    if args.mode:
        pst.pestmode = Pst(args.mode).pestmode
    print(f'Building a {pst.pestmode} control file...')

    for c in args.add_comment:
        pst.add_comment(comment=c)
    for t in TABLES:
        for pattern in getattr(args, f'add_{t}_xls') + getattr(args, f'add_{t}_csv'):
            for spec in expand_spec(pattern):            # book,PAR_* -> every matching sheet
                print(f'Adding {t:8s} from {spec}')
                getattr(pst, f'add_{t}')(table(spec, t))
    if args.fill_parval:
        if manifest is not None:
            manifest.add_source(args.fill_parval, role='parval')
        pst.fill_parval(args.fill_parval, args.real)
    return pst


def _version(args):
    return 2 if getattr(args, 'v2', False) else 1 if getattr(args, 'v1', False) else None


def _eol(args):
    """--eol keep|crlf|lf -> what to write with, or None to keep the endings the file was read with."""
    return {'crlf': '\r\n', 'lf': '\n'}.get(getattr(args, 'eol', 'keep'))


def _finish_pst(pst, path, manifest, dump_tpl, version=None, eol=None):
    # Building a control file is an explicit normalization boundary. Library callers can
    # inspect Pst.validate() and choose which available fixes to apply before writing.
    pst.normalize()
    for f in check_pestpp(pst):                 # reported, not blocking: see checks.validate
        print(f)
    write_pst(pst, path, dump_tpl=dump_tpl, version=version, eol=eol)
    if manifest is not None:
        manifest.set_output(path, **pst.counts)
        print(f'manifest written to {manifest.write()}')


def _resolve_build_output(args, book=None):
    # With an omitted output, argparse puts a lone mode in the first positional slot.
    if args.mode is None and args.pstfile and args.pstfile.isalpha():
        try:
            mode = Pst(args.pstfile).pestmode
        except ValueError:
            pass
        else:
            args.mode, args.pstfile = mode, None
    if args.pstfile is None:
        sources = [args.set_ctl_xls, args.set_ctl_csv]
        for t in TABLES:
            sources.extend(getattr(args, f'add_{t}_xls'))
            sources.extend(getattr(args, f'add_{t}_csv'))
        source = next((s for s in sources if s), book)
        if args.out:
            args.pstfile = args.out
        elif source:
            path = _spec_parts(source)[0]
            if book:
                path = os.path.abspath(path)
            args.pstfile = os.path.splitext(path)[0] + '.pst'
        else:
            raise SystemExit('cannot infer output filename; supply a PST filename, --out, or a source table')


def build(args):
    _resolve_build_output(args)
    manifest = None if args.no_manifest or args.dry_run else Manifest('build', args._argv)
    if args.pstfile.lower().endswith(WORKBOOK_EXT):
        # `makepst build book.xlsx [--out x.pst]`: run the command in the workbook's BUILD sheet
        pst, ns = build_from_workbook(args.pstfile, manifest)
        if args.dry_run:
            return check_report(pst, args.pstfile, built=True, dry_run=True)
        out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.pstfile)), ns.out or ns.pstfile)
        _finish_pst(pst, out, manifest, dump_tpl=not (args.no_dump_tpl or ns.no_dump_tpl),
                    version=_version(args) or _version(ns))
        return
    pst = build_pst(args, manifest)
    if args.dry_run:
        return check_report(pst, args.out or args.pstfile, built=True, dry_run=True)
    _finish_pst(pst, args.out or args.pstfile, manifest, dump_tpl=not args.no_dump_tpl, version=_version(args))


def build_from_workbook(book, manifest=None):
    """The Pst a workbook describes in its BUILD sheet (the command `dump` records there),
    with the parsed build arguments; returns (pst, args)."""
    import pandas as pd
    try:
        rows = pd.read_excel(book, 'BUILD', engine='openpyxl').iloc[:, 0].dropna()
    except ValueError:
        raise SystemExit(f'{book} has no BUILD sheet; add one holding the build command (as `makepst dump` '
                         f'does), or name the tables on the command line: `makepst build OUT.pst --set_ctl_xls '
                         f'"{os.path.basename(book)},CONTROL" --add_par_xls ...`, with --dry_run to check them '
                         f'without writing') from None
    argv = [a.strip('"') for a in shlex.split(' '.join(str(r) for r in rows), posix=False)]
    while argv and (argv[0].lower() in ('python', 'makepst', 'build') or argv[0].lower().endswith('makepst.py')):
        argv.pop(0)
    argv = _resolve_paths(argv, book)
    parser = argparse.ArgumentParser(prog='BUILD sheet')
    _build_parser(parser)
    ns = parser.parse_args(argv)
    _resolve_build_output(ns, book)
    if manifest is not None:
        manifest.add_source(book, 'BUILD', 'build command')
        manifest.add(build_args=argv)      # resolved paths, as executed
    return build_pst(ns, manifest), ns


def _resolve_paths(argv, book):
    """Make the file names in a BUILD command usable from anywhere: the workbook's own name means
    this workbook, other relative names are taken relative to the workbook's folder."""
    base = os.path.dirname(os.path.abspath(book))
    out = []
    for a in argv:
        path, sep, sheet = a.rpartition(',')
        if not sep:
            path, sheet = a, ''
        if os.path.basename(path).lower() == os.path.basename(book).lower():
            path = book
        elif not os.path.isabs(path) and os.path.exists(os.path.join(base, path)) and not os.path.exists(path):
            path = os.path.join(base, path)
        out.append(path + sep + sheet)
    return out


def dump(args):
    if args.workbook is None:
        args.workbook = os.path.splitext(args.pstfile)[0] + '.xlsx'
    pst = read_pst(args.pstfile)
    cmd = to_workbook(pst, args.workbook, split=args.split)
    print('rebuild with:\n  ' + cmd)
    if not args.no_manifest:
        m = Manifest('dump', args._argv)
        m.add_source(args.pstfile, role='control file')
        m.set_output(args.workbook, **pst.counts)
        m.add(build_command=cmd)
        print(f'manifest written to {m.write()}')


def init(args):
    from .starter import write_starter
    if os.path.exists(args.workbook) and not args.force:
        raise SystemExit(f'{args.workbook} exists; use --force to overwrite')
    cmd = write_starter(args.workbook, args.name)
    print(f'starter workbook written to {args.workbook}')
    print(f'build it with:\n  {cmd}')


def parrep(args):
    """PARREP: parameter values from a .par file (or IES ensemble) into a control file.

    The base is a control file, or a workbook with a BUILD sheet (built first, then filled).
    """
    manifest = None if args.no_manifest else Manifest('parrep', args._argv)
    if args.pstfile.lower().endswith(WORKBOOK_EXT):
        pst, _ = build_from_workbook(args.pstfile, manifest)
    else:
        pst = read_pst(args.pstfile)
        if manifest is not None:
            manifest.add_source(args.pstfile, role='base control file')
    if manifest is not None:
        manifest.add_source(args.parfile, role='parameter values')
        manifest.add(realization=args.real, set=args.set)
    pst.fill_parval(args.parfile, args.real)
    for kv in args.set:
        k, _, v = kv.partition('=')
        if not _:
            raise SystemExit(f'--set expects NAME=VALUE, got {kv!r}')
        pst.set_control({k: v})
    _finish_pst(pst, args.out, manifest, dump_tpl=False, version=_version(args), eol=_eol(args))


def set_par(args):
    """Set parameter fields by name or glob, keeping tied children and preferred values consistent."""
    manifest = None if args.no_manifest else Manifest('set', args._argv)
    pst = read_pst(args.pstfile)
    if manifest is not None:
        manifest.add_source(args.pstfile, role='base control file')
    changes = {}
    for spec in args.par:
        name, sep, fields = spec.partition(':')
        if not sep or not fields.strip():
            raise SystemExit(f'--par expects NAME:FIELD=VALUE[,FIELD=VALUE], got {spec!r}')
        want = {}
        for item in fields.split(','):
            k, eq, v = item.partition('=')
            if not eq:
                raise SystemExit(f'--par expects FIELD=VALUE, got {item!r} in {spec!r}')
            want[k.strip()] = v.strip()
        changes.setdefault(name.strip(), {}).update(want)
    try:
        report = pst.set_par(changes, sync_ties=not args.no_sync_ties, sync_prior=not args.no_sync_prior)
    except ValueError as e:
        raise SystemExit(str(e))
    for kv in args.set:
        k, _, v = kv.partition('=')
        if not _:
            raise SystemExit(f'--set expects NAME=VALUE, got {kv!r}')
        pst.set_control({k: v})
    print(f"{len(report['parameters'])} parameters set"
          + (f", {len(report['tied_rescaled'])} tied children rescaled" if report['tied_rescaled'] else '')
          + (f", {len(report['prior_retargeted'])} preferred values retargeted" if report['prior_retargeted'] else ''))
    if manifest is not None:
        manifest.add(changes=changes, sync_ties=not args.no_sync_ties, sync_prior=not args.no_sync_prior)
    _finish_pst(pst, args.out, manifest, dump_tpl=False, version=_version(args), eol=_eol(args))


def rescale(args):
    """Move parameter values into SCALE (every selected parameter starts at 1), or back with --undo."""
    manifest = None if args.no_manifest else Manifest('rescale', args._argv)
    pst = read_pst(args.pstfile)
    if manifest is not None:
        manifest.add_source(args.pstfile, role='base control file')
    flat = lambda items: [x.strip() for s in items for x in s.split(',') if x.strip()]   # noqa: E731
    try:
        report = pst.rescale_par(flat(args.par) or None, flat(args.pargp) or None,
                                 include_fixed=not args.exclude_fixed, undo=args.undo)
    except ValueError as e:
        raise SystemExit(str(e))
    for kv in args.set:
        k, _, v = kv.partition('=')
        if not _:
            raise SystemExit(f'--set expects NAME=VALUE, got {kv!r}')
        pst.set_control({k: v})
    print(f"{len(report['rescaled'])} parameters {'unscaled' if args.undo else 'rescaled to 1'}"
          + (f", {len(report['prior_rewritten'])} prior equations rewritten" if report['prior_rewritten'] else ''))
    for name, why in list(report['skipped'].items())[:10]:
        print(f'SKIPPED {name}: {why}')
    if len(report['skipped']) > 10:
        print(f"SKIPPED ... {len(report['skipped']) - 10} more")
    for note in report['warnings']:
        print(f'WARNING {note}')
    if manifest is not None:
        manifest.add(par=args.par, pargp=args.pargp, include_fixed=not args.exclude_fixed, undo=args.undo,
                     rescaled=len(report['rescaled']), skipped=report['skipped'],
                     prior_rewritten=report['prior_rewritten'])
    _finish_pst(pst, args.out, manifest, dump_tpl=False, version=_version(args), eol=_eol(args))


def _factor_specs(specs):
    factors = {}
    for spec in specs:
        group, sep, value = spec.partition('=')
        if not sep or not group.strip() or not value.strip():
            raise ValueError(f'--factor expects GROUP=NUMBER, got {spec!r}')
        group = group.strip().lower()
        if group in factors:
            raise ValueError(f'duplicate group factor for {group}')
        try:
            factors[group] = float(value)
        except ValueError:
            raise ValueError(f'factor for {group} must be numeric') from None
    return factors


def _residual_source(args, pst):
    if args.res:
        return read_res(args.res)
    if args.obs_csv:
        return read_obs_ensemble(args.obs_csv, args.real, pst=pst)
    return None


def reweight(args):
    """Adjust observation weights by group contribution or explicit factors."""
    if not args.dry_run and not args.out:
        raise SystemExit('reweight requires OUT unless --dry_run is used')
    if args.out and os.path.abspath(args.pstfile) == os.path.abspath(args.out):
        raise SystemExit('reweight input and output must be different files')
    if args.real and not args.obs_csv:
        raise SystemExit('--real requires --obs_csv')

    manifest = None if args.no_manifest or args.dry_run else Manifest('reweight', args._argv)
    pst = read_pst(args.pstfile)
    if manifest is not None:
        manifest.add_source(args.pstfile, role='base control file')
    if args.res and manifest is not None:
        manifest.add_source(args.res, role='residuals')
    if args.obs_csv and manifest is not None:
        manifest.add_source(args.obs_csv, role='observation ensemble')

    try:
        residuals = _residual_source(args, pst)
        clip = tuple(args.clip) if args.clip is not None else None
        if args.equal:
            if residuals is None:
                raise ValueError('--equal requires --res or --obs_csv')
            shares, skipped = equal_shares(pst, residuals)
            for group, reason in skipped.items():
                print(f'INFO    skipping group {group}: {reason}')
            if not shares:
                raise ValueError('no eligible positively weighted observation groups with nonzero phi')
            report = balance_weights(pst, residuals, shares, clip=clip)
            mode = 'equal'
            settings = {'groups': list(shares)}
        elif args.discrepancy:
            if residuals is None:
                raise ValueError('--discrepancy requires --res or --obs_csv')
            report, skipped = discrepancy_weights(pst, residuals, by=args.discrepancy, clip=clip)
            for group, reason in skipped.items():
                print(f'INFO    skipping group {group}: {reason}')
            mode = 'discrepancy'
            settings = {'by': args.discrepancy, 'groups': report['OBGNME'].tolist()}
        elif args.targets:
            path, sheet = _spec_parts(args.targets)
            if manifest is not None:
                manifest.add_source(path, sheet, role='target group shares')
            table = load_table(args.targets)
            required = {'OBGNME', 'TARGET_SHARE'}
            missing = required - set(table.columns)
            if missing:
                raise ValueError(f'--targets needs columns: {", ".join(sorted(missing))}')
            shares = {}
            for row in table[['OBGNME', 'TARGET_SHARE']].itertuples(index=False):
                group = str(row.OBGNME).strip().lower()
                if not group or group == 'nan':
                    raise ValueError('--targets contains a blank OBGNME')
                if group in shares:
                    raise ValueError(f'duplicate target group {group}')
                shares[group] = row.TARGET_SHARE
            report = balance_weights(pst, residuals, shares, clip=clip)
            mode = 'targets'
            settings = {'shares': shares}
        else:
            factors = _factor_specs(args.factor)
            if not factors:
                raise ValueError('at least one --factor is required')
            report = scale_weights(pst, factors, clip=clip, residuals=residuals)
            mode = 'factor'
            settings = {'factors': factors}
    except (OSError, ValueError) as e:
        raise SystemExit(str(e)) from None

    print(report.to_string(index=False))
    if args.report:
        if args.dry_run:
            print(f'INFO    --dry_run: report not written to {args.report}')
        else:
            report.to_csv(args.report, index=False)
            print(f'written {args.report}: {len(report)} groups')
    if manifest is not None:
        manifest.add(mode=mode, settings=settings, clip=clip, report=args.report, realization=args.real)
    if args.dry_run:
        return
    _finish_pst(pst, args.out, manifest, dump_tpl=False, version=_version(args), eol=_eol(args))


def guess_base_dir(pst, target):
    """The folder the control file's relative paths are meant from.

    PEST resolves them from the directory it is run in, which is often the control file's
    folder but just as often the model root with the control file in a subfolder. Try the
    control file's folder, the current directory and the control file's parent, and take the
    one in which the most template / instruction files exist (the control file's folder wins ties).
    """
    here = os.path.dirname(os.path.abspath(target))
    candidates = [here, os.getcwd(), os.path.dirname(here)]
    files = [f for f, _ in pst.tpl + pst.ins]
    best, hits = here, -1
    for base in dict.fromkeys(candidates):
        n = sum(os.path.exists(os.path.join(base, f)) for f in files)
        if n > hits:
            best, hits = base, n
    return best


def validate(args):
    """pestchek-style report for a control file or a workbook (built via its BUILD sheet)."""
    target = args.target
    if target.lower().endswith(WORKBOOK_EXT):
        check_report(build_from_workbook(target)[0], target, built=True, base_dir=args.base_dir,
                     outputs=args.outputs, quiet=args.quiet, strict=args.strict)
    else:
        check_report(read_pst(target), target, pst_path=target, base_dir=args.base_dir,
                     outputs=args.outputs, quiet=args.quiet, strict=args.strict)


def check_report(pst, target, built=False, pst_path=None, base_dir=None, outputs=False, quiet=False,
                 strict=False, dry_run=False):
    """Print the validate report for `pst`; exit 1 on errors (or warnings with `strict`).

    `built`: the Pst comes from tables, not from a control file. It is then judged as `build` would write it,
    after the same normalization (regularisation equations from PRIOR/WEIGHT, unused groups removed, ...);
    judging the tables before it would report errors that building removes. The fixes are listed as notes.
    """
    from .checks import Finding, summary, validate as run_checks
    from .provenance import checked_against
    fixes = pst.validate().fixes_available
    if built:
        pst.normalize()
        notes = [Finding('info', 'normalization', f'{fix} (done by build)') for fix in fixes]
    else:
        notes = [Finding('info', 'normalization', str(fix)) for fix in fixes]
    base = base_dir or guess_base_dir(pst, target)
    findings = run_checks(pst, base_dir=base, pst_path=pst_path, outputs=outputs) + notes
    if not base_dir and not quiet:
        print(f'INFO    file paths resolved relative to {base} (override with --base_dir)')
    if not quiet:
        v = checked_against()
        print(f"INFO    rules from PEST {v['pestchek']}'s pestchek and PEST++ {v['pestpp']}'s options")
    order = {'error': 0, 'warning': 1, 'info': 2}
    for f in sorted(findings, key=lambda f: order[f.severity]):
        if f.severity == 'info' and quiet:
            continue
        print(f)
    n, text = summary(findings)
    print(f'{target}: {text}' + (' (dry run: nothing written)' if dry_run else ''))
    if n['error'] or (strict and n['warning']):
        raise SystemExit(1)


def _load_target(path):
    """A control file or a workbook (built via its BUILD sheet) -> Pst."""
    if path.lower().endswith(WORKBOOK_EXT):
        return build_from_workbook(path)[0]
    return read_pst(path)


def _is_case(path):
    return path.lower().endswith(('.pst',) + WORKBOOK_EXT)


def _report(nerr, nwarn, label):
    print(f'{label}: {nerr} error{"s" if nerr != 1 else ""}, {nwarn} warning{"s" if nwarn != 1 else ""}')
    if nerr:
        raise SystemExit(1)


def tempchek(args):
    """TEMPCHEK: check a template file, or write model input files from parameter values.

    makepst tempchek model.tpl                      check only, list the parameters
    makepst tempchek model.tpl model.in [case.par]  write model.in (values from model.par by default)
    makepst tempchek case.pst [--par FILE]          write every model input file of the case
    """
    from .checks import template_names, template_problems
    from .model_files import fill_template, par_header, scaled_values
    from .writer import effective_control
    target, nerr, nwarn = args.target, 0, 0

    if _is_case(target):
        pst = _load_target(target)
        base = args.base_dir or guess_base_dir(pst, target)
        ctl = effective_control(pst)
        precis, dpoint = ctl.get('precis', 'single'), ctl.get('dpoint', 'point')
        par = pst.par.set_index('PARNME')[['PARVAL1', 'SCALE', 'OFFSET']].copy()
        if args.par:
            hit, new, _ = _to_internal(read_par(args.par, args.real), pst.par)   # through SCALE / OFFSET
            missing = list(pst.par.loc[~hit, 'PARNME'])
            if missing:
                print(f'ERROR   {args.par}: no value for {len(missing)} parameters: {", ".join(missing[:6])}')
                raise SystemExit(1)
            par['PARVAL1'] = new.values
        values = scaled_values(par)
        if not args.base_dir:
            print(f'INFO    file paths resolved relative to {base} (override with --base_dir)')
        for tpl, model_in in pst.tpl:
            tpath = os.path.join(base, tpl)
            problems = template_problems(tpath) if os.path.exists(tpath) else ['template file not found']
            text = None
            if not problems:
                text, problems = fill_template(tpath, values, precis, dpoint)
            for p in problems:
                print(f'ERROR   {tpl}: {p}')
            nerr += len(problems)
            if text is not None:
                if args.dry_run:
                    print(f'OK      {tpl}: {model_in} can be written')
                else:
                    ipath = os.path.join(base, model_in)
                    with open(ipath, 'w') as f:
                        f.write(text)
                    print(f'written {model_in} from {tpl}')
        _report(nerr, nwarn, target)
        return

    tpl, modfile, parfile = target, args.modfile, args.parfile
    problems = template_problems(tpl) if os.path.exists(tpl) else ['template file not found']
    names = []
    if not problems:
        try:
            names = list(dict.fromkeys(template_names(tpl)))
        except ValueError as e:
            problems = [str(e)]
    for p in problems:
        print(f'ERROR   {tpl}: {p}')
    nerr += len(problems)
    if nerr:
        _report(nerr, nwarn, tpl)
    if not names:
        print(f'WARNING {tpl}: no parameters identified')
        nwarn += 1
    if modfile is None:
        print(f'{len(names)} parameters identified in {tpl}: {" ".join(names)}')
        _report(nerr, nwarn, tpl)
        return
    if parfile is None:
        parfile = os.path.splitext(tpl)[0] + '.par'
    if not os.path.exists(parfile):
        print(f'ERROR   parameter value file {parfile} not found')
        raise SystemExit(1)
    precis, dpoint = par_header(parfile)
    values = scaled_values(read_par(parfile, args.real))
    for extra in sorted(set(values) - set(names)):
        print(f'WARNING parameter "{extra}" from {parfile} not cited in {tpl}')
        nwarn += 1
    text, problems = fill_template(tpl, values, precis, dpoint)
    for p in problems:
        print(f'ERROR   {tpl}: {p}')
    nerr += len(problems)
    if text is not None:
        with open(modfile, 'w') as f:
            f.write(text)
        print(f'written {modfile} from {tpl} with values from {parfile}')
    _report(nerr, nwarn, tpl)


def inschek(args):
    """INSCHEK: check an instruction file, or read a model output file with it and write the values to an .obf file.

    makepst inschek heads.ins                   check only, list the observations
    makepst inschek heads.ins heads.out         write heads.obf
    makepst inschek case.pst                    every instruction file of the case -> case.obf
    """
    from .checks import InstructionError, instruction_names, instruction_problems, run_instructions
    from .model_files import write_obf
    target, nerr, nwarn = args.target, 0, 0

    if _is_case(target):
        pst = _load_target(target)
        base = args.base_dir or guess_base_dir(pst, target)
        if not args.base_dir:
            print(f'INFO    file paths resolved relative to {base} (override with --base_dir)')
        values = {}
        for ins, model_out in pst.ins:
            ipath, opath = os.path.join(base, ins), os.path.join(base, model_out)
            problems = instruction_problems(ipath) if os.path.exists(ipath) else ['instruction file not found']
            if not problems and not os.path.exists(opath):
                problems = [f'model output file {model_out} not found']
            if not problems:
                try:
                    got = run_instructions(ipath, opath)
                    values.update(got)
                    print(f'read    {len(got)} observations from {model_out} with {ins}')
                except InstructionError as e:
                    problems = [f'reading {model_out} failed: {e}']
            for p in problems:
                print(f'ERROR   {ins}: {p}')
            nerr += len(problems)
        unread = [n for n in pst.obs['OBSNME'] if n not in values]
        if unread:
            print(f'WARNING {len(unread)} observations in the control file were not read: {", ".join(unread[:6])}')
            nwarn += 1
        out = args.out or os.path.splitext(target)[0] + '.obf'
        write_obf(out, values)
        print(f'written {out}: {len(values)} observation values')
        _report(nerr, nwarn, target)
        return

    ins, modfile = target, args.modfile
    problems = instruction_problems(ins) if os.path.exists(ins) else ['instruction file not found']
    names = []
    if not problems:
        try:
            names = list(dict.fromkeys(instruction_names(ins)))
        except ValueError as e:
            problems = [str(e)]
    for p in problems:
        print(f'ERROR   {ins}: {p}')
    nerr += len(problems)
    if nerr:
        _report(nerr, nwarn, ins)
    if not names:
        print(f'WARNING {ins}: no observations identified')
        nwarn += 1
    if modfile is None:
        print(f'{len(names)} observations identified in {ins}: {" ".join(names)}')
        _report(nerr, nwarn, ins)
        return
    if not os.path.exists(modfile):
        print(f'ERROR   model output file {modfile} not found')
        raise SystemExit(1)
    try:
        values = run_instructions(ins, modfile)
    except InstructionError as e:
        print(f'ERROR   {ins}: reading {modfile} failed: {e}')
        raise SystemExit(1) from None
    out = args.out or os.path.splitext(ins)[0] + '.obf'
    write_obf(out, values)
    print(f'written {out}: {len(values)} observations read from {modfile} with {ins}')
    _report(nerr, nwarn, ins)


def diff(args):
    """Semantic comparison of two control files / workbooks; exit 1 when they differ."""
    from .diff import compare
    old, new = _load_target(args.old), _load_target(args.new)
    # a workbook stands for the control file `build` would write from it, which is normalized;
    # a control file is compared as it is
    for path, pst in ((args.old, old), (args.new, new)):
        if path.lower().endswith(WORKBOOK_EXT):
            pst.normalize()
    d = compare(old, new, rtol=args.rtol)
    print(d.to_text(max_rows=0 if args.summary else args.max_rows))
    summary = f'{args.old} -> {args.new}: {d.summary()}'
    print(summary)
    if args.file:
        full_report = d.to_text(max_rows=None)
        with open(args.file, 'w', encoding='utf-8', newline='') as f:
            f.write(full_report)
            f.write('\n' + summary + '\n')
        print(f'written to {args.file}')
    if args.xlsx:
        d.to_workbook(args.xlsx)
        print(f'written to {args.xlsx}')
    if not d.empty:
        raise SystemExit(1)


def update(args):
    pst = read_pst(args.pst) if args.pst else None
    # With result files, --pst supplies context rather than additional values to write.
    pst_only = args.par is None and args.obs_csv is None and args.res is None
    par = args.par or (pst if pst_only else None)
    obs = pst if pst_only else None
    if args.par and pst is not None:
        # .par values with the groups from the pst, so --group can apply to them
        par = (read_par(args.par, args.real).reset_index()
               .merge(pst.par[['PARNME', 'PARGP']], on='PARNME', how='left'))

    def flat(items):   # repeated flags, each possibly a comma list
        return [x.strip() for s in items for x in s.split(',') if x.strip()] or None

    manifest = None if args.no_manifest or args.dry_run else Manifest('update', args._argv)
    if manifest is not None:
        manifest.add_source(args.workbook, role='workbook')
        for path, role in ((args.par, 'parameter values'), (args.pst, 'control file'),
                           (args.res, 'residuals'), (args.obs_csv, 'observation ensemble')):
            if path:
                manifest.add_source(path, role=role)
    result = update_workbook(args.workbook, par=par, obs=obs, res=args.res, obs_csv=args.obs_csv,
                             real=args.real, pst=pst, out=args.out, backend=args.backend,
                             par_cols=args.par_cols.upper().split(','), obs_cols=args.obs_cols.upper().split(','),
                             overwrite_formulas=args.overwrite_formulas,
                             sheets=flat(args.sheet), groups=flat(args.group), phi=not args.no_phi,
                             dry_run=args.dry_run)
    if manifest is not None:
        manifest.set_output(args.out or args.workbook, sheets=result['sheets'], backend=result['backend'])
        manifest.add(realization=args.real, par_cols=args.par_cols, obs_cols=args.obs_cols,
                     sheet=flat(args.sheet), group=flat(args.group), overwrite_formulas=args.overwrite_formulas)
        print(f'manifest written to {manifest.write()}')


def provenance(args):
    path = args.target if args.target.endswith('.manifest.json') else args.target + '.manifest.json'
    for status, name in check_manifest(path):
        print(f'{status:12s} {name}')
        if status != 'unchanged':
            args._failed = True
    if getattr(args, '_failed', False):
        raise SystemExit(1)


def log(args):
    """The ledger: every manifest under the folders, oldest first — what produced which file from what."""
    from .provenance import find_manifests, log_line, mentions
    rows = find_manifests(args.folder or ['.'], recursive=not args.no_recursive)
    if args.file:
        rows = [r for r in rows if any(mentions(r, f) for f in args.file)]
    if args.command:
        rows = [r for r in rows if r.get('command') in args.command]
    if args.last:
        rows = rows[-args.last:]
    for r in rows:
        print(log_line(r, check=args.check))
    if not rows:
        print('no manifests found')


def bundle(args):
    """Zip a run so it can be reproduced or reviewed elsewhere (control file, manifest, templates,
    instruction files, model inputs, command files; --sources / --outputs / --extra for more)."""
    from .provenance import bundle_run
    pst = read_pst(args.pstfile)
    base = args.base_dir or guess_base_dir(pst, args.pstfile)
    manifest = None if (args.no_manifest or args.list) else Manifest('bundle', args._argv)
    out = None if args.list else (args.out or os.path.splitext(args.pstfile)[0] + '.zip')
    included, missing = bundle_run(pst, args.pstfile, out, base_dir=base, sources=args.sources,
                                   outputs=args.outputs, extra=args.extra, manifest=manifest)

    def show(path):
        rel = os.path.relpath(path, base)
        return path if rel.startswith('..') else rel

    for role, path in included:
        print(f'{"would add" if args.list else "added":10s} {role:18s} {show(path)}')
    for role, path in missing:
        print(f'MISSING    {role:18s} {show(path)}')
    if args.list:
        print(f'{len(included)} files, {len(missing)} missing (nothing written)')
        return
    size = os.path.getsize(out) / 1e6
    print(f'written {out}: {len(included)} files, {size:.1f} MB' + (f', {len(missing)} missing' if missing else ''))
    if manifest is not None:
        manifest.set_output(out, files=len(included), missing=[p for _, p in missing])
        print(f'manifest written to {manifest.write()}')
    if missing and args.strict:
        raise SystemExit(1)


def hpstart(args):
    from .hpstart import write_hpstart
    pst = read_pst(args.pstfile)
    manifest = None if args.no_manifest else Manifest('hpstart', args._argv)
    if manifest is not None:
        for path, role in ((args.pstfile, 'control file'), (args.template, 'hp template'),
                           (args.res, 'residuals'), (args.obs_csv, 'observation ensemble'),
                           (args.par, 'parameter values')):
            if path:
                manifest.add_source(path, role=role)
    counts = write_hpstart(pst, args.template, args.out, res=args.res,
                           obs_csv=args.obs_csv, real=args.real, par=args.par)
    print(f'written {args.out}: {counts["npar"]} parameters, {counts["nobs"]} observations')
    if manifest is not None:
        manifest.set_output(args.out, **counts)
        manifest.add(realization=args.real)
        print(f'manifest written to {manifest.write()}')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # legacy form: makePst.py out.pst mode --add_par_xls ...  (no subcommand)
    if argv and argv[0] not in ('build', 'dump', 'update', 'parrep', 'init', 'set', 'rescale', 'reweight', 'validate', 'provenance', 'log',
                                'bundle', 'diff', 'tempchek', 'inschek', 'hpstart',
                                '-h', '--help', '-v', '--version'):
        argv.insert(0, 'build')

    ap = argparse.ArgumentParser(prog='makepst', description=__doc__)
    ap.add_argument('-v', '--version', action='version', version=f'%(prog)s {__version__}')
    sub = ap.add_subparsers(dest='cmd', required=True)

    _build_parser(sub.add_parser('build', help='Excel/CSV tables -> PEST control file'))

    d = sub.add_parser('dump', help='PEST control file -> new workbook')
    d.add_argument('pstfile')
    d.add_argument('workbook', nargs='?', help='.xlsx to create (default: PST path with .xlsx extension)')
    d.add_argument('--split', action='store_true', help='one PAR_<group> / OBS_<group> sheet per group')
    d.add_argument('--no_manifest', action='store_true', help="don't write <workbook>.manifest.json")

    i = sub.add_parser('init', help='starter workbook with headers, defaults, drop-downs and a BUILD sheet')
    i.add_argument('workbook', help='.xlsx to create')
    i.add_argument('--name', help='case name for the control file in the BUILD command (default: workbook name)')
    i.add_argument('--force', action='store_true', help='overwrite an existing file')

    r = sub.add_parser('parrep', help='new control file with parameter values from a .par file (like PARREP)')
    r.add_argument('pstfile', help='control file to read, or a workbook with a BUILD sheet to build it from')
    r.add_argument('parfile', help='PEST .par file or PESTPP-IES case.N.par.csv')
    r.add_argument('out', help='control file to write')
    r.add_argument('--real', metavar='NAME', help='realization for an ensemble (base, 17, best; default base)')
    r.add_argument('--set', action='append', default=[], metavar='NAME=VALUE',
                   help='also change a control value, e.g. --set noptmax=0 (repeatable)')
    r.add_argument('--no_manifest', action='store_true', help="don't write <out>.manifest.json")
    r.add_argument('--eol', choices=('keep', 'crlf', 'lf'), default='keep',
                   help="line endings to write (default: the input file's own)")
    rf = r.add_mutually_exclusive_group()
    rf.add_argument('--v2', action='store_true', help='write a PEST++ version-2 file')
    rf.add_argument('--v1', action='store_true', help='write the classic format (default: same as the input)')

    s = sub.add_parser('set', help='set parameter fields by name or glob, keeping ties and preferred values consistent')
    s.add_argument('pstfile', help='control file to read')
    s.add_argument('out', help='control file to write')
    s.add_argument('--par', action='append', required=True, metavar='NAME:FIELD=VALUE,...',
                   help='parameter name or glob, then the fields to set, e.g. '
                        '--par "sycr0*:parval1=0.15,parlbnd=0.08,parubnd=0.3" (repeatable)')
    s.add_argument('--set', action='append', default=[], metavar='NAME=VALUE',
                   help='also change a control value, e.g. --set noptmax=0 (repeatable)')
    s.add_argument('--no_sync_ties', action='store_true',
                   help="don't rescale tied children with their parent (they would keep the old ratio and bounds)")
    s.add_argument('--no_sync_prior', action='store_true',
                   help="don't move single-parameter preferred values to the new value (they would pull it back)")
    s.add_argument('--no_manifest', action='store_true', help="don't write <out>.manifest.json")
    s.add_argument('--eol', choices=('keep', 'crlf', 'lf'), default='keep',
                   help="line endings to write (default: the input file's own)")
    sf = s.add_mutually_exclusive_group()
    sf.add_argument('--v2', action='store_true', help='write a PEST++ version-2 file')
    sf.add_argument('--v1', action='store_true', help='write the classic format (default: same as the input)')

    rs = sub.add_parser('rescale',
                        help='move parameter values into SCALE so they start at 1; the model sees the same values')
    rs.add_argument('pstfile', help='control file to read')
    rs.add_argument('out', help='control file to write')
    rs.add_argument('--par', action='append', default=[], metavar='NAME',
                    help='parameter names or globs, e.g. --par "hk*" (repeatable or comma-separated; default all)')
    rs.add_argument('--pargp', action='append', default=[], metavar='GROUP',
                    help='parameter groups (repeatable or comma-separated); with --par, both must match')
    rs.add_argument('--exclude_fixed', action='store_true', help='leave fixed parameters as they are')
    rs.add_argument('--undo', action='store_true',
                    help='fold SCALE back into the values instead (PARVAL1 * SCALE, SCALE = 1)')
    rs.add_argument('--set', action='append', default=[], metavar='NAME=VALUE',
                    help='also change a control value, e.g. --set noptmax=0 (repeatable)')
    rs.add_argument('--no_manifest', action='store_true', help="don't write <out>.manifest.json")
    rs.add_argument('--eol', choices=('keep', 'crlf', 'lf'), default='keep',
                    help="line endings to write (default: the input file's own)")
    rsf = rs.add_mutually_exclusive_group()
    rsf.add_argument('--v2', action='store_true', help='write a PEST++ version-2 file')
    rsf.add_argument('--v1', action='store_true', help='write the classic format (default: same as the input)')

    rw = sub.add_parser('reweight', help='adjust observation weights by group contribution or factor')
    rw.add_argument('pstfile', help='control file to read')
    rw.add_argument('out', nargs='?', help='new control file to write (required unless --dry_run)')
    mode = rw.add_mutually_exclusive_group(required=True)
    mode.add_argument('--equal', action='store_true', help='equalize initial phi among eligible measurement groups')
    mode.add_argument('--discrepancy', choices=('group', 'obs'),
                      help='match weights to the current misfit: group = scale each group so its phi equals '
                           'its weighted-observation count; obs = w = min(w, 1/|residual|) per observation')
    mode.add_argument('--targets', metavar='TABLE', help='CSV or BOOK,SHEET with OBGNME and TARGET_SHARE columns')
    mode.add_argument('--factor', action='append', default=[], metavar='GROUP=FACTOR',
                      help='multiply one group\'s weights (repeatable)')
    source = rw.add_mutually_exclusive_group()
    source.add_argument('--res', metavar='RESFILE', help='PEST .res/.rei residuals')
    source.add_argument('--obs_csv', metavar='FILE', help='PESTPP-IES observation ensemble')
    rw.add_argument('--real', metavar='NAME', help='IES realization (default base)')
    rw.add_argument('--clip', nargs=2, type=float, metavar=('MIN', 'MAX'),
                    help='clip positive final weights to MIN..MAX; original zeros stay zero')
    rw.add_argument('--report', metavar='CSV', help='write the group before/after report to CSV')
    rw.add_argument('--dry_run', action='store_true', help='preview the adjustment without writing files')
    rw.add_argument('--no_manifest', action='store_true', help="don't write <out>.manifest.json")
    rw.add_argument('--eol', choices=('keep', 'crlf', 'lf'), default='keep',
                    help="line endings to write (default: the input file's own)")
    rwf = rw.add_mutually_exclusive_group()
    rwf.add_argument('--v2', action='store_true', help='write a PEST++ version-2 file')
    rwf.add_argument('--v1', action='store_true', help='write the classic format')

    h = sub.add_parser('hpstart', help='update a PEST_HP .hp file from residual or IES observation values')
    h.add_argument('pstfile', help='control file defining observation and parameter order')
    h.add_argument('out', help='new .hp file (must differ from the template)')
    h.add_argument('--template', required=True, help='existing .hp with matching order; instruction metadata is preserved')
    hs = h.add_mutually_exclusive_group(required=True)
    hs.add_argument('--res', help='PEST .res/.rei file providing MODELLED values')
    hs.add_argument('--obs_csv', help='PESTPP-IES case.N.obs.csv providing modeled values')
    h.add_argument('--real', help='IES realization name, or best (default base)')
    h.add_argument('--par', help='optional .par or IES parameter file to replace template parameter values')
    h.add_argument('--no_manifest', action='store_true', help="don't write <out>.manifest.json")

    v = sub.add_parser('validate', help='pestchek-style checks on a control file or a workbook')
    v.add_argument('target', help='control file, or a workbook with a BUILD sheet')
    v.add_argument('--outputs', action='store_true',
                   help='also run each instruction file against its model output file when that exists')
    v.add_argument('--base_dir', metavar='DIR', help='folder template/instruction paths are relative to '
                                                     '(default: the folder of the target)')
    v.add_argument('--strict', action='store_true', help='exit 1 on warnings as well as errors')
    v.add_argument('--quiet', action='store_true', help='hide informational lines')

    q = sub.add_parser('provenance', help='compare files against a saved manifest')
    q.add_argument('target', help='manifest path, or output path with a .manifest.json sidecar')

    g = sub.add_parser('log', help='ledger of every manifest under a folder: what produced which file from what')
    g.add_argument('folder', nargs='*', help='folders (or manifest files) to search; default the current one')
    g.add_argument('--file', action='append', default=[], metavar='NAME',
                   help='only entries whose output or sources include this file name, path or sha256 prefix')
    g.add_argument('--command', action='append', default=[], metavar='CMD', help='only this command (repeatable)')
    g.add_argument('--last', type=int, metavar='N', help='only the N most recent entries')
    g.add_argument('--check', action='store_true', help='also compare every recorded hash with the file on disk')
    g.add_argument('--no_recursive', action='store_true', help='do not descend into subfolders')

    b = sub.add_parser('bundle', help='zip a run for reproduction or review: control file, manifest, templates, '
                                      'instruction files, model inputs, command files')
    b.add_argument('pstfile', help='control file')
    b.add_argument('out', nargs='?', help='zip file to write (default: the control file name with .zip)')
    b.add_argument('--sources', action='store_true', help="also the manifest's sources (the workbook, .par, ...)")
    b.add_argument('--outputs', action='store_true', help='also the model output files the instruction files read')
    b.add_argument('--extra', action='append', default=[], metavar='GLOB',
                   help='more files, relative to the run directory (repeatable; ** allowed)')
    b.add_argument('--base_dir', metavar='DIR', help='the directory PEST runs in (default: guessed as validate does)')
    b.add_argument('--list', action='store_true', help='show what would be bundled, write nothing')
    b.add_argument('--strict', action='store_true', help='exit 1 when a referenced file is missing')
    b.add_argument('--no_manifest', action='store_true', help="don't write <zip>.manifest.json")

    t = sub.add_parser('tempchek', help='check a template file, or write model input files from parameter values (like TEMPCHEK)')
    t.add_argument('target', help='template file, or a control file / workbook (all of its templates)')
    t.add_argument('modfile', nargs='?', help='with a template: model input file to write')
    t.add_argument('parfile', nargs='?', help='with a template and modfile: parameter value file (default <template>.par)')
    t.add_argument('--par', metavar='FILE',
                   help='with a control file: values from a .par file or IES ensemble instead of PARVAL1')
    t.add_argument('--real', metavar='NAME', help='realization for an ensemble (base, 17, best; default base)')
    t.add_argument('--base_dir', metavar='DIR', help='with a control file: folder its paths are relative to')
    t.add_argument('--dry_run', action='store_true', help='with a control file: check that every value fits, write nothing')

    n = sub.add_parser('inschek', help='check an instruction file, or read a model output file with it (like INSCHEK)')
    n.add_argument('target', help='instruction file, or a control file / workbook (all of its instruction files)')
    n.add_argument('modfile', nargs='?', help='with an instruction file: model output file to read')
    n.add_argument('--out', metavar='OBF', help='observation value file to write (default <instruction file>.obf, or <case>.obf)')
    n.add_argument('--base_dir', metavar='DIR', help='with a control file: folder its paths are relative to')

    f = sub.add_parser('diff', help='what changed between two control files (or workbooks)')
    f.add_argument('old', help='control file or workbook')
    f.add_argument('new', help='control file or workbook')
    f.add_argument('--xlsx', metavar='FILE', help='also write the differences to a workbook, one sheet per table')
    f.add_argument('--file', metavar='FILE', help='write the full text report, including all difference rows')
    f.add_argument('--rtol', type=float, default=1e-9, help='relative tolerance for numbers (default 1e-9)')
    f.add_argument('--max_rows', type=int, default=50, help='rows printed per table (default 50)')
    f.add_argument('--summary', action='store_true',
                   help='counts per table, change kind and column instead of the rows themselves')

    u = sub.add_parser('update', help='write PEST results back into an existing workbook')
    u.add_argument('workbook', help='.xlsm/.xlsx to update (sheets matched by PARNME / OBSNME columns)')
    u.add_argument('--par', metavar='FILE', help='PEST .par file or PESTPP-IES case.N.par.csv -> PARVAL1')
    u.add_argument('--pst', metavar='PSTFILE', help='control file -> parameter and observation columns')
    u.add_argument('--res', metavar='RESFILE', help='PEST .res/.rei -> MODELLED / RESIDUAL columns')
    u.add_argument('--obs_csv', metavar='FILE',
                   help='PESTPP-IES case.N.obs.csv -> MODELLED (and RESIDUAL when --pst is given)')
    u.add_argument('--real', metavar='NAME',
                   help='realization to take from IES ensembles: a name such as base or 17, '
                        'or best (lowest phi in case.phi.actual.csv); default base')
    u.add_argument('--par_cols', default='PARVAL1', help='comma list of parameter columns to write')
    u.add_argument('--obs_cols', default='OBSVAL,WEIGHT', help='comma list of observation columns to write')
    u.add_argument('--sheet', action='append', default=[], metavar='GLOB',
                   help='only worksheets matching this name pattern, e.g. PAR_HK or "OBS_*" (repeatable, or a comma list)')
    u.add_argument('--group', action='append', default=[], metavar='NAME',
                   help='only parameters/observations in this group (repeatable, or a comma list; '
                        'with --par alone, also give --pst)')
    u.add_argument('--overwrite_formulas', action='store_true',
                   help='also write into cells that currently hold formulas (default: leave them alone)')
    u.add_argument('--out', help='save to this file instead of in place')
    u.add_argument('--dry_run', action='store_true', help='preview matches and skipped cells without writing files')
    u.add_argument('--backend', choices=['xlwings', 'openpyxl'], help='default: xlwings if installed')
    u.add_argument('--no_manifest', action='store_true', help="don't write <workbook>.manifest.json")
    u.add_argument('--no_phi', action='store_true', help="don't write the PHI / PHI_IES sheets with --res / --obs_csv")

    args = ap.parse_args(argv)
    args._argv = argv                 # the command line as given, for the manifest
    {'build': build, 'dump': dump, 'update': update, 'parrep': parrep, 'init': init, 'set': set_par, 'rescale': rescale,
     'reweight': reweight,
     'validate': validate, 'provenance': provenance, 'log': log, 'bundle': bundle, 'diff': diff,
     'tempchek': tempchek, 'inschek': inschek,
     'hpstart': hpstart}[args.cmd](args)


if __name__ == '__main__':
    main()
