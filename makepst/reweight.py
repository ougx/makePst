"""Observation-weight adjustment helpers.

The command layer deliberately stays thin: this module owns the arithmetic for
equalising group contributions, applying requested group shares, scaling named
groups, and respecting optional weight bounds.
"""
import math

import numpy as np
import pandas as pd


REPORT_COLUMNS = [
    'OBGNME', 'N_OBS', 'N_WEIGHTED_OLD', 'N_WEIGHTED_NEW',
    'OLD_WEIGHT_MIN', 'OLD_WEIGHT_MAX', 'NEW_WEIGHT_MIN', 'NEW_WEIGHT_MAX',
    'OLD_PHI', 'OLD_PCT', 'TARGET_PCT', 'BASE_FACTOR',
    'N_CLIPPED_LOW', 'N_CLIPPED_HIGH', 'N_REDUCED', 'NEW_PHI', 'NEW_PCT',
]


def _number(value, label):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be numeric') from None
    if not np.isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


def _clip_bounds(clip):
    if clip is None:
        return None
    if len(clip) != 2:
        raise ValueError('--clip expects MIN MAX')
    lo, hi = (_number(x, '--clip bound') for x in clip)
    if lo < 0 or hi < lo:
        raise ValueError('--clip requires 0 <= MIN <= MAX')
    return lo, hi


def _group_key(name):
    return str(name).strip().lower()


def is_measurement_group(group, pst):
    """False for covariance-backed, ``predict`` and ``regul*`` groups."""
    group = _group_key(group)
    covariance = {_group_key(g) for g in getattr(pst, 'obs_cov', {})}
    return not (group in covariance or group == 'predict' or group.startswith('regul'))


def _obs_frame(pst):
    """The PST's observation weights and groups, indexed by lower-case observation name."""
    obs = pst.obs
    frame = pd.DataFrame({
        'WEIGHT': pd.to_numeric(obs['WEIGHT'], errors='coerce').to_numpy(dtype=float),
        'OBGNME': obs['OBGNME'].map(_group_key).to_numpy(),
    }, index=pd.Index(obs['OBSNME'].astype(str).str.strip().str.lower(), name='OBSNME'))
    weight = frame['WEIGHT']
    if not np.isfinite(weight).all() or (weight < 0).any():
        raise ValueError('PST observation weights must be finite and nonnegative')
    return frame


def _join_residuals(frame, residuals, groups):
    """Add a RESIDUAL column; only positively weighted observations in `groups` need one."""
    if residuals is None or 'RESIDUAL' not in residuals:
        raise ValueError('a residual source with a RESIDUAL column is required')
    res = residuals.copy()
    if res.index.name is None and 'NAME' in res.columns:
        res = res.set_index('NAME')
    res.index = res.index.astype(str).str.strip().str.lower()
    if res.index.has_duplicates:
        raise ValueError('residual source contains duplicate observation names')
    frame = frame.join(pd.to_numeric(res['RESIDUAL'], errors='coerce'), how='left')
    needed = frame['OBGNME'].isin(groups) & (frame['WEIGHT'] > 0)
    bad = needed & ~np.isfinite(frame['RESIDUAL'])
    if bad.any():
        raise ValueError('missing or nonfinite residuals for observations: '
                         + ', '.join(frame.index[bad][:6]))
    return frame


def _check_groups(frame, groups, pst):
    unknown = sorted(set(groups) - set(frame['OBGNME']))
    if unknown:
        raise ValueError('unknown observation groups: ' + ', '.join(unknown))
    covariance = {_group_key(g) for g in getattr(pst, 'obs_cov', {})}
    bad = sorted(set(groups) & covariance)
    if bad:
        raise ValueError('covariance-backed groups cannot be reweighted: ' + ', '.join(bad))
    special = sorted(g for g in groups if not is_measurement_group(g, pst))
    if special:
        raise ValueError('groups are not measurement groups: ' + ', '.join(special))


def _phi(weights, residuals):
    return float(np.square(weights * residuals).sum())


def _clip(raw, positive, clip):
    """Bound the `positive` (originally nonzero) weights to `clip`; the rest are zero."""
    if clip is None:
        return raw, 0, 0
    lo, hi = clip
    out = raw.clip(lower=lo, upper=hi).where(positive, 0.0)
    low = int(((raw < lo) & positive).sum())
    high = int(((raw > hi) & positive).sum())
    return out, low, high


def _apply_clip(weights, factor, clip):
    """Scale positive weights and preserve original zero weights."""
    return _clip(weights * factor, weights > 0, clip)


def _bounded_factor(weights, residuals, target, clip):
    """Find a factor whose clipped weighted residuals reach target phi."""
    if target == 0:
        return 0.0
    lo, hi = clip
    positive = weights > 0
    base = weights.loc[positive]
    resid = residuals.loc[positive]

    def value(f):
        values = (base * f).clip(lower=lo, upper=hi)
        return _phi(values, resid)

    minimum = value(0.0)
    maximum = value(float('inf'))
    tol = max(1e-12, abs(target) * 1e-10)
    if target < minimum - tol or target > maximum + tol:
        raise ValueError(f'target phi {target:g} is not achievable with --clip {lo:g} {hi:g}; '
                         f'achievable range is {minimum:g} to {maximum:g}')
    if abs(target - minimum) <= tol:
        return 0.0
    if abs(target - maximum) <= tol:
        # Any sufficiently large value gives the same clipped result.  This
        # finite value also makes the report useful to a human.
        return max(1.0, hi / float(base.min()))

    low, high = 0.0, 1.0
    while value(high) < target and high < 1e300:
        high *= 2.0
    for _ in range(120):
        middle = (low + high) / 2.0
        if value(middle) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _report_rows(frame, before, new_weights, selected, targets=None, factors=None, clips=None):
    targets = targets or {}
    factors = factors or {}
    clips = clips or {}
    after = _summaries(frame, new_weights, selected)
    lowered = new_weights < frame['WEIGHT']
    old_total = sum(before[g]['phi'] for g in selected)
    new_total = sum(after[g]['phi'] for g in selected)
    rows = []
    for g in selected:
        b, a = before[g], after[g]
        rows.append({
            'OBGNME': g,
            'N_OBS': b['n_obs'],
            'N_WEIGHTED_OLD': b['n_weighted'],
            'N_WEIGHTED_NEW': a['n_weighted'],
            'OLD_WEIGHT_MIN': b['min_weight'],
            'OLD_WEIGHT_MAX': b['max_weight'],
            'NEW_WEIGHT_MIN': a['min_weight'],
            'NEW_WEIGHT_MAX': a['max_weight'],
            'OLD_PHI': b['phi'],
            'OLD_PCT': 100.0 * b['phi'] / old_total if old_total else np.nan,
            'TARGET_PCT': targets.get(g, np.nan),
            'BASE_FACTOR': factors.get(g, np.nan),
            'N_CLIPPED_LOW': clips.get(g, (0, 0))[0],
            'N_CLIPPED_HIGH': clips.get(g, (0, 0))[1],
            'N_REDUCED': int((lowered & (frame['OBGNME'] == g)).sum()),
            'NEW_PHI': a['phi'],
            'NEW_PCT': 100.0 * a['phi'] / new_total if new_total else np.nan,
        })
    return pd.DataFrame(rows, columns=REPORT_COLUMNS)


def _summaries(frame, weights, groups):
    """Per-group counts, weight range and phi (NaN without a RESIDUAL column)."""
    out = {}
    for group in groups:
        part = frame['OBGNME'] == group
        w = weights.loc[part]
        positive = w > 0
        out[group] = {
            'n_obs': int(part.sum()),
            'n_weighted': int(positive.sum()),
            'min_weight': float(w[positive].min()) if positive.any() else 0.0,
            'max_weight': float(w[positive].max()) if positive.any() else 0.0,
            'phi': _phi(w[positive], frame.loc[part, 'RESIDUAL'][positive])
                   if 'RESIDUAL' in frame else np.nan,
        }
    return out




def _eligible(pst, residuals, skip_zero_phi):
    """Measurement groups with positive weights, joined to residuals.

    Returns ``(frame, before, groups, skipped)``; `skipped` maps each passed-over group to the reason.
    """
    frame = _obs_frame(pst)
    skipped, groups = {}, []
    for group in dict.fromkeys(frame['OBGNME']):
        if not is_measurement_group(group, pst):
            skipped[group] = 'not a measurement group'
        elif (frame.loc[frame['OBGNME'] == group, 'WEIGHT'] > 0).any():
            groups.append(group)
    frame = _join_residuals(frame, residuals, groups)
    before = _summaries(frame, frame['WEIGHT'], groups)
    if skip_zero_phi:
        for group in [g for g in groups if before[g]['phi'] <= 0]:
            skipped[group] = 'zero current phi; weights left unchanged'
            groups.remove(group)
    return frame, before, groups, skipped


def _scale_to_targets(frame, target_phi, clip):
    """Scale each group by one factor so its (clipped) phi reaches ``target_phi[group]``."""
    new_weights = frame['WEIGHT'].copy()
    factors, clips = {}, {}
    for group, target in target_phi.items():
        mask = frame['OBGNME'] == group
        w, r = frame.loc[mask, 'WEIGHT'], frame.loc[mask, 'RESIDUAL']
        if target == 0:
            factor = 0.0
            out, low, high = w * 0.0, 0, 0
        else:
            old_phi = _phi(w[w > 0], r[w > 0])
            if old_phi <= 0:
                raise ValueError(f'{group}: positive target cannot be reached from zero current phi')
            factor = math.sqrt(target / old_phi) if clip is None else _bounded_factor(w, r, target, clip)
            out, low, high = _apply_clip(w, factor, clip)
        new_weights.loc[mask] = out.to_numpy()
        factors[group] = factor
        clips[group] = (low, high)
    return new_weights, factors, clips


def equal_shares(pst, residuals):
    """Equal shares for every measurement group that has positive weights and nonzero phi.

    Returns ``(shares, skipped)``; `skipped` maps each passed-over group to the reason.
    """
    _, _, groups, skipped = _eligible(pst, residuals, skip_zero_phi=True)
    return {g: 1.0 for g in groups}, skipped


def balance_weights(pst, residuals, shares, clip=None):
    """Balance selected groups to relative phi shares, preserving their total phi."""
    clip = _clip_bounds(clip)
    shares = {_group_key(g): _number(v, f'share for {g}') for g, v in shares.items()}
    if not shares or not any(v > 0 for v in shares.values()):
        raise ValueError('at least one target share must be positive')
    if any(v < 0 for v in shares.values()):
        raise ValueError('target shares must be nonnegative')
    groups = list(shares)
    frame = _obs_frame(pst)
    _check_groups(frame, groups, pst)
    frame = _join_residuals(frame, residuals, groups)
    before = _summaries(frame, frame['WEIGHT'], groups)
    empty = [g for g in groups if before[g]['n_weighted'] == 0]
    if empty:
        raise ValueError('target groups have no positively weighted observations: ' + ', '.join(empty))
    pool = sum(before[g]['phi'] for g in groups)
    if pool <= 0:
        raise ValueError('selected groups have zero total phi')
    total_share = sum(shares.values())
    target_phi = {g: pool * shares[g] / total_share for g in groups}
    target_pct = {g: 100.0 * shares[g] / total_share for g in groups}
    new_weights, factors, clips = _scale_to_targets(frame, target_phi, clip)
    pst.obs['WEIGHT'] = new_weights.to_numpy()
    return _report_rows(frame, before, new_weights, groups, target_pct, factors, clips)


def discrepancy_weights(pst, residuals, by='group', clip=None):
    """Make weights consistent with the current misfit: each weighted observation contributes about 1.

    ``by='group'`` scales each measurement group by one factor so its phi equals its number of
    weighted observations (as PEST's PWTADJ2); relative weights within a group are kept.
    ``by='obs'`` sets ``w = min(w, 1/|r|)`` per observation, so no observation contributes more
    than 1 and no weight is raised (pyEMU's adjust_weights_discrepancy with its original ceiling);
    observations with a zero residual keep their weight.  Returns ``(report, skipped)``.
    """
    if by not in ('group', 'obs'):
        raise ValueError("discrepancy mode must be 'group' or 'obs'")
    clip = _clip_bounds(clip)
    frame, before, groups, skipped = _eligible(pst, residuals, skip_zero_phi=by == 'group')
    if not groups:
        raise ValueError('no eligible positively weighted observation groups'
                         + (' with nonzero phi' if by == 'group' else ''))
    if by == 'group':
        target_phi = {g: float(before[g]['n_weighted']) for g in groups}
        new_weights, factors, clips = _scale_to_targets(frame, target_phi, clip)
        total = sum(target_phi.values())
        targets = {g: 100.0 * t / total for g, t in target_phi.items()}
    else:
        w, r = frame['WEIGHT'], frame['RESIDUAL'].abs()
        selected = frame['OBGNME'].isin(groups) & (w > 0) & (r > 0)
        ceiling = (1.0 / r).where(selected, w)
        new_weights = w.copy()
        factors, clips, targets = {}, {}, {}
        for group in groups:
            mask = frame['OBGNME'] == group
            out, low, high = _clip(np.minimum(w[mask], ceiling[mask]), w[mask] > 0, clip)
            new_weights.loc[mask] = out.to_numpy()
            clips[group] = (low, high)
    pst.obs['WEIGHT'] = new_weights.to_numpy()
    return _report_rows(frame, before, new_weights, groups, targets, factors, clips), skipped


def scale_weights(pst, factors, clip=None, residuals=None):
    """Multiply weights in named groups, optionally calculating residual phi."""
    clip = _clip_bounds(clip)
    factors = {_group_key(g): _number(v, f'factor for {g}') for g, v in factors.items()}
    if any(v < 0 for v in factors.values()):
        raise ValueError('weight factors must be nonnegative')
    groups = list(factors)
    frame = _obs_frame(pst)
    _check_groups(frame, groups, pst)
    if residuals is not None:
        frame = _join_residuals(frame, residuals, groups)
    before = _summaries(frame, frame['WEIGHT'], groups)
    new_weights = frame['WEIGHT'].copy()
    clips = {}
    for group, factor in factors.items():
        mask = frame['OBGNME'] == group
        out, low, high = _apply_clip(frame.loc[mask, 'WEIGHT'], factor, clip)
        new_weights.loc[mask] = out.to_numpy()
        clips[group] = (low, high)
    pst.obs['WEIGHT'] = new_weights.to_numpy()
    return _report_rows(frame, before, new_weights, groups, factors=factors, clips=clips)
