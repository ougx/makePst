"""PEST control file text -> Pst.

Reads the classic format and PEST++ version 2 (`pcf version=2`, `* control data keyword`,
`* ... external` sections pointing at csv tables). External tables are resolved relative to
`base_dir` (the control file's folder when read with read_pst).
"""
import os
import re
import warnings

import pandas as pd

from .pst import OBS_COLS, PAR_COLS, PARGP_COLS, PARGP_OPT, PRIOR_COLS, Pst
from .sections import COMPUTED, CONTROL, FIELD_SECTION, SECTIONS, _num

_PP = re.compile(r'([^\s()]+)\(([^)]*)\)')

# sections kept verbatim: read and written back line for line
RAW_SECTIONS = ('sensitivity reuse', 'derivatives command line', 'predictive analysis', 'pareto')

# PEST++ external-table column names -> makepst column names
EXTERNAL_COLUMNS = {
    'parameter groups': {c.lower(): c for c in PARGP_COLS + PARGP_OPT},
    'parameter data': dict({c.lower(): c for c in PAR_COLS}, partied='TIETO'),
    'observation data': {c.lower(): c for c in OBS_COLS},
    'prior information': {'pilbl': 'PINME', 'equation': 'EQ', 'weight': 'WEIGHT', 'obgnme': 'OBGNME'},
    'model input/output': {'pest_file': 'pest', 'model_file': 'model'},
}


def _split_sections(lines):
    """Split text for parsing while retaining an exact record of every section block.

    The parser-facing bodies intentionally keep the historical behaviour (blank lines and
    comments are ignored and trailing whitespace is stripped).  ``raw`` is separate: it keeps
    each original header and every body line exactly, so an unparsed section never loses data.
    """
    comments, sections, pp, raw, order = [], {}, [], {}, []
    current = None
    for lineno, original in enumerate(lines[1:], 2):
        line = original.rstrip()
        s = line.strip()
        if s.startswith('*'):
            current = s[1:].strip().lower()
            sections[current] = []
            raw[current] = {'header': original, 'lines': []}
            order.append(current)
        elif s.startswith('++'):
            pp.append((lineno, s[2:]))
            if current is not None:
                # ``++`` is globally meaningful to PEST++, but it is also physically part of
                # the current opaque block.  Retain it there; the writer suppresses the
                # corresponding generated option so the line is not duplicated.
                raw[current]['lines'].append(original)
        elif not s:
            if current is not None:
                raw[current]['lines'].append(original)
        elif s.startswith('#'):
            if current is None:
                comments.append(s[1:].strip())
            else:
                raw[current]['lines'].append(original)
        elif current is None:
            raise ValueError(f'unexpected text before the first section: {line!r}')
        else:
            sections[current].append(line)
            raw[current]['lines'].append(original)
    return comments, sections, pp, raw, order


def _table(lines, cols):
    rows = [ln.split() for ln in lines]
    df = pd.DataFrame(rows, columns=cols[:max((len(r) for r in rows), default=0)] if rows else cols)
    for c in df.columns:
        num = pd.to_numeric(df[c], errors='coerce')
        if num.notna().all():
            df[c] = num
    return df


def _continued(lines):
    """Join PEST '&' continuation lines."""
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith('&') and out:
            out[-1] += ' ' + s[1:].strip()
        else:
            out.append(s)
    return out


def _external(lines, section, base_dir):
    """The csv tables an `* <section> external` body lists, concatenated, with makepst columns."""
    frames = []
    for ln in lines:
        tokens = ln.split()
        path, opts = tokens[0], dict(t.split('=', 1) for t in tokens[1:] if '=' in t)
        full = path if os.path.isabs(path) else os.path.join(base_dir, path)
        if not os.path.exists(full):
            raise FileNotFoundError(f'* {section} external: {path} not found (looked in {base_dir})')
        df = pd.read_csv(full, sep=opts.get('sep', ','), na_values=opts.get('missing_values'))
        df.columns = [str(c).strip().lower() for c in df.columns]
        mapping = EXTERNAL_COLUMNS[section]
        df = df.rename(columns={c: mapping[c] for c in df.columns if c in mapping})
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _is_template(path, base_dir):
    """A template by extension, or by its first line when the file can be read."""
    low = path.lower()
    if low.endswith('.tpl'):
        return True
    if low.endswith('.ins'):
        return False
    full = path if os.path.isabs(path) else os.path.join(base_dir, path)
    try:
        with open(full, errors='replace') as f:
            return f.readline().split()[:1] in (['ptf'], ['jtf'])
    except OSError:
        return False


def _keyword_control(lines):
    """`* control data keyword` body -> (control values, pest++ options)."""
    ctl, pp = {}, []
    for ln in lines:
        tokens = ln.split('#', 1)[0].split(None, 1)
        if not tokens:
            continue
        key = tokens[0].lower()
        value = tokens[1].strip() if len(tokens) > 1 else ''
        if key in FIELD_SECTION or key == 'pestmode':
            ctl[key] = _num(value) if value else value
        else:
            pp.append((key, value))         # PEST++ options may sit in the keyword section
    return ctl, pp


def from_text(text, base_dir='.'):
    lines = text.splitlines()
    if not lines or not lines[0].strip().lower().startswith('pcf'):
        raise ValueError("not a PEST control file (first line must be 'pcf')")
    version = 2 if 'version=2' in lines[0].replace(' ', '').lower() else 1
    comments, sections, pp, raw, section_order = _split_sections(lines)

    if 'control data keyword' in sections:
        version = 2
        ctl, kw_pp = _keyword_control(sections.pop('control data keyword'))
        pp = [(None, f'{k}({v})') for k, v in kw_pp] + pp
    elif 'control data' in sections:
        ctl = CONTROL.parse(sections.pop('control data'))
    else:
        raise ValueError('no * control data section')

    pst = Pst(ctl.get('pestmode', 'estimation'))
    pst.version = version
    pst.comments = comments
    pst.section_order = section_order
    counts = {k: ctl.get(k) for k in COMPUTED}
    pst.use_svd = 'singular value decomposition' in sections or any(
        k in ctl for k in ('svdmode', 'maxsing', 'eigthresh'))
    for name in list(sections):
        sec = SECTIONS.get(name)
        if sec is not None:
            ctl.update(sec.parse(sections.pop(name)))
    pst.control = {k: v for k, v in ctl.items() if k not in COMPUTED and not k.startswith('_')}

    def take(section):
        """Body of a classic section, or the table of its external variant (None if absent)."""
        if section + ' external' in sections:
            if section in sections:
                raise ValueError(f'both * {section} and * {section} external present')
            return _external(sections.pop(section + ' external'), section, base_dir)
        if section in sections:
            return sections.pop(section)
        return None

    pargp = take('parameter groups')
    if isinstance(pargp, pd.DataFrame):
        pst.pargp = pargp[[c for c in PARGP_COLS + PARGP_OPT if c in pargp]]
    elif pargp is not None:
        pst.pargp = _table(pargp, PARGP_COLS + PARGP_OPT)
    pst.pargp['PARGPNME'] = pst.pargp['PARGPNME'].astype(str).str.strip().str.lower()

    par = take('parameter data')
    if isinstance(par, pd.DataFrame):
        pst.par = par[[c for c in PAR_COLS + ['TIETO'] if c in par]].copy()
    else:
        body = par or []
        par_lines = [ln for ln in body if len(ln.split()) >= len(PAR_COLS)]
        tie_lines = [ln for ln in body if len(ln.split()) == 2]
        pst.par = _table(par_lines, PAR_COLS)
        if tie_lines:
            pst.add_tied(_table(tie_lines, ['PARNME', 'TIETO']))
    for c in ('PARNME', 'PARTRANS', 'PARGP'):
        pst.par[c] = pst.par[c].astype(str).str.strip().str.lower()

    groups = [ln.split() for ln in sections.pop('observation groups', [])]
    pst.obsgp_order = [g[0].lower() for g in groups]
    pst.obs_cov = {g[0].lower(): g[1] for g in groups if len(g) > 1}

    obs = take('observation data')
    if isinstance(obs, pd.DataFrame):
        pst.obs = obs[[c for c in OBS_COLS if c in obs]].copy()
    else:
        pst.obs = _table(obs or [], OBS_COLS)
    pst.obs['OBSNME'] = pst.obs['OBSNME'].astype(str).str.strip().str.lower()
    pst.obs['OBGNME'] = pst.obs['OBGNME'].astype(str).str.strip().str.lower()

    pst.cmd = [ln.strip() for ln in sections.pop('model command line', [])]

    io = take('model input/output')
    if isinstance(io, pd.DataFrame):
        pairs = [(str(a), str(b)) for a, b in zip(io['pest'], io['model'])]
    else:
        pairs = [tuple(ln.split()[:2]) for ln in (io or [])]
    ntpl = counts.get('ntplfle')
    if ntpl is None:                        # keyword format has no counts: tell templates apart
        ntpl = sum(1 for p, _ in pairs if _is_template(p, base_dir))
    pst.tpl, pst.ins = pairs[:ntpl], pairs[ntpl:]

    prior = take('prior information')
    if isinstance(prior, pd.DataFrame):
        pst.prior = prior[PRIOR_COLS].copy()
        for c in ('PINME', 'EQ', 'OBGNME'):
            pst.prior[c] = pst.prior[c].astype(str).str.strip().str.lower()
    else:
        rows = []
        for ln in _continued(prior or []):
            t = ln.split()
            rows.append((t[0].lower(), ' '.join(t[1:-2]).lower(), float(t[-2]), t[-1].lower()))
        pst.prior = pd.DataFrame(rows, columns=PRIOR_COLS)

    for name in RAW_SECTIONS:
        if name in sections:
            sections.pop(name)
            pst.raw_sections[name] = raw[name]['lines']
            pst.raw_section_headers[name] = raw[name]['header']
            pst.raw_section_positions[name] = section_order.index(name)
    for name in list(sections):
        # Unknown sections are opaque, not invalid.  Keeping the exact source block makes a
        # read/write cycle safe even when makePst predates the PEST dialect that introduced it.
        sections.pop(name)
        pst.raw_sections[name] = raw[name]['lines']
        pst.raw_section_headers[name] = raw[name]['header']
        pst.raw_section_positions[name] = section_order.index(name)
        warnings.warn(f'section * {name} not understood; preserved verbatim')

    for lineno, ln in pp:
        for key, value in _PP.findall(ln):
            pst.pestpp.append((key, value))
            where = f'line {lineno}' if lineno else 'control data keyword'
            pst.pestpp_where.setdefault(key.lower(), []).append(where)

    for k, want in (('npar', pst.npar), ('nobs', pst.nobs), ('nprior', pst.nprior)):
        if counts.get(k) not in (None, want):
            warnings.warn(f'{k} says {counts[k]} but {want} rows were read')
    return pst


def read_pst(path):
    # newline='' keeps the file's own line endings visible, so a round trip can write them back: PEST reads
    # either, but a control file that silently changes from CRLF to LF no longer compares byte-for-byte with
    # its parent, and on Windows the model chain is kept CRLF throughout.
    with open(path, newline='') as f:
        raw = f.read()
    pst = from_text(raw.replace('\r\n', '\n'), base_dir=os.path.dirname(os.path.abspath(path)))
    crlf = raw.count('\r\n')
    pst.eol = '\r\n' if crlf and crlf >= raw.count('\n') - crlf else '\n'
    return pst
