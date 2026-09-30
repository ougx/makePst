"""The pestchek rules that go beyond makepst's own consistency checks.

Derived from the message catalogue of PEST 17.2's pestchek (pestchek.F / cheksub.F; the version is
PESTCHEK_SOURCE_VERSION), with the PEST_HP control variables' limits from the PEST_HP 17 manual, leaving
out what makepst cannot see or does not model (text-format parse errors, JUPITER derivative
files, adaptive-regularisation "iw_" parameters, secondary and file parameters, distribution
files). Each function returns Finding objects; the driver in checks.py collects them.
"""
import difflib
import re

import numpy as np
import pandas as pd

from .pst import ADJUSTABLE, Pst, equation_params
from .sections import ALL_SECTIONS, FIELD_SECTION, PESTCHEK_SOURCE_VERSION, extra_token, fmt, token_name
from .writer import effective_control

INCTYP = ('relative', 'absolute', 'rel_to_max')
FORCEN = ('switch', 'always_2', 'always_3', 'switch_5', 'always_5')
DERMTHD_3PT = ('parabolic', 'best_fit', 'outside_pts')
DERMTHD_5PT = ('minvar', 'maxprec')
SPLITACTION = ('smaller', 'zero', 'previous')
# control variables only PEST_HP (or PEST with /hpstart) accepts; pestchek warns about each
HP_ONLY = ('run_abandon_fac', 'win_mrun_hours', 'softstophours', 'hardstophours', 'rrfsave', 'zerosenval',
           'orr_not_first', 'uptestlim', 'uptestmin', 'reg2measrat', 'jcowarnthresh', 'jcozerothresh')
# `++` options that some PEST++ builds reject outright, with what is known first-hand about which. PEST++ stops
# on an option it does not recognise rather than ignoring it, and old copies of the binaries live on in run
# folders, so a control file that works on one machine can refuse to start on another. Add entries as they
# are met; an option missing here is not a claim that every build accepts it.
# What PEST reads each control variable as. Flag fields (precis, lamforgive, ...) are checked against the words
# their section accepts; the rest are text PEST does not parse as a number.
INTEGER = set('''
    maxcompdim numcom jacfile messfile numlam jacupdate iboundstick upvecbend noptswitch noptmax nphistp nphinored
    nrelpar lastrun icov icor ieig ires svdmode maxsing eigwrite lsqrmode lsqr_itnlim lsqrwrite maxaui auistartopt
    auirestitn auiholdmaxchg auinumfree nauinoaccept iregadj noptregadj uptestmin uptestlim svda_mulbpa
    svda_scaladj svda_extsuper svda_supdercalc svda_par_excl'''.split())
REAL = set('''
    derzerolim rlambda1 rlamfac phiratsuf phiredlam relparmax facparmax facorig phiredswh splitswh phiredstp
    relparstp phistopthresh eigthresh lsqr_atol lsqr_btol lsqr_conlim noauiphirat auisensrat auiphiratsuf
    auiphirataccept phimlim phimaccept fracphim wfinit wfmin wfmax wffac wftol regweightrat regsingthresh
    run_slow_fac run_abandon_fac win_mrun_hours jcowarnthresh jcozerothresh zerosenval hardstophours softstophours
    reg2measrat'''.split())
TEXT = {'phiabandon', 'absparmax', 'basepestfile', 'basejacfile'}   # phiabandon may name a file
# Words pestchek looks for by name that are not control-data variables for makePst's schema: section-header
# words (the PEST_HP "simultaneous parameter increments", "randomized jacobian" and "rsi" sections are kept
# verbatim as unknown sections), and secondary / file parameters, which makePst does not model.
# tools/rule_sources.py reports any other word pestchek reads that the schema does not know.
PESTCHEK_IGNORED = ('group', 'groups', 'lsqr', 'name', 'param', 'random', 'row', 'rsi', 'simul', 'simultaneous',
                    'fileparfile', 'nparsec', 'nparfile', 'nequation')
MAXABSPARMAX = 10
_INT = re.compile(r'^[-+]?\d+$')
_REAL = re.compile(r'^[-+]?(\d+\.?\d*|\.\d+)([eEdD][-+]?\d+)?$')     # Fortran reads 1d-3 too
PESTPP_VERSIONED = {
    'glm_hp_lambdas': 'accepted by pestpp-glm 5.2.27, rejected by 5.2.17',
}


def _finding(severity, where, message):
    from .checks import Finding
    return Finding(severity, where, message)


def _names(items, n=6):
    items = list(items)
    return ', '.join(str(i) for i in items[:n]) + (f' ... ({len(items)} total)' if len(items) > n else '')


def _num(series):
    return pd.to_numeric(series, errors='coerce')


# ---------------------------------------------------------------------- parameter data
def check_parameters(pst: Pst):
    out = []
    err = lambda w, m: out.append(_finding('error', w, m))      # noqa: E731
    warn = lambda w, m: out.append(_finding('warning', w, m))   # noqa: E731
    par = pst.par
    if par.empty:
        return out
    ctl = effective_control(pst)
    v, lb, ub = _num(par['PARVAL1']), _num(par['PARLBND']), _num(par['PARUBND'])
    trans = par['PARTRANS'].astype(str).str.lower()
    chg = par['PARCHGLIM'].astype(str).str.lower().str.strip()
    islog = trans == 'log'
    adj = trans.isin(ADJUSTABLE)

    if adj.sum() == 0:
        err('parameters', 'no parameters to estimate: all are fixed or tied')

    # PARCHGLIM vocabulary, absolute(n) and the log / factor rule
    absn = chg.str.extract(r'^absolute\((\d+)\)$')[0]
    bad = par.loc[~(chg.isin(('relative', 'factor')) | absn.notna()), 'PARNME']
    if len(bad):
        err('parameters', f'PARCHGLIM must be relative, factor or absolute(n): {_names(bad)}')
    abs_defined = set(re.findall(r'absparmax\((\d+)\)', str(ctl.get('absparmax', '')).lower()))
    undefined = sorted({n for n in absn.dropna() if n not in abs_defined})
    if undefined:
        err('parameters', f'absolute(n) change limits with no absparmax(n)= value in the control data: n = {_names(undefined)}')
    bad = par.loc[islog & (chg != 'factor'), 'PARNME']
    if len(bad):
        err('parameters', f'log-transformed parameters must be factor-limited (PARCHGLIM): {_names(bad)}')

    # values and bounds
    bad = par.loc[(islog & (v <= 0)).fillna(False), 'PARNME']
    if len(bad):
        err('parameters', f'log-transformed parameters with a non-positive initial value: {_names(bad)}')
    bad = par.loc[(adj & (ub <= lb)).fillna(False), 'PARNME']
    if len(bad):
        err('parameters', f'upper bound equal to or below the lower bound: {_names(bad)}')
    factor = chg == 'factor'
    bad = par.loc[(adj & factor & ((lb == 0) | (ub == 0) | (lb * ub < 0))).fillna(False), 'PARNME']
    if len(bad):
        err('parameters', f'factor-limited parameters need bounds of the same sign and neither zero: {_names(bad)}')
    relparmax = _num(pd.Series([ctl.get('relparmax')]))[0]
    if pd.notna(relparmax) and relparmax <= 1:
        bad = par.loc[(adj & (chg == 'relative') & ((lb == 0) | (ub == 0) | (lb * ub < 0))).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'RELPARMAX <= 1 but relative-limited parameters have a zero bound or bounds of opposite sign: {_names(bad)}')
    if 'SCALE' in par:
        bad = par.loc[(_num(par['SCALE']) == 0).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'SCALE must not be zero: {_names(bad)}')
    if 'DERCOM' in par:
        dercom = _num(par['DERCOM'])
        bad = par.loc[(dercom < 0).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'DERCOM must not be negative: {_names(bad)}')
        jacfile = _num(pd.Series([ctl.get('jacfile', 0)]))[0]
        bad = par.loc[(adj & (dercom == 0)).fillna(False), 'PARNME']
        if len(bad) and not jacfile:
            err('parameters', f'DERCOM 0 means externally supplied derivatives, but JACFILE is 0: {_names(bad)}')

    # tied parameters
    tied = trans == 'tied'
    if tied.any() and 'TIETO' in par:
        target = par['TIETO'].astype(str).str.strip().str.lower()
        bad = par.loc[tied & (target == par['PARNME']), 'PARNME']
        if len(bad):
            err('parameters', f'tied to itself: {_names(bad)}')
        bad = par.loc[(tied & (v == 0)).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'tied parameters cannot have an initial value of zero: {_names(bad)}')
        parents = set(target[tied])
        bad = par.loc[(par['PARNME'].isin(parents) & (v == 0)).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'parent of a tied parameter cannot have an initial value of zero: {_names(bad)}')
        # A tied parameter keeps its own bounds, and PEST_HP refuses to start when its initial value falls
        # outside them. That is easy to do without noticing: setting a parent's value - parameter replacement,
        # a hand edit, a new starting point - moves the children but not their bounds.
        bad = par.loc[(tied & ((v < lb) | (v > ub))).fillna(False), 'PARNME']
        if len(bad):
            err('parameters', f'tied PARVAL1 outside its own bounds: {_names(bad)}')
    bad = par.loc[(par['PARGP'].astype(str).str.lower() == 'none') & ~trans.isin(('tied', 'fixed')), 'PARNME']
    if len(bad):
        err('parameters', f'group "none" is reserved for fixed and tied parameters: {_names(bad)}')

    # derivative increments against the parameter range (warnings, as in pestchek)
    if len(pst.pargp):
        gp = pst.pargp.set_index(pst.pargp['PARGPNME'].astype(str).str.lower())
        inctyp = par['PARGP'].map(gp['INCTYP'].astype(str).str.lower()) if 'INCTYP' in gp else None
        derinc = par['PARGP'].map(_num(gp['DERINC'])) if 'DERINC' in gp else None
        derinclb = par['PARGP'].map(_num(gp['DERINCLB'])) if 'DERINCLB' in gp else None
        if inctyp is not None and derinc is not None:
            inc = np.where(inctyp == 'relative', derinc * v.abs(), derinc)
            inc = pd.Series(inc, index=par.index)
            if derinclb is not None:
                inc = inc.where(inc >= derinclb.fillna(0), derinclb)
            too_big = adj & (inc > (ub - lb) / 3.2)
            bad = par.loc[too_big.fillna(False), 'PARNME']
            if len(bad):
                warn('parameters', f'derivative increment larger than a third of the parameter range: {_names(bad)}')
            zero = adj & (v == 0) & (inctyp == 'relative') & (derinclb.fillna(0) == 0 if derinclb is not None else True)
            bad = par.loc[zero.fillna(False), 'PARNME']
            if len(bad):
                warn('parameters', f'initial value 0 with a relative increment and no DERINCLB: the increment will be zero: {_names(bad)}')
    return out


# ---------------------------------------------------------------------- parameter groups
def check_groups(pst: Pst):
    out = []
    err = lambda w, m: out.append(_finding('error', w, m))      # noqa: E731
    warn = lambda w, m: out.append(_finding('warning', w, m))   # noqa: E731
    gp = pst.pargp
    if gp.empty:
        return out
    name = gp['PARGPNME'].astype(str)
    dup = name[name.duplicated()].unique()
    if len(dup):
        err('parameter groups', f'duplicate group names: {_names(dup)}')
    low = lambda c: gp[c].astype(str).str.lower().str.strip()   # noqa: E731
    for col, allowed in (('INCTYP', INCTYP), ('FORCEN', FORCEN), ('DERMTHD', DERMTHD_3PT + DERMTHD_5PT)):
        if col in gp:
            bad = name[~low(col).isin(allowed)]
            if len(bad):
                err('parameter groups', f'{col} must be one of {"/".join(allowed)}: {_names(bad)}')
    if 'FORCEN' in gp and 'DERMTHD' in gp:
        five = low('FORCEN').isin(('switch_5', 'always_5'))
        bad = name[(five & ~low('DERMTHD').isin(DERMTHD_5PT)) | (~five & low('FORCEN').isin(FORCEN)
                                                                  & ~low('DERMTHD').isin(DERMTHD_3PT))]
        if len(bad):
            err('parameter groups', 'DERMTHD must be parabolic/best_fit/outside_pts with switch/always_2/always_3, '
                                    f'minvar/maxprec with switch_5/always_5: {_names(bad)}')
    if 'DERINCLB' in gp:
        bad = name[(_num(gp['DERINCLB']) < 0).fillna(False)]
        if len(bad):
            err('parameter groups', f'DERINCLB must not be negative: {_names(bad)}')
    if 'DERINC' in gp:
        bad = name[(_num(gp['DERINC']) <= 0).fillna(False)]
        if len(bad):
            err('parameter groups', f'DERINC must be positive: {_names(bad)}')
        high = name[(low('INCTYP') == 'relative') & (_num(gp['DERINC']) > 0.5)] if 'INCTYP' in gp else []
        if len(high):
            warn('parameter groups', f'relative DERINC unusually high (> 0.5): {_names(high)}')
    if 'SPLITTHRESH' in gp:
        bad = name[(_num(gp['SPLITTHRESH']) < 0).fillna(False)]
        if len(bad):
            err('parameter groups', f'SPLITTHRESH must not be negative: {_names(bad)}')
    if 'SPLITRELDIFF' in gp:
        bad = name[(_num(gp['SPLITRELDIFF']) <= 0).fillna(False)]
        if len(bad):
            err('parameter groups', f'SPLITRELDIFF must be positive: {_names(bad)}')
    if 'SPLITACTION' in gp:
        bad = name[gp['SPLITACTION'].notna() & ~low('SPLITACTION').isin(SPLITACTION)]
        if len(bad):
            err('parameter groups', f'SPLITACTION must be smaller/zero/previous: {_names(bad)}')
    # what the parameters make of the groups
    par = pst.par
    if len(par):
        trans = par['PARTRANS'].astype(str).str.lower()
        by_group = par.groupby(par['PARGP'].astype(str).str.lower())
        dead = [g for g, part in by_group if not trans[part.index].isin(ADJUSTABLE).any()]
        if dead:
            warn('parameter groups', f'all parameters fixed or tied: {_names(dead)}')
        if 'INCTYP' in gp:
            inctyp = dict(zip(name.str.lower(), low('INCTYP')))
            mixed = [g for g, part in by_group if (trans[part.index] == 'log').any() and inctyp.get(g) not in (None, 'relative')]
            if mixed:
                warn('parameter groups', f'log-transformed parameters with a non-relative increment type: {_names(mixed)}')
    return out


# ---------------------------------------------------------------------- observations, prior information
def check_observations(pst: Pst):
    out = []
    err = lambda w, m: out.append(_finding('error', w, m))      # noqa: E731
    warn = lambda w, m: out.append(_finding('warning', w, m))   # noqa: E731
    obs = pst.obs
    if obs.empty:
        return out
    if (obs['OBSNME'].astype(str).str.lower() == 'dum').any():
        err('observations', '"dum" is not allowed as an observation name (it is the dummy instruction name)')
    w = _num(obs['WEIGHT']).fillna(0)
    silent = [g for g, part in obs.groupby(obs['OBGNME'].astype(str).str.lower()) if (w[part.index] <= 0).all()]
    if silent and len(silent) < obs['OBGNME'].nunique():        # makePst's own note; pestchek is silent on this
        out.append(_finding('info', 'observations', f'every observation has zero weight in groups: {_names(silent)}'))
    unused = [g for g in pst.obsgp_order if g not in set(pst.obsgp)]
    if unused:
        warn('observation groups', f'no observations or prior information belong to observation groups: '
                                   f'{_names(unused)} (dropped on write)')
    groups = set(obs['OBGNME'].astype(str).str.lower()) | set(pst.prior['OBGNME'].astype(str).str.lower())
    if pst.pestmode == 'regularisation' and not any(g.startswith('regul') for g in groups):
        err('observations', 'regularisation mode needs at least one observation group whose name starts with "regul"')
    if pst.pestmode == 'prediction':
        n = int((obs['OBGNME'].astype(str).str.lower() == 'predict').sum())
        if n != 1:
            err('observations', f'prediction mode needs exactly one observation in group "predict" ({n} found)')
    return out


def check_prior(pst: Pst):
    out = []
    err = lambda w, m: out.append(_finding('error', w, m))      # noqa: E731
    prior = pst.prior
    if prior.empty:
        return out
    label = prior['PINME'].astype(str)
    bad = label[(_num(prior['WEIGHT']) < 0).fillna(False)]
    if len(bad):
        err('prior information', f'weight must not be negative: {_names(bad)}')
    bad = label[label.str.len() > 20]
    if len(bad):
        err('prior information', f'labels longer than 20 characters: {_names(bad)}')
    clash = label[label.str.lower().isin(set(pst.obs['OBSNME'].astype(str).str.lower()))]
    if len(clash):
        err('prior information', f'label is also an observation name: {_names(clash)}')
    bad = label[prior['OBGNME'].astype(str).str.lower() == 'predict']
    if len(bad):
        err('prior information', f'prior information cannot belong to group "predict": {_names(bad)}')
    for r in prior.itertuples():
        refs = equation_params(str(r.EQ))
        if len(refs) != len(set(refs)):
            err(f'prior {r.PINME}', 'a parameter is referenced more than once in the equation')
        lhs = str(r.EQ).split('=', 1)[0]
        factors = re.findall(r'(?<![\w.])([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*\*', lhs)
        if factors and all(float(f) == 0 for f in factors):
            err(f'prior {r.PINME}', 'every parameter factor is zero')
    return out


# ---------------------------------------------------------------------- control variables
def _between(lo, hi, lo_incl=True, hi_incl=True):
    return lambda x: (x >= lo if lo_incl else x > lo) and (x <= hi if hi_incl else x < hi)


CONTROL_RULES = [
    # (variable, test on the numeric value, message)
    ('rlambda1', lambda x: x >= 0, 'must not be negative'),
    ('rlamfac', lambda x: abs(x) > 1, 'must have an absolute value greater than one'),
    ('phiratsuf', _between(0, 1), 'must be between zero and one'),
    ('phiredlam', _between(0, 1), 'must be between zero and one'),
    ('jacupdate', lambda x: x >= 0, 'must not be negative'),
    ('facparmax', lambda x: x > 1, 'must be greater than one'),
    ('facorig', _between(0, 1, lo_incl=False), 'must be greater than zero and at most one'),
    ('iboundstick', lambda x: x >= 0, 'must not be negative'),
    ('upvecbend', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('phiredswh', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('noptswitch', lambda x: x >= 1, 'must be 1 or greater'),
    ('noptmax', lambda x: x >= -2, 'must be -2 or greater'),
    ('phiredstp', _between(0, 1), 'must be between zero and one'),
    ('nphistp', lambda x: x >= 2, 'must be 2 or greater'),
    ('nphinored', lambda x: x >= 1, 'must be 1 or greater'),
    ('relparstp', lambda x: x > 0, 'must be greater than zero'),
    ('nrelpar', lambda x: x >= 1, 'must be 1 or greater'),
    ('lastrun', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('icov', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('icor', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('ieig', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('ires', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('jacfile', lambda x: x in (-1, 0, 1, 2), 'must be 0, 1, -1 or 2'),
    ('messfile', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('numcom', lambda x: x >= 1, 'must be 1 or greater'),
    ('maxcompdim', lambda x: x >= 0, 'must not be negative'),
    ('derzerolim', lambda x: x >= 0, 'must not be negative'),
    ('run_slow_fac', lambda x: x >= 1.2, 'must be 1.2 or greater'),
    ('run_abandon_fac', lambda x: x == 0 or x >= 1.2, 'must be zero or 1.2 or greater'),
    ('win_mrun_hours', lambda x: x >= 0, 'must be zero or greater'),
    ('uptestmin', _between(3, 70), 'must be between 3 and 70'),
    ('uptestlim', _between(3, 150), 'must be between 3 and 150'),
    ('jcowarnthresh', lambda x: x >= 0, 'must be zero or greater'),
    ('jcozerothresh', lambda x: x >= 0, 'must be zero or greater'),
    ('zerosenval', lambda x: abs(x) < 1e30, 'must have an absolute value less than 1E30'),
    ('hardstophours', lambda x: x > 0, 'must be greater than zero'),
    ('softstophours', lambda x: x > 0, 'must be greater than zero'),
    ('reg2measrat', _between(0, 1, True, False), 'must be zero or more and less than one'),
    ('svdmode', lambda x: x in (0, 1, 2), 'must be 0, 1 or 2'),
    ('maxsing', lambda x: x > 0, 'must be greater than zero'),
    ('eigthresh', _between(0, 1, True, False), 'must be zero or more and less than one'),
    ('eigwrite', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('lsqrmode', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('lsqr_atol', lambda x: x >= 0, 'must not be negative'),
    ('lsqr_btol', lambda x: x >= 0, 'must not be negative'),
    ('lsqr_conlim', lambda x: x >= 0, 'must not be negative'),
    ('lsqr_itnlim', lambda x: x > 0, 'must be greater than zero'),
    ('lsqrwrite', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('phimlim', lambda x: x > 0, 'must be greater than zero'),
    ('fracphim', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('wfinit', lambda x: x > 0, 'must be greater than zero'),
    ('wfmin', lambda x: x > 0, 'must be greater than zero'),
    ('wffac', lambda x: x > 1, 'must be greater than 1'),
    ('wftol', lambda x: x > 0, 'must be greater than zero'),
    ('iregadj', lambda x: x in (0, 1, 2, 3, 4, 5), 'must be 0, 1, 2, 3, 4 or 5'),
    ('noptregadj', lambda x: x > 0, 'must be greater than zero'),
    ('regweightrat', lambda x: abs(x) > 1, 'must have an absolute value greater than 1'),
    ('regsingthresh', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('maxaui', lambda x: x > 0, 'must be greater than zero'),
    ('noauiphirat', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('auirestitn', lambda x: x >= 0 and x != 1, 'must be zero or greater than one'),
    ('auisensrat', lambda x: x >= 1, 'must be one or greater'),
    ('auiholdmaxchg', lambda x: x in (0, 1), 'must be 0 or 1'),
    ('auinumfree', lambda x: x > 0, 'must be greater than zero'),
    ('auiphiratsuf', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('auiphirataccept', _between(0, 1, False, False), 'must be greater than zero and less than one'),
    ('nauinoaccept', lambda x: x > 0, 'must be greater than zero'),
]


def check_control(pst: Pst):
    out = []
    err = lambda m: out.append(_finding('error', 'control data', m))      # noqa: E731
    warn = lambda m: out.append(_finding('warning', 'control data', m))   # noqa: E731
    ctl = effective_control(pst)

    def num(k):
        try:
            return float(fmt(ctl[k]).lower().replace('d', 'e')) if k in ctl and fmt(ctl[k]) != '' else None
        except (TypeError, ValueError):
            return None

    # what PEST can read at all: a number where it expects one, a word it knows where it expects a word
    flags = {k: sec.flags[k] for sec in ALL_SECTIONS for k in sec.flags}
    for k, v in ctl.items():
        text = fmt(v)
        if k not in FIELD_SECTION or text == '' or k == 'pestmode':
            continue
        if k in INTEGER and not _INT.match(text):
            err(f'{k.upper()} must be an integer (is "{text}")')
        elif k in REAL and not _REAL.match(text):
            err(f'{k.upper()} must be a number (is "{text}")')
        elif k == 'obsreref' and text.lower().startswith('obsreref_'):
            if not re.fullmatch(r'obsreref_0*[1-9]\d*', text.lower()):
                err(f'OBSREREF: only a positive integer (seconds) can follow "obsreref_" (is "{text}")')
        elif k in flags and text.lower() not in flags[k]:
            err(f'{k.upper()} must be {" or ".join(sorted(flags[k]))} (is "{text}")')
        elif k == 'absparmax':
            for tok in text.split():
                m = re.fullmatch(r'absparmax\((\d+)\)=(\S+)', tok.lower())
                if not m or not _REAL.match(m.group(2)):
                    err(f'ABSPARMAX must be given as absparmax(n)=value (is "{tok}")')
                elif not 1 <= int(m.group(1)) <= MAXABSPARMAX:
                    err(f'ABSPARMAX(n): n must be 1 to {MAXABSPARMAX} (is "{tok}")')

    for k, ok, msg in CONTROL_RULES:
        x = num(k)
        if x is not None and not ok(x):
            err(f'{k.upper()} {msg} (is {fmt(ctl[k])})')
    n = {k: num(k) for k in ('relparstp', 'relparmax', 'uptestmin', 'uptestlim', 'phimlim', 'phimaccept',
                             'wfinit', 'wfmin', 'wfmax', 'noptmax', 'numlam', 'rlambda1', 'maxcompdim',
                             'jacupdate', 'auiphiratsuf', 'auiphirataccept', 'lsqrmode', 'svdmode')}
    if None not in (n['relparstp'], n['relparmax']) and n['relparstp'] >= n['relparmax']:
        err('RELPARSTP must be less than RELPARMAX')
    if None not in (n['uptestmin'], n['uptestlim']) and n['uptestmin'] > n['uptestlim']:
        err('UPTESTMIN must not exceed UPTESTLIM')
    if num('hardstophours') is not None and num('softstophours') is not None:
        err('a value can be supplied for either HARDSTOPHOURS or SOFTSTOPHOURS, but not for both')
    if num('jcowarnthresh') and num('jcozerothresh') and num('jcozerothresh') <= num('jcowarnthresh'):
        err('JCOZEROTHRESH must exceed JCOWARNTHRESH when both are positive')
    reref = str(ctl.get('obsreref', '')).lower().startswith('obsreref')
    if str(ctl.get('orr_not_first', '')).lower() == 'orr_not_first' and not reref:
        err('"orr_not_first" needs observation re-referencing ("obsreref") on the same line')
    if num('zerosenval') is not None and (num('numcom') or 1) <= 1 and not reref:
        err('ZEROSENVAL must not be supplied unless PEST uses multiple model commands for derivatives '
            '(NUMCOM > 1) or observation re-referencing')
    if pst.pestmode == 'regularisation':
        if None not in (n['phimlim'], n['phimaccept']):
            if n['phimaccept'] <= n['phimlim']:
                err('PHIMACCEPT must be greater than PHIMLIM')
            elif n['phimaccept'] >= 1.2 * n['phimlim']:
                err('PHIMACCEPT must be less than 1.2 times PHIMLIM')
        if None not in (n['wfinit'], n['wfmin'], n['wfmax']):
            if not n['wfmin'] < n['wfmax']:
                err('WFMIN must be less than WFMAX')
            if not n['wfmin'] <= n['wfinit'] <= n['wfmax']:
                err('WFINIT must lie between WFMIN and WFMAX')
    if n['rlambda1'] == 0 and n['numlam'] not in (None, 1):
        err('if RLAMBDA1 is zero, NUMLAM must be one')
    if n['maxcompdim'] and n['jacupdate']:
        err('JACUPDATE must be zero when MAXCOMPDIM activates compressed Jacobian storage')
    if None not in (n['auiphiratsuf'], n['auiphirataccept']) and n['auiphirataccept'] <= n['auiphiratsuf']:
        err('AUIPHIRATACCEPT must be larger than AUIPHIRATSUF')
    aui = str(ctl.get('doaui', '')).lower() == 'aui'
    if aui and pst.pestmode in ('regularisation', 'prediction'):
        err('automatic user intervention is not permitted in regularisation or predictive analysis mode')
    if aui and n['svdmode']:
        err('singular value decomposition must not be activated together with automatic user intervention')
    if n['lsqrmode'] and n['svdmode']:
        err('LSQR and SVD must not both be activated')
    if str(ctl.get('lamforgive', '')).lower() == 'lamforgive' and pst.pestmode == 'prediction':
        err('LAMFORGIVE must be "nolamforgive" in predictive analysis mode')
    if n['noptmax'] in (-1, -2) and str(ctl.get('jcosave', '')).lower() == 'nojcosave':
        err('with NOPTMAX -1 or -2, JCOSAVE must not be "nojcosave"')
    if pst.pestmode in ('regularisation', 'prediction', 'pareto') and pst.nobsgp <= 1:
        err(f'{pst.pestmode} mode needs more than one observation group')
    if n['noptmax'] == 0:
        warn('NOPTMAX is 0: one model run, no optimisation')
    maxsing = num('maxsing')
    nadj = int(pst.par['PARTRANS'].astype(str).str.lower().isin(ADJUSTABLE).sum()) if len(pst.par) else 0
    if n['svdmode'] and maxsing is not None and nadj and maxsing > nadj:
        warn(f'MAXSING ({fmt(ctl["maxsing"])}) exceeds the number of adjustable parameters ({nadj})')
    if nadj > 300 and (not n['lsqrmode'] or n['svdmode'] in (0, 1)) and any(num(k) for k in ('icov', 'icor', 'ieig')):
        warn(f'{nadj} adjustable parameters: setting ICOV, ICOR and IEIG to 0 saves significant memory')
    hp = [k for k in HP_ONLY if k in ctl and fmt(ctl[k]) != '' and str(ctl[k]).lower() != f'no{k}']
    if hp:
        out.append(_finding('info', 'control data',
                            f'PEST_HP-only variables (plain PEST needs /hpstart): {", ".join(k.upper() for k in hp)}'))
    sensitive = sorted({k.lower() for k, _ in pst.pestpp} & set(PESTPP_VERSIONED))
    for k in sensitive:
        out.append(_finding('info', 'pestpp options',
                            f'++{k} is not accepted by every PEST++ build ({PESTPP_VERSIONED[k]}); a binary that '
                            f'does not know it stops with "the following \'++\' args were not accepted"'))
    return out


def check_unknown_control(pst: Pst):
    """Control tokens the schema does not know: a warning each, kept as written (as for an unknown `++` option)."""
    out = []
    for name, (section, n) in getattr(pst, 'control_line', {}).items():
        token = extra_token(name, pst.control.get(name))
        close = difflib.get_close_matches(token_name(name), list(FIELD_SECTION), n=1, cutoff=0.75)
        out.append(_finding('warning', f'{section} line {n}',
                            f'"{token}" is not a control variable of PEST {PESTCHEK_SOURCE_VERSION}'
                            + (f'; did you mean "{close[0]}"?' if close else '')
                            + ' (kept as written; a newer PEST_HP may know it)'))
    return out


def check_extra(pst: Pst):
    """Every rule in this module."""
    return (check_control(pst) + check_unknown_control(pst) + check_groups(pst) + check_parameters(pst)
            + check_observations(pst) + check_prior(pst))
