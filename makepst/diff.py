"""`makepst diff`: what changed between two control files (or a control file and a workbook).

Compares the tables, not the text: parameters and observations added / removed / changed
column by column, prior equations, control values, `++` options, template / instruction
pairs and command lines. Numbers are compared with a relative tolerance so a reformatted
value is not a change.
"""
import numpy as np
import pandas as pd

from .pst import OBS_COLS, PAR_COLS, PRIOR_COLS, Pst
from .sections import fmt
from .writer import effective_control

PAR_COMPARE = [c for c in PAR_COLS if c != 'PARNME'] + ['TIETO']


class Diff:
    """Result of compare(): DataFrames per table plus dicts for the scalar parts."""

    def __init__(self):
        self.par = pd.DataFrame(columns=['PARNME', 'change', 'column', 'old', 'new'])
        self.obs = pd.DataFrame(columns=['OBSNME', 'change', 'column', 'old', 'new'])
        self.prior = pd.DataFrame(columns=['PINME', 'change', 'column', 'old', 'new'])
        self.pargp = pd.DataFrame(columns=['PARGPNME', 'change', 'column', 'old', 'new'])
        self.control = pd.DataFrame(columns=['NAME', 'change', 'old', 'new'])
        self.pestpp = pd.DataFrame(columns=['NAME', 'change', 'old', 'new'])
        self.io = pd.DataFrame(columns=['kind', 'change', 'pest file', 'model file'])
        self.cmd = pd.DataFrame(columns=['change', 'command'])
        self.comments = pd.DataFrame(columns=['change', 'comment'])

    @property
    def empty(self):
        return all(len(getattr(self, t)) == 0 for t in self.tables)

    tables = ('par', 'obs', 'prior', 'pargp', 'control', 'pestpp', 'io', 'cmd', 'comments')

    def summary(self):
        parts = []
        for t in self.tables:
            df = getattr(self, t)
            if len(df):
                kinds = df['change'].value_counts().to_dict()
                parts.append(f"{t}: " + ', '.join(f'{n} {k}' for k, n in kinds.items()))
        return '; '.join(parts) if parts else 'no differences'

    def to_text(self, max_rows=50):
        """The differences as text; `None` prints every row, `0` prints counts by kind and column.

        A bulk edit - new bounds across a parameter family, a weight rebalance - runs to hundreds of rows,
        where all the reader wants is confirmation that nothing unexpected moved.
        """
        out = []
        for t in self.tables:
            df = getattr(self, t)
            if not len(df):
                continue
            out.append(f'== {t} ({len(df)})')
            if max_rows == 0:
                keys = [c for c in ('change', 'column') if c in df.columns]
                for key, n in (df.groupby(keys).size().items() if keys else []):
                    label = ' '.join(str(k) for k in key) if isinstance(key, tuple) else str(key)
                    out.append(f'   {label}: {n}')
                continue
            show = (df if max_rows is None else df.head(max_rows)).astype(str)
            widths = [max(len(c), show[c].str.len().max()) for c in show.columns]
            out.append('  '.join(f'{c:<{w}}' for c, w in zip(show.columns, widths)))
            for row in show.itertuples(index=False):
                out.append('  '.join(f'{v:<{w}}' for v, w in zip(row, widths)))
            if max_rows is not None and len(df) > max_rows:
                out.append(f'... {len(df) - max_rows} more')
        return '\n'.join(out) if out else 'no differences'

    def to_workbook(self, path):
        with pd.ExcelWriter(path, engine='openpyxl') as xw:
            summary = pd.DataFrame({'table': list(self.tables),
                                    'changes': [len(getattr(self, t)) for t in self.tables]})
            summary.to_excel(xw, sheet_name='SUMMARY', index=False)
            for t in self.tables:
                df = getattr(self, t)
                if len(df):
                    df.to_excel(xw, sheet_name=t.upper(), index=False)


def _same(a, b, rtol):
    if a is None and b is None:
        return True
    if isinstance(a, float) and np.isnan(a) and isinstance(b, float) and np.isnan(b):
        return True
    try:
        fa, fb = float(a), float(b)
        if np.isnan(fa) and np.isnan(fb):
            return True
        return np.isclose(fa, fb, rtol=rtol, atol=0)
    except (TypeError, ValueError):
        return fmt(a) == fmt(b)


def _table_diff(old, new, key, columns, rtol):
    """Rows added / removed / changed between two DataFrames keyed on `key`."""
    o = old.set_index(key)
    n = new.set_index(key)
    rows = []
    for k in n.index.difference(o.index):
        rows.append((k, 'added', '', '', ''))
    for k in o.index.difference(n.index):
        rows.append((k, 'removed', '', '', ''))
    common = o.index.intersection(n.index)
    for c in columns:
        if c not in o and c not in n:
            continue
        a = o[c].reindex(common) if c in o else pd.Series(None, index=common, dtype=object)
        b = n[c].reindex(common) if c in n else pd.Series(None, index=common, dtype=object)
        for k, va, vb in zip(common, a, b):
            if not _same(va, vb, rtol):
                rows.append((k, 'changed', c, fmt(va), fmt(vb)))
    return pd.DataFrame(rows, columns=[key, 'change', 'column', 'old', 'new'])


def _scalar_diff(old, new, rtol, key='NAME'):
    rows = []
    for k in sorted(set(old) | set(new)):
        if k not in old:
            rows.append((k, 'added', '', fmt(new[k])))
        elif k not in new:
            rows.append((k, 'removed', fmt(old[k]), ''))
        elif not _same(old[k], new[k], rtol):
            rows.append((k, 'changed', fmt(old[k]), fmt(new[k])))
    return pd.DataFrame(rows, columns=[key, 'change', 'old', 'new'])


def _list_diff(old, new, col):
    rows = [('added', x) for x in new if x not in old] + [('removed', x) for x in old if x not in new]
    return pd.DataFrame(rows, columns=['change', col])


def _tie_only_when_tied(par):
    """TIETO only matters for tied parameters; workbooks often fill it for every row."""
    if 'TIETO' not in par:
        return par
    par = par.copy()
    par.loc[par['PARTRANS'] != 'tied', 'TIETO'] = None
    return par


def _tie_scale_diff(old, new, rtol):
    """Compare tied parameters' PARVAL1 ratio to their tied-to parameter."""
    def ratios(par):
        if 'TIETO' not in par:
            return {}
        indexed = par.set_index(par['PARNME'].astype(str).str.lower(), drop=False)
        result = {}
        for row in par.loc[par['PARTRANS'].astype(str).str.lower() == 'tied'].itertuples(index=False):
            name = str(row.PARNME)
            parent_name = str(row.TIETO).strip()
            parent = indexed.loc[parent_name.lower()] if parent_name.lower() in indexed.index else None
            if parent is None or isinstance(parent, pd.DataFrame):
                continue
            try:
                child_value = float(row.PARVAL1)
                parent_value = float(parent['PARVAL1'])
                if not np.isfinite(child_value) or not np.isfinite(parent_value) or parent_value == 0:
                    continue
                result[name.lower()] = (name, parent_name.lower(), child_value / parent_value)
            except (TypeError, ValueError):
                continue
        return result

    before, after = ratios(old), ratios(new)
    rows = []
    for key in sorted(before.keys() & after.keys()):
        old_name, old_parent, old_ratio = before[key]
        new_name, new_parent, new_ratio = after[key]
        if old_parent != new_parent:
            continue  # TIETO itself is reported by the ordinary parameter diff.
        if not _same(old_ratio, new_ratio, rtol):
            rows.append((new_name, 'changed', 'tied factor', fmt(old_ratio), fmt(new_ratio)))
    return pd.DataFrame(rows, columns=['PARNME', 'change', 'column', 'old', 'new'])


def compare(old: Pst, new: Pst, rtol=1e-9):
    """Differences from `old` to `new`, as a Diff."""
    d = Diff()
    d.par = _table_diff(_tie_only_when_tied(old.par), _tie_only_when_tied(new.par), 'PARNME', PAR_COMPARE, rtol)
    d.par = pd.concat([d.par, _tie_scale_diff(old.par, new.par, rtol)], ignore_index=True)
    d.obs = _table_diff(old.obs, new.obs, 'OBSNME', [c for c in OBS_COLS if c != 'OBSNME'], rtol)
    d.prior = _table_diff(old.prior, new.prior, 'PINME', [c for c in PRIOR_COLS if c != 'PINME'], rtol)
    d.pargp = _table_diff(old.pargp, new.pargp, 'PARGPNME',
                          [c for c in old.pargp.columns.union(new.pargp.columns) if c != 'PARGPNME'], rtol)
    d.control = _scalar_diff(effective_control(old), effective_control(new), rtol)
    d.pestpp = _scalar_diff(dict(old.pestpp), dict(new.pestpp), rtol)
    io_old = [('tpl', *t) for t in old.tpl] + [('ins', *t) for t in old.ins]
    io_new = [('tpl', *t) for t in new.tpl] + [('ins', *t) for t in new.ins]
    rows = [('added', *x) for x in io_new if x not in io_old] + [('removed', *x) for x in io_old if x not in io_new]
    d.io = pd.DataFrame([(k, ch, a, b) for ch, k, a, b in rows], columns=['kind', 'change', 'pest file', 'model file'])
    d.cmd = _list_diff(old.cmd, new.cmd, 'command')
    d.comments = _list_diff(old.comments, new.comments, 'comment')
    return d
