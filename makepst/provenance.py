"""Provenance manifest: which inputs, which command, which version produced an output.

Written as a sidecar `<output>.manifest.json` next to the file a command produces, so a
control file or an updated workbook can be traced back to the exact workbook (by hash),
sheets, result files and command line that made it.
"""
import datetime as _dt
import getpass
import hashlib
import json
import os
import platform
import sys


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def _file_record(path):
    st = os.stat(path)
    return {
        'path': os.path.abspath(path),
        'sha256': sha256(path),
        'size': st.st_size,
        'modified': _dt.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec='seconds'),
    }


def checked_against():
    """The releases makePst's checks were read from: PEST's pestchek (control data, PEST_HP variables) and
    PEST++'s option list. A check is only as current as these; the manifest records them with each output."""
    from .pestpp import PESTPP_REGISTRY_VERSION
    from .sections import PESTCHEK_SOURCE_VERSION
    return {'pestchek': PESTCHEK_SOURCE_VERSION, 'pestpp': PESTPP_REGISTRY_VERSION}


class Manifest:
    def __init__(self, command, argv=None):
        from . import __version__
        import pandas
        import openpyxl
        self.data = {
            'makepst': __version__,
            'command': command,
            'argv': list(sys.argv[1:] if argv is None else argv),
            'created': _dt.datetime.now().astimezone().isoformat(timespec='seconds'),
            'user': _safe(getpass.getuser),
            'host': platform.node(),
            'cwd': os.getcwd(),
            'python': platform.python_version(),
            'pandas': pandas.__version__,
            'openpyxl': openpyxl.__version__,
            'checked_against': checked_against(),
            'platform': platform.platform(),
            'sources': [],
            'output': None,
        }
        try:
            import xlwings
        except ImportError:
            pass
        else:
            self.data['xlwings'] = xlwings.__version__
        self._by_path = {}

    def add_source(self, path, sheet=None, role=None):
        """Record an input file; the same file read several times gets one entry with all its sheets."""
        key = os.path.abspath(path)
        rec = self._by_path.get(key)
        if rec is None:
            rec = _file_record(path)
            if role:
                rec['role'] = role
            self._by_path[key] = rec
            self.data['sources'].append(rec)
        if sheet and sheet not in rec.setdefault('sheets', []):
            rec['sheets'].append(sheet)
        if role and 'role' not in rec:
            rec['role'] = role
        return rec

    def set_output(self, path, **extra):
        self.data['output'] = dict(_file_record(path), **extra)

    def add(self, **fields):
        self.data.update(fields)

    def write(self, path=None):
        """Write next to the output as <output>.manifest.json (or to `path`); returns the path."""
        if path is None:
            path = self.data['output']['path'] + '.manifest.json'
        with open(path, 'w') as f:
            json.dump(self.data, f, indent=2)
        return path


def _safe(fn):
    try:
        return fn()
    except Exception:            # no login name in some CI containers
        return None


def find_manifests(folders, recursive=True):
    """Every *.manifest.json under the folders (a manifest path itself is accepted), sorted by creation time."""
    found = []
    for folder in folders:
        if os.path.isfile(folder):
            found.append(folder)
            continue
        if recursive:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'build', 'dist')]
                found += [os.path.join(root, f) for f in files if f.endswith('.manifest.json')]
        else:
            found += [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith('.manifest.json')]
    rows = []
    for path in found:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and 'command' in data:
            data['_manifest'] = os.path.abspath(path)
            data['_mtime'] = os.stat(path).st_mtime            # tie-breaker: 'created' has one-second resolution
            rows.append(data)
    return sorted(rows, key=lambda d: (d.get('created', ''), d['_mtime']))


def mentions(data, name):
    """Does a manifest's output or any source match `name` (a file name, path, or sha256 prefix)?"""
    name = name.lower()
    records = list(data.get('sources', [])) + ([data['output']] if data.get('output') else [])
    for rec in records:
        path = str(rec.get('path', '')).lower()
        if path.endswith(name) or os.path.basename(path) == os.path.basename(name) or \
                str(rec.get('sha256', '')).startswith(name):
            return True
    return False


def log_line(data, base='.', check=False):
    """One ledger line: created, command, output <- sources, dimensions, version [, hash check]."""
    out = data.get('output') or {}
    counts = ' '.join(f'{out[k]} {k[1:]}' for k in ('npar', 'nobs', 'nprior') if k in out)
    if 'sheets' in out and isinstance(out['sheets'], dict):
        counts = f'{sum(v.get("rows", 0) for v in out["sheets"].values())} rows in {len(out["sheets"])} sheets'
    names = [os.path.basename(s.get('path', '?')) for s in data.get('sources', [])]
    sources = ', '.join(names) if len(names) <= 6 else ', '.join(names[:3]) + f', ... ({len(names)} files)'
    output = _relative(out.get('path', '?'), base)
    created = str(data.get('created', ''))[:16].replace('T', ' ')
    line = f'{created:16s} {data.get("command", "?"):10s} {output} <- {sources or "-"}'
    if counts:
        line += f'  [{counts}]'
    line += f'  makepst {data.get("makepst", "?")}'
    if check:
        states = {s for s, _ in check_manifest(data['_manifest'])}
        line += '  ' + ('unchanged' if states == {'unchanged'} else
                        'missing' if 'missing' in states else 'changed')
    return line


def _relative(path, base):
    try:
        rel = os.path.relpath(path, base)
        return rel if not rel.startswith('..') else path
    except ValueError:                      # different drive on Windows
        return path


def bundle_run(pst, pst_path, out_zip, base_dir='.', sources=False, outputs=False, extra=(), manifest=None):
    """Zip a run's files so it can be reproduced elsewhere: the control file and its manifest, every
    template and instruction file, the model input files the templates write, files named on the
    model command line, optionally the manifest's sources (the workbook, .par ...), the model output
    files, and extra paths. Paths inside the zip are relative to base_dir (files outside it go under
    _external/). Returns (included, missing) as lists of (role, path).
    """
    import zipfile

    wanted = [('control file', os.path.abspath(pst_path))]
    side = os.path.abspath(pst_path) + '.manifest.json'
    if os.path.exists(side):
        wanted.append(('manifest', side))
    rel = lambda p: os.path.abspath(os.path.join(base_dir, p))          # noqa: E731
    for tpl, model_in in pst.tpl:
        wanted.append(('template', rel(tpl)))
        wanted.append(('model input', rel(model_in)))
    for ins, model_out in pst.ins:
        wanted.append(('instruction file', rel(ins)))
        if outputs:
            wanted.append(('model output', rel(model_out)))
    for cmd in pst.cmd:
        for tok in cmd.split():
            if os.path.isfile(rel(tok)) and rel(tok) not in [p for _, p in wanted]:
                wanted.append(('model command', rel(tok)))
    if sources and os.path.exists(side):
        with open(side, encoding='utf-8') as f:
            for rec in json.load(f).get('sources', []):
                wanted.append((f'source: {rec.get("role", "input")}', rec['path']))
    for pattern in extra:
        import glob
        hits = sorted(glob.glob(os.path.join(base_dir, pattern), recursive=True)) or \
            sorted(glob.glob(pattern, recursive=True))
        for h in hits:
            if os.path.isfile(h):
                wanted.append(('extra', os.path.abspath(h)))

    base = os.path.abspath(base_dir)
    included, missing, seen = [], [], set()
    z = zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) if out_zip else None    # None: list only
    try:
        for role, path in wanted:
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            if not os.path.isfile(path):
                missing.append((role, path))
                continue
            arc = _relative(path, base)
            if os.path.isabs(arc):
                arc = os.path.join('_external', os.path.basename(path))
            if z is not None:
                z.write(path, arc.replace(os.sep, '/'))
            included.append((role, path))
            if manifest is not None:
                manifest.add_source(path, role=role)
    finally:
        if z is not None:
            z.close()
    return included, missing


def check_manifest(path):
    """Compare recorded source/output hashes with files at their recorded paths."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    records = list(data.get('sources', []))
    if data.get('output'):
        records.append(data['output'])
    if not records:
        raise ValueError('manifest has no file records')
    result = []
    for rec in records:
        name = rec.get('path')
        expected = rec.get('sha256')
        if not name or not expected:
            status = 'not recorded'
        elif not os.path.isfile(name):
            status = 'missing'
        else:
            status = 'unchanged' if sha256(name) == expected else 'changed'
        result.append((status, name or '(no path)'))
    return result
