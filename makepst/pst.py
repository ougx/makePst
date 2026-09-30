"""The Pst data model: everything a PEST control file holds, as tables.

All routes go through this class: Excel -> Pst -> .pst, .pst -> Pst -> Excel,
.par -> Pst -> Excel. `validate()` audits consistency without changing the tables;
`normalize()` applies the fixes that the report makes available.
"""
import fnmatch
import os
import re
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .sections import COMPUTED, FIELD_SECTION, SECTIONS, _num, fmt, is_number, normalize_mode

PAR_COLS = 'PARNME PARTRANS PARCHGLIM PARVAL1 PARLBND PARUBND PARGP SCALE OFFSET DERCOM'.split()
PAR_EXTRA = ['TIETO', 'PRIOR', 'WEIGHT']          # optional columns carried on self.par
PARGP_COLS = 'PARGPNME INCTYP DERINC DERINCLB FORCEN DERINCMUL DERMTHD'.split()
PARGP_OPT = 'SPLITTHRESH SPLITRELDIFF SPLITACTION'.split()
OBS_COLS = 'OBSNME OBSVAL WEIGHT OBGNME'.split()
PRIOR_COLS = 'PINME EQ WEIGHT OBGNME'.split()

ADJUSTABLE = ('log', 'none')
# operators and whitespace; a sign directly after an exponent (1e-3, 2.5E+02) is part of the number
_EQ_SPLIT = re.compile(r'[\s()*=/]+|(?<![0-9][eE])[-+]')


def _clean(s):
    return s.astype(str).str.strip().str.lower()


def _control_place(text):
    """('control data', 4) from a CONTROL sheet's LINE cell such as 'control data line 4', else None."""
    m = re.fullmatch(r'\*?\s*(.*?)[\s,]+(?:line\s*)?(\d+)', str(text or '').strip().lower())
    if m and m.group(1) in SECTIONS and int(m.group(2)) > 0:
        return SECTIONS[m.group(1)].name, int(m.group(2))
    return None


def _is_storage(names):
    return names.str.startswith('ss') | names.str.startswith('sy')


def _empty(cols):
    return pd.DataFrame(columns=cols)


def _append(old, new):
    """concat that keeps `new`'s dtypes when `old` is still the empty placeholder."""
    if len(old) == 0:
        return new.reset_index(drop=True)
    return pd.concat([old, new], ignore_index=True)


def equation_params(eq):
    """Parameter names referenced by a prior-information equation."""
    return [t for t in _EQ_SPLIT.split(str(eq).lower()) if t and t != 'log' and not is_number(t)]


# a whole left-hand side of the form  [coef *] name  or  [coef *] log(name)  - a preferred value, not a relationship
_ONE_TERM = re.compile(r'^\s*(?:([-+]?[\d.]+(?:[eE][-+]?\d+)?)\s*\*\s*)?(log\s*\(\s*)?([A-Za-z_]\w*)\s*\)?\s*$')


def _single_term(eq):
    """(coefficient, parameter, is_log) for a single-parameter prior equation, else None.

    Multi-parameter equations (preferred difference, homogeneity) state a relationship rather than a value,
    so nothing can be derived for them from one parameter's value.
    """
    lhs, sep, _ = str(eq).partition('=')
    if not sep:
        return None
    m = _ONE_TERM.match(lhs)
    if not m or (m.group(2) is not None) != (lhs.count('(') == 1):
        return None
    return float(m.group(1) or 1.0), m.group(3).lower(), m.group(2) is not None


# one term of a prior-equation left-hand side:  [+|-] [coef *] name  or  [+|-] [coef *] log(name)
_TERM = re.compile(r'\s*([-+])?\s*(?:((?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?)\s*\*\s*)?'
                   r'(?:(log)\s*\(\s*([^\s()*=+/-]+)\s*\)|([^\s()*=+/-]+))\s*', re.I)


def _terms(lhs):
    """[(sign, coefficient text, name, is_log)] for a prior-equation left-hand side, or None if it does not parse."""
    pos, out = 0, []
    while pos < len(lhs):
        m = _TERM.match(lhs, pos)
        if not m or m.end() == pos or (out and m.group(1) is None):   # terms are joined by + or -
            return None
        sign, coef, is_log, log_name, name = m.groups()
        out.append((-1.0 if sign == '-' else 1.0, coef, log_name or name, is_log is not None))
        pos = m.end()
    return out or None


def _rescale_equation(eq, factors):
    """`eq` rewritten for parameters rescaled as p = f * p' (`factors` maps lower-case names to f).

    Linear terms take the factor into their coefficient; a log term splits into log(p') + log10(f), the
    constant moving to the right-hand side. Either way the equation's residual is the same number as before,
    so its weight still means what it did. Returns None when no rescaled parameter appears in it.
    """
    lhs, sep, rhs = str(eq).partition('=')
    terms = _terms(lhs) if sep else None
    if terms is None or not is_number(rhs.strip()):
        raise ValueError(f'cannot parse prior equation {eq!r}')
    if not any(name.lower() in factors for _, _, name, _ in terms):
        return None
    shift, size, parts = 0.0, 0.0, []
    for sign, coef, name, is_log in terms:
        c = sign * (float(coef.lower().replace('d', 'e')) if coef else 1.0)
        f = factors.get(name.lower())
        if f is not None and is_log:
            shift += c * np.log10(f)
            size += abs(c * np.log10(f))
        elif f is not None:
            c, coef = c * f, None
        negative = c < 0 or (c == 0 and sign < 0)
        if not coef:
            coef = fmt(abs(c))
            coef += '' if re.search(r'[.eE]', coef) else '.0'      # 1.0, the way equations are written
        text = f"{coef} * {f'log({name})' if is_log else name}"
        parts.append(('-' if negative else '') + text if not parts else f"{'-' if negative else '+'} {text}")
    rhs = rhs.strip()
    if shift != 0:
        old = float(rhs.lower().replace('d', 'e'))
        new = old - shift
        # log10 round-off after a rescale and its undo, not a real preferred value
        rhs = fmt(0.0 if abs(new) < 1e-9 * (abs(old) + size) else new)
    return f"{' '.join(parts)} = {rhs}"


def _to_internal(vals, par):
    """(rows of `par` found in `vals`, their PARVAL1, how many were converted) for read_par's frame `vals`.

    A .par file records the SCALE and OFFSET it was written with. Where those differ from the control file's
    (a run before `rescale`, say), the value is carried over through the model value PARVAL1 * SCALE + OFFSET,
    which is what the model saw; copying PARVAL1 across would be off by the ratio of the scales.
    """
    num = lambda s: pd.to_numeric(s, errors='coerce')                # noqa: E731
    names = par['PARNME'].astype(str).str.lower()
    hit = names.isin(vals.index)
    value = num(names[hit].map(vals['PARVAL1']))
    if not {'SCALE', 'OFFSET'} <= set(vals.columns):
        return hit, value, 0                                        # an IES ensemble carries no scales
    fs, fo = num(names[hit].map(vals['SCALE'])), num(names[hit].map(vals['OFFSET']))
    s = num(par.loc[hit, 'SCALE']).fillna(1.0) if 'SCALE' in par else pd.Series(1.0, index=value.index)
    o = num(par.loc[hit, 'OFFSET']).fillna(0.0) if 'OFFSET' in par else pd.Series(0.0, index=value.index)
    same = np.isclose(fs, s, rtol=1e-6, atol=0) & np.isclose(fo, o, rtol=1e-6, atol=1e-12 * s.abs())
    convert = ~same & fs.notna() & fo.notna() & (s != 0)
    value[convert] = ((value * fs + fo - o) / s)[convert]
    return hit, value, int(convert.sum())


# PEST++ options naming files whose parameter values or sensitivities are in control-file units
_PARAMETER_UNIT_FILES = ('parcov', 'base_jacobian', 'ies_par_en', 'ies_restart_parameter_ensemble',
                         'sweep_parameter_csv_file')


@dataclass(frozen=True)
class ValidationFix:
    """A safe, named normalization available for a Pst validation report."""
    kind: str
    description: str
    items: tuple = ()

    def __str__(self):
        return self.description


@dataclass
class ValidationReport:
    """Non-mutating result from :meth:`Pst.validate`."""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    fixes_available: list = field(default_factory=list)
    _owner_id: int = field(default=0, repr=False, compare=False)

    @property
    def ok(self):
        return not self.errors

    def __bool__(self):
        return self.ok


class Pst:
    def __init__(self, pestmode='estimation', ss=False):
        self.pestmode = normalize_mode(pestmode)
        self.ss = ss                    # steady state: drop storage (ss*/sy*) parameters and groups
        self.comments = []              # '# ...' lines written after 'pcf'
        self.control = {}               # user values for every control-style section, flat
        self.control_line = {}          # control tokens the schema does not know: name -> (section, line number)
        self.use_svd = True
        self.par = _empty(PAR_COLS)
        self.pargp = _empty(PARGP_COLS)
        self.obs = _empty(OBS_COLS)
        self.prior = _empty(PRIOR_COLS)
        self.tpl = []                   # (template file, model input file)
        self.ins = []                   # (instruction file, model output file)
        self.cmd = []                   # model command lines
        self.pestpp = []                # (key, value) for ++key(value) lines
        self.pestpp_where = {}          # option name (lower case) -> where each occurrence was read, for messages
        # Unparsed section bodies, keyed by their normalised section name.  This includes the
        # four long-standing pass-through sections and any section unknown to this version of
        # makePst.  The companion metadata lets the writer reproduce an unknown block's exact
        # header/body lines at the same point in the document.
        self.raw_sections = {}
        self.raw_section_headers = {}
        self.raw_section_positions = {}
        self.section_order = []
        self.obsgp_order = []           # preferred observation-group order (from a read pst)
        self.obs_cov = {}               # observation group -> covariance matrix file
        self.version = 1                # control file format: 1 classic, 2 PEST++ external tables
        self.eol = '\n'                 # line endings to write back; read_pst sets it from the file
        self._auto_labels = set()       # prior labels generated by normalize() from PRIOR/WEIGHT

    # ------------------------------------------------------------------ counts
    @property
    def npar(self):
        return len(self.par)

    @property
    def nobs(self):
        return len(self.obs)

    @property
    def npargp(self):
        return len(self.pargp)

    @property
    def nprior(self):
        return len(self.prior)

    @property
    def obsgp(self):
        used = list(dict.fromkeys(self.obs['OBGNME']))
        if self.nprior:
            used += [g for g in dict.fromkeys(self.prior['OBGNME']) if g not in used]
        return [g for g in self.obsgp_order if g in used] + [g for g in used if g not in self.obsgp_order]

    @property
    def nobsgp(self):
        return len(self.obsgp)

    @property
    def counts(self):
        return {'npar': self.npar, 'nobs': self.nobs, 'npargp': self.npargp, 'nprior': self.nprior,
                'nobsgp': self.nobsgp, 'ntplfle': len(self.tpl), 'ninsfle': len(self.ins),
                'pestmode': self.pestmode}

    # ------------------------------------------------------------------ control
    def set_control(self, values):
        """Set control-style values from a dict or a CONTROL sheet (NAME/VALUE columns)."""
        lines = {}
        if isinstance(values, pd.DataFrame):
            df = values.iloc[:, :4].copy()
            df.columns = ['LINE', 'NAME', 'DEFAULT', 'VALUE'][:df.shape[1]]
            rows = df.dropna(subset=['NAME', 'VALUE']).itertuples()
            values = {}
            for r in rows:
                name = str(r.NAME).strip().lower()
                values[name] = r.VALUE
                lines[name] = getattr(r, 'LINE', None)
        for k, v in values.items():
            k = str(k).strip().lower()
            if k == 'pestmode':
                self.pestmode = normalize_mode(v)
            elif k in COMPUTED:
                warnings.warn(f'control value {k}={v!r} ignored; it is computed from the tables')
            elif k not in FIELD_SECTION:
                # A token this makePst does not know (a newer PEST_HP variable, say) is kept, like an unknown
                # `++` option - but PEST reads it only on its own line, so it needs one: a control file gives
                # it, a CONTROL sheet says it in the LINE column ('control data line 4').
                place = _control_place(lines.get(k)) or self.control_line.get(k)
                if place is None:
                    warnings.warn(f'unknown control variable {k!r} ignored: give the line it belongs on, '
                                  f"as 'control data line 4' in the CONTROL sheet's LINE column")
                elif fmt(v) != '':
                    self.control[k] = _num(v) if isinstance(v, str) else v
                    self.control_line[k] = place
            elif fmt(v) != '':
                self.control[k] = _num(v) if isinstance(v, str) else v   # csv gives strings

    # ------------------------------------------------------------------ tables
    def add_pargp(self, df):
        df = df.dropna(subset=PARGP_COLS).copy()
        df['PARGPNME'] = _clean(df['PARGPNME'])
        if self.ss:
            df = df[~_is_storage(df['PARGPNME'])]
        cols = PARGP_COLS + [c for c in PARGP_OPT if c in df and df[c].notna().all()]
        self.pargp = _append(self.pargp, df[cols]).drop_duplicates('PARGPNME')

    def add_par(self, df):
        df = df.dropna(subset=PAR_COLS).copy()
        for c in ('PARNME', 'PARTRANS', 'PARGP'):
            df[c] = _clean(df[c])
        if self.ss:
            df = df[~_is_storage(df['PARNME'])]
        cols = PAR_COLS + [c for c in PAR_EXTRA if c in df]
        self.par = _append(self.par, df[cols])

    def add_tied(self, df):
        """Two columns: tied parameter name, parameter it is tied to.

        Listed parameters become PARTRANS 'tied'; a row tying a parameter to itself is ignored.
        """
        tied = df.iloc[:, :2].dropna()
        names, targets = _clean(tied.iloc[:, 0]), _clean(tied.iloc[:, 1])
        lookup = {n: t for n, t in zip(names, targets) if n != t}
        if 'TIETO' not in self.par:
            self.par['TIETO'] = pd.Series(None, index=self.par.index, dtype=object)
        hit = self.par['PARNME'].isin(lookup)
        missing = set(lookup) - set(self.par.loc[hit, 'PARNME'])
        if missing:
            warnings.warn(f'tied table names parameters that were not added: {sorted(missing)[:5]}')
        self.par.loc[hit, 'TIETO'] = self.par.loc[hit, 'PARNME'].map(lookup)
        self.par.loc[hit, 'PARTRANS'] = 'tied'

    def add_obs(self, df, weight_factor=1.0):
        df = df.dropna(subset=OBS_COLS).copy()
        df['OBSNME'] = _clean(df['OBSNME'])
        df['OBGNME'] = _clean(df['OBGNME'])
        df['WEIGHT'] = df['WEIGHT'] * weight_factor
        self.obs = _append(self.obs, df[OBS_COLS])

    def add_prior(self, df):
        """Explicit prior information (PINME, EQ, WEIGHT, OBGNME); any rows switch to regularisation mode."""
        df = df.dropna(subset=PRIOR_COLS).copy()
        if df.empty:
            return
        self.pestmode = 'regularisation'
        for c in ('PINME', 'EQ', 'OBGNME'):
            df[c] = _clean(df[c])
        self.prior = _append(self.prior, df[PRIOR_COLS])

    def add_obsgp(self, df):
        """Observation groups with optional covariance files (OBGNME, COVFILE); sets the group order."""
        df = df.dropna(subset=['OBGNME']).copy()
        df['OBGNME'] = _clean(df['OBGNME'])
        self.obsgp_order = list(dict.fromkeys(df['OBGNME']))
        if 'COVFILE' in df:
            self.obs_cov.update({g: fmt(f) for g, f in zip(df['OBGNME'], df['COVFILE']) if fmt(f)})

    def add_io(self, df):
        """Rows of (type, in, out): tpl/ins pairs, or cmd with the command in the second column."""
        for r in df.itertuples(index=False):
            t = fmt(r[0]).lower()
            if t == 'tpl':
                self.tpl.append((fmt(r[1]), fmt(r[2])))
            elif t == 'ins':
                self.ins.append((fmt(r[1]), fmt(r[2])))
            elif t == 'cmd':
                self.cmd.append(fmt(r[1]))

    def add_tpl(self, tpl, model_in):
        self.tpl.append((tpl, model_in))

    def add_ins(self, ins, model_out):
        self.ins.append((ins, model_out))

    def set_command_line(self, cmd):
        self.cmd = [cmd]

    def add_pp(self, df):
        """PEST++ options from the first two columns (name, value); blank values are skipped."""
        where = df.attrs.get('where')           # 'PP!A{}' or 'pp.csv:{}' from load_table
        for i, r in enumerate(df.itertuples(index=False)):
            k, v = fmt(r[0]), r[1]
            if not k or fmt(v) == '':
                continue
            if isinstance(v, (float, np.floating)) and float(v).is_integer():
                v = int(v)
            self.pestpp.append((k, fmt(v)))
            if where:
                self.pestpp_where.setdefault(k.lower(), []).append(where.format(i + 2))

    def add_comment(self, table=None, comment=''):
        """Free-text header comments; a one-column table adds a line per cell, wider tables are tabulated."""
        if comment:
            self.comments += [c.rstrip() for c in str(comment).splitlines()]
        if table is not None:
            if table.shape[1] == 1:
                self.comments += [fmt(v) for v in table.iloc[:, 0] if fmt(v)]
            else:
                self.comments += table_lines(table, header=True)

    def fill_parval(self, parfile, real=None):
        """Replace PARVAL1 with the values in a PEST .par file or a PESTPP-IES ensemble (see read_par).

        A .par value written under a different SCALE / OFFSET than this control file's is converted so the
        model sees the same value (see `rescale_par`); an ensemble carries no scales and is copied as is.
        """
        vals = read_par(parfile, real)
        hit, value, converted = _to_internal(vals, self.par)
        missing = self.par.loc[~hit, 'PARNME']
        if len(missing):
            warnings.warn(f'{len(missing)} parameters not in {parfile}: {list(missing[:5])}...')
        if converted:
            print(f'{converted} values in {parfile} were written with a different SCALE / OFFSET; '
                  f'converted to this control file\'s so the model sees the same values')
        self.par.loc[hit, 'PARVAL1'] = value.values

    def set_par(self, changes, sync_ties=True, sync_prior=True):
        """Set parameter fields on the parameters matched by a glob, keeping ties and prior targets consistent.

        `changes` maps a name pattern to the fields to set, e.g.
        `{'sycr0*': {'PARVAL1': 0.15, 'PARLBND': 0.08, 'PARUBND': 0.30}, 'getscaler': {'PARVAL1': 14.1}}`.
        Patterns are fnmatch, matched case-insensitively; a pattern that matches nothing raises.

        Two invariants are easy to break by hand and are maintained here:

        * `sync_ties` - a tied parameter tracks its parent by their value ratio, so moving a parent's PARVAL1
          should move its children by the same factor. Their bounds are left alone unless the new value would
          fall outside them, in which case they are scaled too: PEST_HP refuses to start on a tied parameter
          outside its own bounds, and that is exactly what a parent moved by hand leaves behind.
        * `sync_prior` - preferred-value regularisation defends a target, not a starting point. A target left
          behind pulls the parameter back to the old value on the first iteration, silently undoing the edit.
          Only single-parameter equations are touched; multi-parameter ones (preferred difference, homogeneity)
          are relationships rather than values and are left alone.

        Returns a dict with the names changed, the tied children rescaled and the prior labels retargeted.
        """
        par = self.par
        num = lambda s: pd.to_numeric(s, errors='coerce')                # noqa: E731  (sections._num is scalar)
        lower = par['PARNME'].astype(str).str.lower()
        before = dict(zip(lower, num(par['PARVAL1'])))
        touched, unknown_cols = [], []
        for pattern, fields in changes.items():
            pat = str(pattern).lower()
            hit = pd.Series([fnmatch.fnmatch(n, pat) for n in lower], index=par.index)
            if not hit.any():
                raise ValueError(f'no parameter matches {pattern!r}')
            for col, value in fields.items():
                col = col.upper()
                if col not in par.columns:
                    if col not in PAR_COLS + PAR_EXTRA:
                        unknown_cols.append(col)
                        continue
                    par[col] = ''
                par.loc[hit, col] = _num(value) if isinstance(value, str) else value   # csv/CLI gives strings
            touched += list(lower[hit])
        if unknown_cols:
            raise ValueError(f'not parameter columns: {sorted(set(unknown_cols))}')
        touched = list(dict.fromkeys(touched))

        rescaled = []
        if sync_ties and 'TIETO' in par and (par['PARTRANS'] == 'tied').any():
            after = dict(zip(lower, num(par['PARVAL1'])))
            tied = par['PARTRANS'] == 'tied'
            tie_to = par['TIETO'].astype(str).str.strip().str.lower()
            for i in par.index[tied]:
                parent = tie_to[i]
                old, new = before.get(parent), after.get(parent)
                if parent not in touched or not old or old == new or pd.isna(new):
                    continue
                factor = new / old
                val, lo, hi = (num(pd.Series([par.at[i, c]])).iloc[0] for c in ('PARVAL1', 'PARLBND', 'PARUBND'))
                if pd.isna(val):
                    continue
                val *= factor
                par.at[i, 'PARVAL1'] = val
                if not pd.isna(lo) and not pd.isna(hi) and not (lo <= val <= hi):
                    par.at[i, 'PARLBND'], par.at[i, 'PARUBND'] = lo * factor, hi * factor
                rescaled.append(lower[i])

        retargeted = []
        if sync_prior and self.nprior:
            after = dict(zip(lower, num(par['PARVAL1'])))
            moved = {n for n in touched if before.get(n) != after.get(n)} | set(rescaled)
            for i in self.prior.index:
                term = _single_term(self.prior.at[i, 'EQ'])
                if term is None:
                    continue
                coef, name, is_log = term
                if name not in moved:
                    continue
                v = after.get(name)
                if v is None or pd.isna(v) or (is_log and v <= 0):
                    continue
                lhs = str(self.prior.at[i, 'EQ']).split('=', 1)[0].rstrip()
                self.prior.at[i, 'EQ'] = f'{lhs} = {fmt(coef * (np.log10(v) if is_log else v))}'
                retargeted.append(str(self.prior.at[i, 'PINME']))
        return {'parameters': touched, 'tied_rescaled': rescaled, 'prior_retargeted': retargeted}

    def rescale_par(self, patterns=None, groups=None, include_fixed=True, undo=False):
        """Move each selected parameter's value into its SCALE so it starts at 1, without changing the model.

        PEST applies SCALE and OFFSET only when it writes a model input file: the model gets
        PARVAL1 * SCALE + OFFSET. A parameter at value v becomes PARVAL1 = 1, SCALE = SCALE * v,
        bounds / v (swapped when v < 0), OFFSET unchanged, so every model input file is the same as before.
        `undo=True` folds SCALE back into the value instead (PARVAL1 * SCALE, bounds * SCALE, SCALE = 1).

        Selection: `patterns` (fnmatch globs, case-insensitive) and `groups` (PARGP names); a parameter is
        selected when it matches every filter given, and all parameters are when neither is. A pattern or
        group that matches nothing raises. `include_fixed=False` leaves fixed parameters alone. Tied children
        follow a selected parent, so a tied family ends up consistent.

        Prior information is written in PEST's own units, so equations naming a rescaled parameter are
        rewritten exactly: linear terms take the factor into the coefficient, log terms move log10(v) to the
        right-hand side. Each equation's residual is the same number as before, so weights are unchanged.
        Equations generated from a PRIOR / WEIGHT column are kept as explicit rows (the column cannot express
        the rewritten form) and those cells are cleared. An equation that cannot be parsed raises before
        anything is changed.

        Returns a dict: `rescaled` (names), `skipped` ({name: reason}), `prior_rewritten` and `prior_frozen`
        (labels), and `warnings` for what depends on parameter units but cannot be converted - absolute
        derivative increments, absolute(n) change limits, and PEST++ / SVD-assist files named in the control
        file (a covariance matrix, Jacobian or ensemble written for the old values).
        """
        par = self.par
        num = lambda s: pd.to_numeric(s, errors='coerce')                # noqa: E731
        lower = par['PARNME'].astype(str).str.lower()
        trans = par['PARTRANS'].astype(str).str.lower()
        pargp = par['PARGP'].astype(str).str.lower()

        selected = pd.Series(True, index=par.index)
        if patterns:
            pats = [str(p).lower() for p in ([patterns] if isinstance(patterns, str) else patterns)]
            hits = {p: lower.map(lambda n, p=p: fnmatch.fnmatch(n, p)) for p in pats}
            unmatched = [p for p, h in hits.items() if not h.any()]
            if unmatched:
                raise ValueError(f'no parameter matches {unmatched}')
            selected &= pd.concat(hits.values(), axis=1).any(axis=1)
        if groups:
            want = {str(g).strip().lower() for g in ([groups] if isinstance(groups, str) else groups)}
            unknown = sorted(want - set(pargp))
            if unknown:
                raise ValueError(f'no parameter in groups {unknown}')
            selected &= pargp.isin(want)
        if not include_fixed:
            selected &= trans != 'fixed'
        if 'TIETO' in par:
            parent = par['TIETO'].astype(str).str.strip().str.lower()
            selected |= (trans == 'tied') & parent.isin(set(lower[selected]))

        val = num(par['PARVAL1'])
        scale = num(par['SCALE']).fillna(1.0) if 'SCALE' in par else pd.Series(1.0, index=par.index)
        factors, skipped = {}, {}
        for i in par.index[selected]:
            f = 1.0 / scale[i] if undo else val[i]
            if pd.isna(val[i]) or pd.isna(f):
                skipped[lower[i]] = 'value is not a number'
            elif f == 0:
                skipped[lower[i]] = 'PARVAL1 is zero'
            elif trans[i] == 'log' and f < 0:
                skipped[lower[i]] = 'log-transformed with a negative factor'
            elif f != 1:
                factors[lower[i]] = f

        # PRIOR / WEIGHT generated rows cannot carry the rewritten form: keep them as explicit rows
        frozen, auto = [], None
        if 'PRIOR' in par and 'WEIGHT' in par and self.pestmode == 'regularisation':
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                auto = self._auto_prior()
            auto = auto[auto['EQ'].map(lambda eq: any(p in factors for p in equation_params(eq))).astype(bool)]
        prior = self.prior.copy()
        if auto is not None and len(auto):
            new = auto[~auto['PINME'].isin(set(prior['PINME']))]
            prior = _append(prior, new) if len(new) else prior
            frozen = list(auto['PINME'])

        rewritten = []                     # everything is computed before anything changes
        for i in prior.index:
            eq = _rescale_equation(prior.at[i, 'EQ'], factors)
            if eq is not None:
                prior.at[i, 'EQ'] = eq
                rewritten.append(str(prior.at[i, 'PINME']))

        idx = par.index[lower.isin(factors)]
        f = lower[idx].map(factors)
        lo, hi = num(par.loc[idx, 'PARLBND']) / f, num(par.loc[idx, 'PARUBND']) / f
        flip = f < 0
        if 'SCALE' not in par:
            par['SCALE'] = 1.0
        for c in ('PARVAL1', 'SCALE', 'PARLBND', 'PARUBND'):
            if pd.api.types.is_numeric_dtype(par[c]):
                par[c] = par[c].astype(float)             # SCALE reads as int; it takes floats now
        par.loc[idx, 'PARVAL1'] = (val[idx] * scale[idx]) if undo else 1.0
        par.loc[idx, 'SCALE'] = 1.0 if undo else scale[idx] * f
        par.loc[idx, 'PARLBND'] = lo.where(~flip, hi)
        par.loc[idx, 'PARUBND'] = hi.where(~flip, lo)
        if frozen:
            owner = lower.isin(frozen)
            par.loc[owner, ['PRIOR', 'WEIGHT']] = None
            self._auto_labels -= set(frozen)
        self.prior = prior

        notes = []
        moved = idx[trans[idx].isin(ADJUSTABLE)]
        if len(moved) and len(self.pargp):
            gp = self.pargp.assign(key=self.pargp['PARGPNME'].astype(str).str.lower()).set_index('key')
            for g in dict.fromkeys(pargp[moved]):
                if g not in gp.index:
                    continue
                what = []
                if str(gp.at[g, 'INCTYP']).strip().lower() == 'absolute':
                    what.append(f"INCTYP absolute (DERINC {fmt(gp.at[g, 'DERINC'])})")
                if 'DERINCLB' in gp and (num(pd.Series([gp.at[g, 'DERINCLB']])).fillna(0) > 0).iloc[0]:
                    what.append(f"DERINCLB {fmt(gp.at[g, 'DERINCLB'])}")
                if what:
                    names = list(lower[moved][pargp[moved] == g])
                    notes.append(f"parameter group {g}: {' and '.join(what)} is in parameter units and was not "
                                 f"rescaled; it now applies to {len(names)} rescaled parameters: {names[:5]}")
        absolute = list(lower[moved][par.loc[moved, 'PARCHGLIM'].astype(str).str.lower().str.startswith('absolute')])
        if absolute:
            notes.append(f'absolute(n) change limits are in parameter units and were not rescaled: {absolute[:5]}')
        if factors:
            from .pestpp import canonical
            named = sorted({canonical(k) for k, _ in self.pestpp} & set(_PARAMETER_UNIT_FILES))
            named += [k for k in ('basepestfile', 'basejacfile') if fmt(self.control.get(k))]
            if named:
                notes.append(f'{", ".join(named)} name files written for the old parameter values; '
                             f'they no longer match the rescaled parameters')
        return {'rescaled': list(lower[idx]), 'skipped': skipped, 'prior_rewritten': rewritten,
                'prior_frozen': frozen, 'warnings': notes}

    # ------------------------------------------------------------------ validation
    def validate(self):
        """Audit this model without changing it; return errors, warnings and available fixes."""
        # Reuse the same pure table and pestchek-style checks as ``makepst validate``.
        # Imports are local because checks/rules also use the Pst type and writer helpers.
        from .checks import Finding, check_tables
        from .rules import check_extra

        findings = check_tables(self) + check_extra(self)
        # PEST rejects a starting value outside its bounds, so `makepst validate` calls it an error; a
        # control file carrying one (parrep from an IES realization, say) is still worth writing
        for f in findings:
            if f.severity == 'error' and f.message.startswith('adjustable PARVAL1 outside bounds'):
                f.severity = 'warning'
        report = ValidationReport(
            errors=[f for f in findings if f.severity == 'error'],
            warnings=[f for f in findings if f.severity in ('warning', 'info')],
            _owner_id=id(self),
        )

        used = set(self.par['PARGP'])
        unused = tuple(self.pargp.loc[~self.pargp['PARGPNME'].isin(used), 'PARGPNME'].tolist())
        if unused:
            report.fixes_available.append(ValidationFix(
                'unused_parameter_groups', 'Remove unused parameter groups', unused))

        if 'TIETO' in self.par:
            trans = self.par.set_index('PARNME')['PARTRANS']
            tied = self.par['PARTRANS'].eq('tied')
            targets = self.par.loc[tied, 'TIETO'].astype(str).str.strip().str.lower().map(trans)
            fixed = tuple(self.par.loc[tied].loc[targets.eq('fixed').values, 'PARNME'].tolist())
            if fixed:
                report.fixes_available.append(ValidationFix(
                    'tied_to_fixed', 'Convert parameters tied to fixed parameters to fixed', fixed))

        prior_rows = []
        adjustable = set(self.par.loc[self.par['PARTRANS'].isin(ADJUSTABLE), 'PARNME'])
        if self.nprior:
            for row in self.prior.itertuples():
                refs = equation_params(str(row.EQ))
                if any(name not in adjustable for name in refs):
                    prior_rows.append(str(row.PINME))
        generate_auto = False
        if self.pestmode == 'regularisation' and {'PRIOR', 'WEIGHT'} <= set(self.par):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                expected = self._auto_prior()
            report.warnings.extend(Finding('warning', 'prior information', str(w.message)) for w in caught)
            # _build_prior drops a PRIOR that names a fixed, tied or missing parameter, so
            # compare against what it would keep, or normalize() could never satisfy this check
            buildable = expected['EQ'].map(
                lambda eq: all(p in adjustable for p in equation_params(eq))).astype(bool)
            if (~buildable).any():
                skipped = expected.loc[~buildable, 'PINME'].tolist()
                report.warnings.append(Finding(
                    'warning', 'prior information',
                    f'{len(skipped)} PRIOR entries name a fixed, tied or missing parameter and '
                    f'get no equation: {skipped[:5]}'))
            expected = expected[buildable]
            current = self.prior[self.prior['PINME'].isin(self._auto_labels)]
            manual = self.prior[~self.prior['PINME'].isin(self._auto_labels)]
            collisions = tuple(sorted(set(expected['PINME']) & set(manual['PINME'])))
            if collisions:
                report.errors.append(Finding(
                    'error', 'prior information',
                    f'generated prior labels already exist as explicit rows: {list(collisions[:5])}'))
            signature = lambda df: sorted(tuple(fmt(row[c]) for c in PRIOR_COLS)
                                          for _, row in df[PRIOR_COLS].iterrows())
            generate_auto = not collisions and signature(current) != signature(expected)
            if generate_auto:
                report.errors.append(Finding(
                    'error', 'prior information',
                    'regularisation prior rows from parameter PRIOR/WEIGHT fields have not been generated; '
                    'call normalize() or apply the prior_information fix'))
        if prior_rows or generate_auto:
            details = tuple(dict.fromkeys(prior_rows))
            desc = 'Rebuild prior information from parameter PRIOR/WEIGHT fields and remove equations with non-adjustable or missing references'
            report.fixes_available.append(ValidationFix('prior_information', desc, details))
            if prior_rows:
                report.errors.append(Finding(
                    'error', 'prior information',
                    f'references fixed, tied or missing parameters: {list(details[:5])}'))
        if any(f.kind == 'tied_to_fixed' for f in report.fixes_available):
            fixed = next(f.items for f in report.fixes_available if f.kind == 'tied_to_fixed')
            report.errors.append(Finding(
                'error', 'parameters', f'tied parameters have fixed targets: {list(fixed[:5])}'))
        return report

    def apply_fixes(self, report=None):
        """Apply the fixes listed by a validation report and return a fresh report.

        Fixes are opt-in. Pass the report you reviewed, or omit it to validate immediately
        before applying. Structural errors such as missing groups and invalid tie chains are
        left for the caller to correct; independent fixes can still be applied safely.
        """
        if report is None:
            report = self.validate()
        if report._owner_id != id(self):
            raise ValueError('validation report belongs to a different Pst instance')
        available_now = {fix.kind for fix in self.validate().fixes_available}
        kinds = {fix.kind for fix in report.fixes_available} & available_now
        if 'tied_to_fixed' in kinds and 'TIETO' in self.par:
            trans = self.par.set_index('PARNME')['PARTRANS']
            tied = self.par['PARTRANS'].eq('tied')
            targets = self.par.loc[tied, 'TIETO'].astype(str).str.strip().str.lower().map(trans)
            self.par.loc[tied & targets.reindex(self.par.index).eq('fixed').fillna(False), 'PARTRANS'] = 'fixed'
        if 'unused_parameter_groups' in kinds:
            used = set(self.par['PARGP'])
            defined = set(self.pargp['PARGPNME'])
            if used <= defined:
                self._filter_pargp()
        if 'prior_information' in kinds:
            # Duplicate labels are ambiguous, so leave that table untouched for manual repair.
            if not self.prior['PINME'].duplicated().any():
                can_rebuild = True
                if self.pestmode == 'regularisation' and {'PRIOR', 'WEIGHT'} <= set(self.par):
                    expected = self._auto_prior()
                    manual = self.prior[~self.prior['PINME'].isin(self._auto_labels)]
                    can_rebuild = not (set(expected['PINME']) & set(manual['PINME']))
                if can_rebuild:
                    self._build_prior()
        return self.validate()

    def normalize(self):
        """Apply currently available safe fixes, then return this Pst for chaining."""
        report = self.validate()
        self.apply_fixes(report)
        return self

    def _check_bounds(self):
        """PEST refuses to start with an adjustable parameter outside its bounds; say so early."""
        par = self.par
        v, lb, ub = (pd.to_numeric(par[c], errors='coerce') for c in ('PARVAL1', 'PARLBND', 'PARUBND'))
        out = par['PARTRANS'].isin(ADJUSTABLE) & ((v < lb) | (v > ub))
        if out.any():
            warnings.warn(f'{int(out.sum())} adjustable parameters have PARVAL1 outside their bounds: '
                          f'{list(par.loc[out, "PARNME"][:5])}...')

    def _resolve_tied(self):
        par = self.par
        tied = par['PARTRANS'] == 'tied'
        if not tied.any():
            return
        if 'TIETO' not in par:
            raise ValueError('tied parameters present but no TIETO column / tied table')
        par['TIETO'] = par['TIETO'].where(par['TIETO'].notna(), None)
        par.loc[tied, 'TIETO'] = _clean(par.loc[tied, 'TIETO'])
        trans = par.set_index('PARNME')['PARTRANS']
        target = par.loc[tied, 'TIETO'].map(trans)
        bad = par.loc[tied][target.isna().values]
        if len(bad):
            raise ValueError(f'tied parameters whose target is missing: '
                             f'{list(zip(bad.PARNME, bad.TIETO))[:10]}')
        chain = par.loc[tied][(target == 'tied').values]
        if len(chain):
            raise ValueError(f'parameters tied to a tied parameter: {list(chain.PARNME[:10])}')
        to_fix = par.loc[tied][(target == 'fixed').values]
        if len(to_fix):
            print(f'{len(to_fix)} parameters tied to fixed parameters; setting them fixed too')
            par.loc[to_fix.index, 'PARTRANS'] = 'fixed'

    def _filter_pargp(self):
        used = list(dict.fromkeys(self.par['PARGP']))
        missing = [g for g in used if g not in set(self.pargp['PARGPNME'])]
        if missing:
            raise ValueError(f'parameter groups without a definition: {missing}')
        self.pargp = self.pargp[self.pargp['PARGPNME'].isin(used)].reset_index(drop=True)

    def _build_prior(self):
        # drop what a previous validate() generated so this is idempotent
        prior = [self.prior[~self.prior['PINME'].isin(self._auto_labels)]]
        self._auto_labels = set()
        if self.pestmode == 'regularisation' and {'PRIOR', 'WEIGHT'} <= set(self.par):
            auto = self._auto_prior()
            self._auto_labels = set(auto['PINME'])
            prior.append(auto)
        # skip empty frames: pandas 2.x warns that they will stop influencing result dtypes
        prior = [df for df in prior if len(df)]
        prior = pd.concat(prior, ignore_index=True) if prior else _empty(PRIOR_COLS)
        dup = prior['PINME'][prior['PINME'].duplicated()].unique()
        if len(dup):
            raise ValueError(f'duplicate prior information labels: {list(dup[:10])}')
        adjustable = set(self.par.loc[self.par['PARTRANS'].isin(ADJUSTABLE), 'PARNME'])
        # astype(bool): an empty object-dtype mask would select columns, not rows
        ok = prior['EQ'].map(lambda eq: all(p in adjustable for p in equation_params(eq))).astype(bool)
        if (~ok).any():
            dropped = prior.loc[~ok, 'PINME']
            print(f'dropping {len(dropped)} prior equations that reference fixed, tied or '
                  f'missing parameters: {list(dropped[:5])}...')
        self.prior = prior[ok].reset_index(drop=True)

    def _auto_prior(self):
        """Regularisation equations from the PRIOR / WEIGHT columns of the parameter table.

        PRIOR is either a preferred value or the name of another parameter the
        parameter should equal.  Log-transformed parameters get log() equations.
        """
        p = self.par.dropna(subset=['PRIOR', 'WEIGHT'])
        p = p[(pd.to_numeric(p['WEIGHT'], errors='coerce') > 0) & p['PARTRANS'].isin(ADJUSTABLE)]
        rows = []
        for r in p.itertuples():
            islog = r.PARTRANS == 'log'
            name, target = r.PARNME, fmt(r.PRIOR).lower()
            if is_number(target):
                v = float(target)
                if islog:
                    if v <= 0:
                        warnings.warn(f'{name}: non-positive prior value {v} for a log parameter; skipped')
                        continue
                    eq = f'1.0 * log({name}) = {np.log10(v):{".11g"}}'
                else:
                    eq = f'1.0 * {name} = {v:{".11g"}}'
            elif islog:
                eq = f'1.0 * log({name}) - 1.0 * log({target}) = 0'
            else:
                eq = f'1.0 * {name} - 1.0 * {target} = 0'
            rows.append((name, eq, r.WEIGHT, ('regul' + r.PARGP)[:12]))
        return pd.DataFrame(rows, columns=PRIOR_COLS)


# ---------------------------------------------------------------------- helpers
def read_par(parfile, real=None):
    """Parameter values as a DataFrame indexed by lower-case parameter name.

    `parfile` is a PEST .par file (name value scale offset) or a PESTPP-IES ensemble
    (case.N.par.csv), in which case `real` picks the realization: a name such as
    'base' or '17', 'best' (lowest phi in case.phi.actual.csv next to it), or None
    for 'base'.
    """
    if is_ensemble(parfile):
        ens = read_ensemble(parfile)
        row = ens.loc[select_realization(ens, parfile, real)]
        return pd.DataFrame({'PARVAL1': pd.to_numeric(row)}).rename_axis('PARNME')
    df = pd.read_csv(parfile, sep=r'\s+', skiprows=1, header=None,
                     names=['PARNME', 'PARVAL1', 'SCALE', 'OFFSET'])
    df['PARNME'] = _clean(df['PARNME'])
    return df.set_index('PARNME')


def is_ensemble(path):
    """True for a PESTPP-IES ensemble csv (first column real_name)."""
    with open(path) as f:
        return f.readline().strip().lower().startswith('real_name')


def read_ensemble(path):
    """A PESTPP-IES ensemble csv (case.N.par.csv / case.N.obs.csv): rows are realizations."""
    df = pd.read_csv(path, index_col=0)
    df.index = df.index.astype(str).str.strip().str.lower()
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


_ENSEMBLE_NAME = re.compile(r'^(?P<case>.+?)\.(?P<iter>\d+)\.(par|obs)\.csv$', re.I)


def best_realization(path):
    """Realization with the lowest phi for ensemble file `path`, from case.phi.actual.csv next to it."""
    m = _ENSEMBLE_NAME.match(os.path.basename(path))
    if not m:
        raise ValueError(f'cannot tell case and iteration from {os.path.basename(path)!r}; '
                         f'expected case.N.par.csv')
    phi_file = os.path.join(os.path.dirname(path), f"{m['case']}.phi.actual.csv")
    if not os.path.exists(phi_file):
        raise FileNotFoundError(f"'best' needs {phi_file}")
    phi = pd.read_csv(phi_file)
    phi.columns = [str(c).strip().lower() for c in phi.columns]
    row = phi[phi['iteration'] == int(m['iter'])]
    if row.empty:
        raise ValueError(f"iteration {m['iter']} not in {phi_file}")
    stats = {'iteration', 'total_runs', 'mean', 'standard_deviation', 'min', 'max'}
    reals = row.iloc[0].drop(labels=[c for c in phi.columns if c in stats])
    return str(pd.to_numeric(reals).idxmin())


def select_realization(ens, path, real=None):
    if real is None:
        real = 'base'
        if real not in ens.index:
            raise ValueError(f"no 'base' realization in {path}; give one of {list(ens.index[:10])}...")
    elif str(real).lower() == 'best':
        real = best_realization(path)
    real = str(real).strip().lower()
    if real not in ens.index:
        raise ValueError(f'realization {real!r} not in {path}; available: {list(ens.index[:10])}...')
    return real


def read_obs_ensemble(path, real=None, pst=None):
    """One realization of a PESTPP-IES observation ensemble as MODELLED (+ RESIDUAL and GROUP with a Pst)."""
    ens = read_ensemble(path)
    row = pd.to_numeric(ens.loc[select_realization(ens, path, real)])
    df = pd.DataFrame({'MODELLED': row}).rename_axis('NAME')
    if pst is not None:
        obs = pst.obs.set_index('OBSNME')
        df['RESIDUAL'] = pd.to_numeric(obs['OBSVAL']).reindex(df.index) - df['MODELLED']
        df['WEIGHT'] = pd.to_numeric(obs['WEIGHT']).reindex(df.index)
        df['GROUP'] = obs['OBGNME'].reindex(df.index)
    return df


def read_res(resfile):
    """A PEST .res / .rei residuals file as a DataFrame indexed by lower-case observation name."""
    df = pd.read_csv(resfile, sep=r'\s+')
    df.columns = [c.strip().upper() for c in df.columns]
    df['NAME'] = _clean(df['NAME'])
    return df.set_index('NAME')


def table_lines(df, header=False, numeric_right=True):
    """A DataFrame as aligned, whitespace-delimited text lines (no index)."""
    cols = list(df.columns)
    cells = [[fmt(v) for v in df[c]] for c in cols]
    # right-align by content, not dtype, so a column reads the same whatever produced it
    is_num = [numeric_right and any(col) and all(is_number(s) for s in col if s) for col in cells]
    widths = [max([len(str(c)) if header else 0] + [len(s) for s in col]) for c, col in zip(cols, cells)]
    lines = []
    if header:
        lines.append('  '.join(f'{str(c):<{w}}' for c, w in zip(cols, widths)).rstrip())
    for i in range(len(df)):
        lines.append('  '.join(f'{col[i]:>{w}}' if right else f'{col[i]:<{w}}'
                               for col, w, right in zip(cells, widths, is_num)).rstrip())
    return lines
