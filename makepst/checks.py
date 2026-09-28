"""`makepst validate`: what pestchek would say, before the control file is written.

Checks the tables (duplicates, groups, ties, bounds, prior equations, name lengths), the
files the control file points at (templates, instruction files, model command), and the
names those files cite against the tables. With `outputs=True`, instruction files are also
run against the model output files that exist, with a small interpreter that follows the
PEST manual; it is not PEST, so its findings are warnings.
"""
import os
import re

import pandas as pd

from .model_files import fit_problems, scaled_values
from .pst import ADJUSTABLE, Pst, equation_params, is_number
from .sections import ALL_SECTIONS
from .writer import effective_control

KNOWN_SECTIONS = {s.name for s in ALL_SECTIONS} | {
    'regularization', 'parameter groups', 'parameter data', 'observation groups', 'observation data',
    'model command line', 'model input/output', 'prior information',
    'sensitivity reuse', 'derivatives command line', 'predictive analysis', 'pareto',
    'control data keyword',
}
LIMITS = {'parameter': 12, 'observation': 20, 'group': 12}      # classic PEST; PEST++ allows 200


class Finding:
    __slots__ = ('severity', 'where', 'message')

    def __init__(self, severity, where, message):
        self.severity, self.where, self.message = severity, where, message

    def __str__(self):
        return f'{self.severity.upper():<8}{self.where}: {self.message}'


def _names(items, n=6):
    items = list(items)
    s = ', '.join(str(i) for i in items[:n])
    return s + (f' ... ({len(items)} total)' if len(items) > n else '')


# ---------------------------------------------------------------------- tables
def check_tables(pst: Pst):
    """Internal consistency of the tables; the same rules as Pst.validate(), without changing anything."""
    out = []
    err = lambda where, msg: out.append(Finding('error', where, msg))    # noqa: E731
    warn = lambda where, msg: out.append(Finding('warning', where, msg))  # noqa: E731
    par, obs = pst.par, pst.obs

    if pst.npar == 0:
        err('parameters', 'no parameters')
    if pst.nobs == 0:
        err('observations', 'no observations')
    for what, col in (('parameters', par['PARNME']), ('observations', obs['OBSNME'])):
        dup = col[col.duplicated()].unique()
        if len(dup):
            err(what, f'duplicate names: {_names(dup)}')

    groups = set(pst.pargp['PARGPNME'])
    missing = [g for g in dict.fromkeys(par['PARGP']) if g not in groups]
    if missing:
        err('parameter groups', f'used but not defined: {_names(missing)}')
    unused = [g for g in pst.pargp['PARGPNME'] if g not in set(par['PARGP'])]
    if unused:
        warn('parameter groups', f'defined but unused (removable with normalize): {_names(unused)}')

    trans = par.set_index('PARNME')['PARTRANS'] if not par['PARNME'].duplicated().any() else None
    bad_trans = par.loc[~par['PARTRANS'].isin(('log', 'none', 'fixed', 'tied')), 'PARNME']
    if len(bad_trans):
        err('parameters', f'PARTRANS not log/none/fixed/tied: {_names(bad_trans)}')
    tied = par[par['PARTRANS'] == 'tied']
    if len(tied):
        if 'TIETO' not in par:
            err('parameters', f'{len(tied)} tied parameters but no TIETO column / tied table')
        elif trans is not None:
            target = tied['TIETO'].astype(str).str.strip().str.lower().map(trans)
            gone = tied.loc[target.isna(), 'PARNME']
            if len(gone):
                err('parameters', f'tied to a parameter that does not exist: {_names(gone)}')
            chain = tied.loc[(target == 'tied').values, 'PARNME']
            if len(chain):
                err('parameters', f'tied to a tied parameter: {_names(chain)}')
            to_fix = tied.loc[(target == 'fixed').values, 'PARNME']
            if len(to_fix):
                warn('parameters', f'tied to a fixed parameter (become fixed on write): {_names(to_fix)}')

    v, lb, ub = (pd.to_numeric(par[c], errors='coerce') for c in ('PARVAL1', 'PARLBND', 'PARUBND'))
    nonnum = par.loc[v.isna() | lb.isna() | ub.isna(), 'PARNME']
    if len(nonnum):
        err('parameters', f'non-numeric PARVAL1 / PARLBND / PARUBND: {_names(nonnum)}')
    adj = par['PARTRANS'].isin(ADJUSTABLE)
    inverted = par.loc[(lb > ub).fillna(False), 'PARNME']
    if len(inverted):
        err('parameters', f'PARLBND above PARUBND: {_names(inverted)}')
    outside = par.loc[(adj & ((v < lb) | (v > ub))).fillna(False), 'PARNME']
    if len(outside):
        err('parameters', f'adjustable PARVAL1 outside bounds: {_names(outside)}')
    nonpos = par.loc[(adj & (par['PARTRANS'] == 'log') & (lb <= 0)).fillna(False), 'PARNME']
    if len(nonpos):
        err('parameters', f'log-transformed with a non-positive lower bound: {_names(nonpos)}')

    w = pd.to_numeric(obs['WEIGHT'], errors='coerce')
    ov = pd.to_numeric(obs['OBSVAL'], errors='coerce')
    bad = obs.loc[w.isna() | ov.isna(), 'OBSNME']
    if len(bad):
        err('observations', f'non-numeric OBSVAL / WEIGHT: {_names(bad)}')
    neg = obs.loc[(w < 0).fillna(False), 'OBSNME']
    if len(neg):
        err('observations', f'negative weight: {_names(neg)}')
    if len(obs) and (w.fillna(0) <= 0).all():
        warn('observations', 'every observation has zero weight')

    if pst.nprior:
        adjustable = set(par.loc[adj, 'PARNME'])
        for r in pst.prior.itertuples():
            eq = str(r.EQ)
            refs = equation_params(eq)
            if '=' not in eq or not refs:
                err(f'prior {r.PINME}', f'malformed equation: {eq!r}')
                continue
            rhs = eq.split('=', 1)[1].strip()
            if not is_number(rhs):
                err(f'prior {r.PINME}', f'right-hand side is not a number: {rhs!r}')
            gone = [p for p in refs if p not in set(par['PARNME'])]
            if gone:
                err(f'prior {r.PINME}', f'references unknown parameters: {_names(gone)}')
            else:
                notadj = [p for p in refs if p not in adjustable]
                if notadj:
                    warn(f'prior {r.PINME}', f'references fixed/tied parameters (removable with normalize): {_names(notadj)}')
                logs = set(re.findall(r'log\((\w+)\)', eq.lower()))
                wrong = [p for p in refs if (trans is not None) and ((trans.get(p) == 'log') != (p in logs))]
                if wrong:
                    err(f'prior {r.PINME}', f'log() use does not match PARTRANS: {_names(wrong)}')
        dup = pst.prior['PINME'][pst.prior['PINME'].duplicated()].unique()
        if len(dup):
            err('prior information', f'duplicate labels: {_names(dup)}')

    for what, names, limit in (('parameter', par['PARNME'], LIMITS['parameter']),
                               ('observation', obs['OBSNME'], LIMITS['observation']),
                               ('group', pd.concat([pst.pargp['PARGPNME'], obs['OBGNME']]), LIMITS['group'])):
        long = names[names.astype(str).str.len() > limit].unique()
        if len(long):
            warn(f'{what} names', f'longer than {limit} characters (PEST limit; PEST++ allows 200): {_names(long)}')
    if 'DERCOM' in par:
        ncom = max(len(pst.cmd), 1)
        badcom = par.loc[(pd.to_numeric(par['DERCOM'], errors='coerce') > ncom).fillna(False), 'PARNME']
        if len(badcom):
            err('parameters', f'DERCOM exceeds the number of model command lines ({ncom}): {_names(badcom)}')
    if not pst.tpl:
        err('model input/output', 'no template files')
    if not pst.ins:
        err('model input/output', 'no instruction files')
    if not pst.cmd:
        err('model command line', 'no model command line')
    return out


# ---------------------------------------------------------------------- templates / instructions
def _first_line(path):
    with open(path, errors='replace') as f:
        return f.readline().rstrip('\r\n')


def template_names(path):
    """Parameter names cited in a template file (lower-case), in order of appearance."""
    with open(path, errors='replace') as f:
        head = f.readline().split()
        body = f.read()
    if len(head) != 2 or head[0].lower() not in ('ptf', 'jtf'):
        raise ValueError(f'first line must be "ptf <delimiter>", got {" ".join(head)!r}')
    d = re.escape(head[1])
    return [m.strip().lower() for m in re.findall(f'{d}(.*?){d}', body)]


def template_problems(path):
    """pestchek's template checks beyond the names: spaces narrower than 3 characters, tabs inside them."""
    with open(path, errors='replace') as f:
        head = f.readline().split()
        body = f.read()
    if len(head) != 2:
        return []
    d = re.escape(head[1])
    out = []
    for m in re.finditer(f'{d}(.*?){d}', body):
        space = m.group(0)
        if '\t' in space:
            out.append(f'tab character inside the parameter space for {m.group(1).strip()!r}')
        elif len(space) < 3:
            out.append(f'parameter space for {m.group(1).strip()!r} is narrower than 3 characters')
    return sorted(set(out))


_INS_NAME = re.compile(r'!([^!\s]+)!|\[([^\]\s]+)\]|\(([^)\s]+)\)')


def instruction_problems(path):
    """pestchek's static instruction-file checks (syntax only; nothing is read from the model output)."""
    with open(path, errors='replace') as f:
        head = f.readline().split()
        raw = f.read().splitlines()
    if len(head) != 2 or head[0].lower() not in ('pif', 'jif'):
        return ['first line must be "pif" or "jif" followed by the marker delimiter']
    d = head[1]
    if len(d) != 1 or d.isalnum() or d in '!&[]():':
        return [f'illegal marker delimiter {d!r}']
    tok_re = re.compile(re.escape(d) + '[^' + re.escape(d) + ']*' + re.escape(d) + r'|\S+')
    out = []
    first = True
    col = 0                        # rightmost column pinned by t / [ ] / ( ) on the current output line
    for li, ln in enumerate(raw, 2):
        s = ln.strip()
        if not s:
            continue
        continued = s.startswith('&')
        if continued:
            if first:
                out.append(f'line {li}: the first instruction line cannot begin with the continuation character "&"')
            s = s[1:].strip()
        else:
            if s.count(d) % 2:
                out.append(f'line {li}: marker delimiter {d!r} not closed')
            lead = s.split()[0].lower()
            if not (lead.startswith(d) or re.fullmatch(r'l\d+', lead)):
                out.append(f'line {li}: an instruction line must begin with "l", a marker or "&"')
            col = 0
        first = False
        for ti, t in enumerate(tok_re.findall(s)):
            low = t.lower()
            if t.startswith(d):
                if len(t) < 3:
                    out.append(f'line {li}: marker has zero length')
                elif '\t' in t:
                    out.append(f'line {li}: tab character inside marker {t}')
            elif re.fullmatch(r'l\d+', low):
                if int(low[1:]) <= 0:
                    out.append(f'line {li}: the integer after "l" must be positive')
                if ti > 0 or continued:
                    out.append(f'line {li}: a line advance ({t}) can only occur at the beginning of an instruction line')
            elif re.fullmatch(r't\d+', low):
                if int(low[1:]) <= 0:
                    out.append(f'line {li}: the integer after "t" must be positive')
                elif int(low[1:]) < col:
                    out.append(f'line {li}: {t} moves backwards; a model output line must be read from left to right')
                else:
                    col = int(low[1:])
            elif low == 'w':
                pass
            elif t.startswith('!'):
                if not t.endswith('!') or t.count('!') != 2 or len(t) < 3:
                    out.append(f'line {li}: "!" not balanced in {t}')
                elif len(t) - 2 > 20:
                    out.append(f'line {li}: observation name longer than 20 characters in {t}')
            elif t[0] in '[(':
                m = re.fullmatch(r'[\[(]([^\])]*)[\])](\d+):(\d+)', t)
                if not m:
                    out.append(f'line {li}: fixed / semi-fixed instruction must be [name]n1:n2 or (name)n1:n2, got {t}')
                elif not m.group(1):
                    out.append(f'line {li}: missing observation name in {t}')
                elif m.group(1).lower() == 'dum':
                    out.append(f'line {li}: "dum" is only allowed for non-fixed (!dum!) observations')
                elif int(m.group(2)) == 0 or int(m.group(3)) < int(m.group(2)):
                    out.append(f'line {li}: columns n1:n2 must be positive and increasing in {t}')
                elif len(m.group(1)) > 20:
                    out.append(f'line {li}: observation name longer than 20 characters in {t}')
                elif int(m.group(2)) < col:
                    out.append(f'line {li}: {t} starts left of column {col}; a model output line must be read from left to right')
                else:
                    col = int(m.group(3))
            elif low.startswith('&'):
                out.append(f'line {li}: "&" is only allowed at the beginning of a line')
            else:
                out.append(f'line {li}: illegal instruction {t!r}')
    return out


def instruction_names(path):
    """Observation names cited in an instruction file (lower-case, 'dum' excluded), in order."""
    with open(path, errors='replace') as f:
        head = f.readline().split()
        body = f.read()
    if len(head) != 2 or head[0].lower() not in ('pif', 'jif'):
        raise ValueError(f'first line must be "pif <delimiter>", got {" ".join(head)!r}')
    d = re.escape(head[1])
    body = re.sub(f'{d}[^{d}]*{d}', ' ', body)          # markers may contain ! [ ( characters
    names = [next(g for g in m.groups() if g) for m in _INS_NAME.finditer(body)]
    return [n.lower() for n in names if n.lower() != 'dum']


def check_files(pst: Pst, base_dir='.', outputs=False):
    """Files the control file points at, and the names they cite."""
    out = []
    err = lambda where, msg: out.append(Finding('error', where, msg))    # noqa: E731
    warn = lambda where, msg: out.append(Finding('warning', where, msg))  # noqa: E731
    info = lambda where, msg: out.append(Finding('info', where, msg))     # noqa: E731
    rel = lambda p: os.path.join(base_dir, p)                              # noqa: E731

    par_names = set(pst.par['PARNME'])
    ctl = effective_control(pst)
    precis, dpoint = ctl.get('precis', 'single'), ctl.get('dpoint', 'point')
    values = scaled_values(pst.par)
    cited = {}
    for tpl, model_in in pst.tpl:
        if not os.path.exists(rel(tpl)):
            err(tpl, 'template file not found')
            continue
        try:
            names = template_names(rel(tpl))
        except ValueError as e:
            err(tpl, str(e))
            continue
        if not names:
            warn(tpl, 'cites no parameters')
        problems = template_problems(rel(tpl))
        for problem in problems:
            err(tpl, problem)
        if not problems:                            # as TEMPCHEK would write it: does every value fit its space?
            for problem in fit_problems(rel(tpl), values, precis, dpoint):
                err(tpl, f'value cannot be written into its space: {problem}')
        unknown = sorted(set(names) - par_names)
        if unknown:
            err(tpl, f'cites parameters not in the control file: {_names(unknown)}')
        for n in names:
            cited.setdefault(n, set()).add(tpl)
        folder = os.path.dirname(rel(model_in)) if model_in else ''
        if folder and not os.path.isdir(folder):
            warn(tpl, f'folder of model input file {model_in} does not exist')
    if cited:                                       # only meaningful once a template was read
        missing = [n for n in pst.par['PARNME'] if n not in cited]
        if missing:
            err('templates', f'parameters cited in no template file: {_names(missing)}')

    obs_names = set(pst.obs['OBSNME'])
    seen = {}
    for ins, model_out in pst.ins:
        if not os.path.exists(rel(ins)):
            err(ins, 'instruction file not found')
            continue
        try:
            names = instruction_names(rel(ins))
        except ValueError as e:
            err(ins, str(e))
            continue
        if not names:
            warn(ins, 'reads no observations')
        for problem in instruction_problems(rel(ins)):
            err(ins, problem)
        unknown = sorted(set(names) - obs_names)
        if unknown:
            err(ins, f'reads observations not in the control file: {_names(unknown)}')
        counts = pd.Series(names).value_counts()
        dup_here = sorted(counts[counts > 1].index)
        if dup_here:
            err(ins, f'observation read more than once: {_names(dup_here)}')
        for n in set(names):
            seen.setdefault(n, []).append(ins)
        if outputs:
            if not os.path.exists(rel(model_out)):
                info(ins, f'model output {model_out} not present; instructions not run')
            else:
                try:
                    values = run_instructions(rel(ins), rel(model_out))
                    info(ins, f'read {len(values)} of {len(set(names))} observations from {model_out}')
                except InstructionError as e:
                    warn(ins, f'reading {model_out} failed: {e}')
    if seen:
        dup_across = sorted(n for n, files in seen.items() if len(files) > 1)
        if dup_across:
            err('instructions', f'observation read by more than one instruction file: {_names(dup_across)}')
        missing = [n for n in pst.obs['OBSNME'] if n not in seen]
        if missing:
            err('instructions', f'observations read by no instruction file: {_names(missing)}')

    for cmd in pst.cmd:
        # only judge tokens that are clearly file paths; 'python model.py' is checked on model.py
        for tok in cmd.split()[:2]:
            looks_like_file = ('/' in tok or '\\' in tok or tok.lower().endswith(('.bat', '.exe', '.py', '.sh')))
            if looks_like_file and not os.path.exists(rel(tok)):
                warn('model command line', f'{tok} not found in {os.path.abspath(base_dir)}')
    return out


def check_sections(pst_path):
    """Sections makePst cannot interpret but will safely carry through a round trip."""
    out = []
    with open(pst_path, errors='replace') as f:
        for line in f:
            s = line.strip()
            if s.startswith('*'):
                name = s[1:].strip().lower()
                base = name[:-len(' external')] if name.endswith(' external') else name
                if base not in KNOWN_SECTIONS:
                    out.append(Finding('info', 'sections', f'unknown section preserved verbatim: * {name}'))
    return out


# ---------------------------------------------------------------------- instruction interpreter
class InstructionError(Exception):
    pass


def run_instructions(ins_path, out_path):
    """Read a model output file with an instruction file; returns {observation: value}.

    Follows the PEST manual: primary/secondary markers, l, w, t, !name!, [name]c1:c2,
    (name)c1:c2, dum, & continuation. Raises InstructionError where PEST would stop.
    """
    with open(ins_path, errors='replace') as f:
        head = f.readline().split()
        raw = f.read().splitlines()
    if len(head) != 2:
        raise InstructionError('bad pif line')
    d = head[1]
    lines = []
    for ln in raw:
        if ln.strip().startswith('&') and lines:
            lines[-1] += ' ' + ln.strip()[1:]
        elif ln.strip():
            lines.append(ln.strip())
    with open(out_path, errors='replace') as f:
        output = f.read().splitlines()

    tok_re = re.compile(re.escape(d) + '[^' + re.escape(d) + ']*' + re.escape(d) + r'|\S+')
    values = {}
    row, col = -1, 0                       # row: index into output (-1 = before the first line)

    def cur():
        if row < 0 or row >= len(output):
            raise InstructionError(f'instruction line {li + 1}: beyond end of {os.path.basename(out_path)}')
        return output[row]

    for li, ln in enumerate(lines):
        toks = tok_re.findall(ln)
        for ti, t in enumerate(toks):
            low = t.lower()
            if t.startswith(d) and t.endswith(d):
                marker = t[1:-1]
                if ti == 0:                # primary marker: search forward
                    r = row + 1
                    while r < len(output) and marker not in output[r]:
                        r += 1
                    if r >= len(output):
                        raise InstructionError(f'instruction line {li + 1}: primary marker {marker!r} not found')
                    row, col = r, output[r].index(marker) + len(marker)
                else:                      # secondary marker: search in the current line
                    line = cur()
                    i = line.find(marker, col)
                    if i < 0:
                        raise InstructionError(f'instruction line {li + 1}: secondary marker {marker!r} not found')
                    col = i + len(marker)
            elif low[0] == 'l' and low[1:].isdigit():
                row += int(low[1:])
                col = 0
                cur()
            elif low == 'w':
                line = cur()
                i = col
                while i < len(line) and not line[i].isspace():
                    i += 1
                if i >= len(line):
                    raise InstructionError(f'instruction line {li + 1}: no whitespace after column {col + 1}')
                while i < len(line) and line[i].isspace():
                    i += 1
                col = i
            elif low[0] == 't' and low[1:].isdigit():
                col = int(low[1:]) - 1
            elif t.startswith('!') and t.endswith('!'):
                line = cur()
                m = re.compile(r'\S+').search(line, col)
                if not m:
                    raise InstructionError(f'instruction line {li + 1}: nothing to read for {t}')
                _store(values, t[1:-1], m.group(), li)
                col = m.end()
            elif t.startswith('[') or t.startswith('('):
                m = re.match(r'[\[(]([^\])]+)[\])](\d+):(\d+)$', t)
                if not m:
                    raise InstructionError(f'instruction line {li + 1}: bad instruction {t!r}')
                name, c1, c2 = m.group(1), int(m.group(2)) - 1, int(m.group(3))
                line = cur()
                if t.startswith('['):
                    _store(values, name, line[c1:c2], li)
                    col = c2
                else:
                    seg = re.compile(r'\S+')
                    mm = seg.search(line, c1)
                    if not mm or mm.start() >= c2:
                        raise InstructionError(f'instruction line {li + 1}: nothing in columns {c1 + 1}-{c2} for {name}')
                    start = mm.start()
                    while start > 0 and not line[start - 1].isspace():   # token may begin before c1
                        start -= 1
                    end = seg.search(line, start).end()
                    _store(values, name, line[start:end], li)
                    col = end
            else:
                raise InstructionError(f'instruction line {li + 1}: unknown instruction {t!r}')
    return values


def _store(values, name, text, li):
    name = name.lower()
    if name == 'dum':
        return
    try:
        values[name] = float(text)
    except ValueError:
        raise InstructionError(f'instruction line {li + 1}: {name} read {text.strip()!r}, not a number') from None


# ---------------------------------------------------------------------- driver
def validate(pst: Pst, base_dir='.', pst_path=None, outputs=False):
    from .pestpp import check_pestpp
    from .rules import check_extra
    # `++` findings are reported here but kept out of Pst.validate(), which gates writing: plain PEST ignores
    # `++` lines, and an option judged against this registry must not stop a control file being written.
    findings = check_tables(pst) + check_extra(pst) + check_pestpp(pst)
    if pst_path:
        findings += check_sections(pst_path)
    findings += check_files(pst, base_dir, outputs)
    return findings


def summary(findings):
    n = {s: sum(1 for f in findings if f.severity == s) for s in ('error', 'warning', 'info')}
    return n, f"{n['error']} errors, {n['warning']} warnings, {n['info']} notes"
