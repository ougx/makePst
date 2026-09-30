"""Pst -> PEST control file text (classic, or PEST++ version 2 with external csv tables)."""
from collections import Counter
import os
import re

import pandas as pd

from .pst import OBS_COLS, PAR_COLS, PRIOR_COLS, Pst, table_lines
from .sections import AUI, COMPUTED, CONTROL, LSQR, REGUL, SVD, SVDA, extra_token, fmt


# These sections are deliberately opaque even though makePst knows where they conventionally
# belong.  All other entries in ``Pst.raw_sections`` are unknown sections read from a file.
PASS_THROUGH_SECTIONS = ('sensitivity reuse', 'derivatives command line', 'predictive analysis', 'pareto')
_PP = re.compile(r'([^\s()]+)\(([^)]*)\)')


def _block(header, lines):
    return header + '\n' + '\n'.join(lines) + '\n'


def _validate_for_write(pst):
    report = pst.validate()
    if report.errors:
        raise ValueError('Pst validation failed: ' + '; '.join(str(f) for f in report.errors))
    pst._check_bounds()                     # written anyway; PEST itself will refuse to start


def _raw_block(pst, name):
    """A pass-through block using its source header and exact body lines when available."""
    header = getattr(pst, 'raw_section_headers', {}).get(name, f'* {name}')
    return _block(header, pst.raw_sections[name])


def _pestpp_text(pst):
    """Render global options once, excluding copies already retained inside opaque blocks."""
    embedded = Counter()
    for lines in pst.raw_sections.values():
        for line in lines:
            if line.strip().startswith('++'):
                embedded.update(_PP.findall(line.strip()[2:]))
    out = []
    for key, value in pst.pestpp:
        pair = (key, value)
        if embedded[pair]:
            embedded[pair] -= 1
        else:
            out.append(f'++{key}({value})\n')
    return ''.join(out)


def _section_name(header):
    """Normalise a generated/source header to the logical section it represents."""
    name = header.strip()[1:].strip().lower()
    if name == 'control data keyword':
        return 'control data'
    if name.endswith(' external'):
        return name[:-len(' external')]
    return name


def _restore_unknown_sections(pst, text):
    """Insert opaque unknown blocks before their next surviving source-order neighbour.

    Known content is regenerated in canonical order.  For each unknown block, its first known
    successor in the input is used as an anchor; if none survives it is placed after the last
    generated section and before global ``++`` options.  This preserves the original position
    without constraining makePst's normal ordering of understood sections.
    """
    unknown = [n for n in pst.raw_sections if n not in PASS_THROUGH_SECTIONS]
    if not unknown:
        return text

    order = list(getattr(pst, 'section_order', []))
    positions = getattr(pst, 'raw_section_positions', {})
    unknown.sort(key=lambda n: positions.get(n, len(order)))

    lines = text.splitlines()
    header_at = {}
    for i, line in enumerate(lines):
        if line.strip().startswith('*'):
            header_at.setdefault(_section_name(line), i)
    generated = set(header_at)

    before = {}
    tail = []
    for name in unknown:
        pos = positions.get(name, order.index(name) if name in order else len(order))
        successor = None
        for candidate in order[pos + 1:]:
            logical = _section_name('* ' + candidate)
            if logical in generated:
                successor = logical
                break
        block = _raw_block(pst, name).splitlines()
        if successor is None:
            tail.extend(block)
        else:
            before.setdefault(successor, []).extend(block)

    out = []
    tail_inserted = False
    for line in lines:
        # Only lines from the generated document are examined here, so a ``++`` retained
        # inside an inserted opaque block cannot be mistaken for the global-options boundary.
        if tail and not tail_inserted and line.strip().startswith('++'):
            out.extend(tail)
            tail_inserted = True
        if line.strip().startswith('*'):
            out.extend(before.pop(_section_name(line), []))
        out.append(line)
    # A source-order anchor can disappear when switching format/mode.  Such blocks are still
    # preserved, at the safe tail location used for sections with no successor.
    for block in before.values():
        tail.extend(block)
    if tail and not tail_inserted:
        out.extend(tail)
    return '\n'.join(out) + '\n'


def _extra(pst, section):
    """Kept unknown control tokens of `section`, as {line number: [token text]}."""
    out = {}
    for name, (sec, n) in getattr(pst, 'control_line', {}).items():
        if sec == section.name and fmt(pst.control.get(name)) != '':
            out.setdefault(n, []).append(extra_token(name, pst.control[name]))
    return out


def _observation_groups(pst):
    return [f'{g} {pst.obs_cov[g]}' if g in pst.obs_cov else g for g in pst.obsgp]


def to_text(pst: Pst, validate=True):
    """Classic (version 1) control file text."""
    if validate:
        _validate_for_write(pst)
    ctl = dict(pst.control)
    ctl.update(pst.counts)

    out = 'pcf\n'
    out += ''.join(f'# {c}\n' for c in pst.comments)
    out += CONTROL.render(ctl, _extra(pst, CONTROL))
    if pst.use_svd:
        out += SVD.render(ctl, _extra(pst, SVD))
    if 'lsqrmode' in ctl:
        out += LSQR.render(ctl, _extra(pst, LSQR))
    if str(ctl.get('doaui', '')).lower() == 'aui':
        aui = dict(ctl)
        aui.setdefault('maxaui', int((pst.par['PARTRANS'].isin(('log', 'none'))).sum()))
        out += AUI.render(aui, _extra(pst, AUI))
    if 'basepestfile' in ctl:
        out += SVDA.render(ctl, _extra(pst, SVDA))
    if 'sensitivity reuse' in pst.raw_sections:
        out += _raw_block(pst, 'sensitivity reuse')

    out += _block('* parameter groups', table_lines(pst.pargp))

    par = pst.par
    out += _block('* parameter data', table_lines(par[PAR_COLS]))
    tied = par['PARTRANS'] == 'tied'
    if tied.any():
        out += '\n'.join(table_lines(par.loc[tied, ['PARNME', 'TIETO']])) + '\n'

    out += _block('* observation groups', _observation_groups(pst))
    out += _block('* observation data', table_lines(pst.obs[OBS_COLS]))
    if 'derivatives command line' in pst.raw_sections:
        out += _raw_block(pst, 'derivatives command line')
    out += _block('* model command line', pst.cmd)
    io = pd.DataFrame(pst.tpl + pst.ins, columns=['pest', 'model'])
    out += _block('* model input/output', table_lines(io))

    if pst.nprior:
        out += _block('* prior information', table_lines(pst.prior[PRIOR_COLS]))
    if 'predictive analysis' in pst.raw_sections:
        out += _raw_block(pst, 'predictive analysis')
    if pst.pestmode == 'regularisation':
        out += REGUL.render(ctl, _extra(pst, REGUL))
    if 'pareto' in pst.raw_sections:
        out += _raw_block(pst, 'pareto')
    out += _pestpp_text(pst)
    return _restore_unknown_sections(pst, out)


# ---------------------------------------------------------------------- version 2
def effective_control(pst):
    """Every control value the classic writer would emit, as keyword -> value (counts excluded)."""
    ctl = dict(pst.control)
    out = {'pestmode': pst.pestmode}
    out.update(CONTROL.values(ctl))
    if pst.use_svd:
        out.update(SVD.values(ctl))
    if 'lsqrmode' in ctl:
        out.update(LSQR.values(ctl))
    if str(ctl.get('doaui', '')).lower() == 'aui':
        out.update(AUI.values(ctl))
    if 'basepestfile' in ctl:
        out.update(SVDA.values(ctl))
    if pst.pestmode == 'regularisation':
        out.update(REGUL.values(ctl))
    out.update({k: ctl[k] for k in getattr(pst, 'control_line', {}) if k in ctl})   # kept unknown tokens
    return {k: v for k, v in out.items() if (k not in COMPUTED or k == 'pestmode') and fmt(v) != ''}


def external_tables(pst: Pst, stem):
    """The csv tables a version-2 file points at: {file name: DataFrame} with PEST++ column names."""
    par = pst.par[PAR_COLS].copy()
    if (pst.par['PARTRANS'] == 'tied').any():
        par['partied'] = pst.par['TIETO'].where(pst.par['PARTRANS'] == 'tied', '')
    tables = {
        f'{stem}.pargp_data.csv': pst.pargp.rename(columns=str.lower),
        f'{stem}.par_data.csv': par.rename(columns=str.lower),
        f'{stem}.obs_data.csv': pst.obs[OBS_COLS].rename(columns=str.lower),
    }
    if pst.nprior:
        prior = pst.prior[PRIOR_COLS].rename(columns={'PINME': 'pilbl', 'EQ': 'equation',
                                                      'WEIGHT': 'weight', 'OBGNME': 'obgnme'})
        tables[f'{stem}.prior_data.csv'] = prior
    return tables


def to_text_v2(pst: Pst, stem, validate=True):
    """PEST++ version-2 text; the tables it references come from external_tables(pst, stem)."""
    if validate:
        _validate_for_write(pst)
    out = 'pcf version=2\n'
    out += ''.join(f'# {c}\n' for c in pst.comments)
    out += '* control data keyword\n'
    for k, v in effective_control(pst).items():
        out += f'{k:<22}{fmt(v)}\n'
    for name in ('sensitivity reuse',):
        if name in pst.raw_sections:
            out += _raw_block(pst, name)
    out += f'* parameter groups external\n{stem}.pargp_data.csv\n'
    out += f'* parameter data external\n{stem}.par_data.csv\n'
    out += _block('* observation groups', _observation_groups(pst))
    out += f'* observation data external\n{stem}.obs_data.csv\n'
    if 'derivatives command line' in pst.raw_sections:
        out += _raw_block(pst, 'derivatives command line')
    out += _block('* model command line', pst.cmd)
    io = pd.DataFrame(pst.tpl + pst.ins, columns=['pest', 'model'])
    out += _block('* model input/output', table_lines(io))
    if pst.nprior:
        out += f'* prior information external\n{stem}.prior_data.csv\n'
    for name in ('predictive analysis', 'pareto'):
        if name in pst.raw_sections:
            out += _raw_block(pst, name)
    out += _pestpp_text(pst)
    return _restore_unknown_sections(pst, out)


def write_pst(pst: Pst, path, dump_tpl=True, version=None, eol=None):
    """Write `pst` to `path`; version 1 (classic) or 2 (PEST++ external tables beside the file).

    `version=None` keeps the version the Pst was read with (1 for a Pst built from tables).
    `eol=None` keeps the line endings it was read with (LF for a Pst built from tables); pass '\\r\\n' or
    '\\n' to force. PEST reads either, but a round trip that flips them stops the result comparing
    byte-for-byte with its parent, and on Windows the model chain is kept CRLF throughout.
    Returns the list of files written.
    """
    version = version or getattr(pst, 'version', 1)
    eol = eol or getattr(pst, 'eol', '\n')
    folder = os.path.dirname(path) or '.'
    written = [path]
    if version == 2:
        stem = os.path.splitext(os.path.basename(path))[0]
        text = to_text_v2(pst, stem)
        for name, df in external_tables(pst, stem).items():
            df.to_csv(os.path.join(folder, name), index=False, lineterminator=eol)
            written.append(os.path.join(folder, name))
    else:
        text = to_text(pst)
    with open(path, 'w', newline='') as f:
        f.write(text.replace('\n', eol) if eol != '\n' else text)
    if dump_tpl:
        write_dump_tpl(pst, os.path.join(folder, 'dump.tpl'), eol=eol)
    print(f'pst written to {path}' + (f' (version 2, {len(written) - 1} csv tables)' if version == 2 else ''))
    return written


def write_dump_tpl(pst: Pst, path, eol=None):
    """A template file that echoes every parameter; handy for checking what PEST sends.

    Endings follow the control file: PEST copies a template's line endings into the model input file it
    writes, and Fortran preprocessors on Windows can mis-parse LF-only records.
    """
    eol = eol or getattr(pst, 'eol', '\n')
    par = pd.DataFrame({'name': pst.par['PARNME'], 'tpl': '~       ' + pst.par['PARNME'] + '       ~'})
    with open(path, 'w', newline='') as f:
        f.write(('ptf ~\n' + '\n'.join(table_lines(par)) + '\n').replace('\n', eol) if eol != '\n'
                else 'ptf ~\n' + '\n'.join(table_lines(par)) + '\n')
