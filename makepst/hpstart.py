"""Update a PEST_HP accelerator file using named result values.

The .hp file contains instruction metadata that cannot be reconstructed from a
residuals or IES observation file, so an existing file is required as a template.
"""
import os

import numpy as np
import pandas as pd

from .pst import _to_internal, read_obs_ensemble, read_par, read_res


def _values(frame, names, column, source):
    if frame.index.has_duplicates:
        raise ValueError(f'{source} has duplicate names: {frame.index[frame.index.duplicated()].tolist()[:5]}')
    missing = pd.Index(names).difference(frame.index)
    if len(missing):
        raise ValueError(f'{source} is missing {len(missing)} names: {missing[:5].tolist()}')
    values = pd.to_numeric(frame.loc[list(names), column], errors='coerce').to_numpy(dtype='<f8')
    if not np.isfinite(values).all():
        raise ValueError(f'{source} contains non-finite or nonnumeric {column} values')
    return values


def write_hpstart(pst, template, output, *, res=None, obs_csv=None, real=None, par=None):
    """Preserve a template .hp's instruction arrays and replace its reference observations.

    The template and control file must describe the same parameters and observations
    in the same order; the binary file stores counts but not their names.
    """
    if (res is None) == (obs_csv is None):
        raise ValueError('give exactly one of res or obs_csv')
    if res is not None and real is not None and par is None:
        raise ValueError('--real requires an IES observation or parameter ensemble')
    if os.path.abspath(template) == os.path.abspath(output):
        raise ValueError('output must differ from the template .hp file')
    with open(template, 'rb') as f:
        raw = bytearray(f.read())
    if len(raw) < 12:
        raise ValueError('template .hp file is too short')
    npar, nobs, numl = np.frombuffer(raw, dtype='<i4', count=3)
    if npar != pst.npar or nobs != pst.nobs or numl < 0:
        raise ValueError(f'template .hp counts ({npar}, {nobs}, {numl}) do not match '
                         f'control file ({pst.npar} parameters, {pst.nobs} observations)')
    expected = 12 + 8 * (int(npar) + int(nobs)) + 4 * (3 * int(nobs) + int(numl))
    if len(raw) != expected:
        raise ValueError(f'template .hp length is {len(raw)} bytes; expected {expected}')

    if res is not None:
        frame = read_res(res)
        source = res
    else:
        frame = read_obs_ensemble(obs_csv, real)
        source = obs_csv
    modeled = _values(frame, pst.obs['OBSNME'], 'MODELLED', source)
    start = 12 + 8 * int(npar)
    raw[start:start + 8 * int(nobs)] = modeled.tobytes()
    if par is not None:
        frame = read_par(par, real)
        _values(frame, pst.par['PARNME'], 'PARVAL1', par)           # names and numbers are all there
        parameters = _to_internal(frame, pst.par)[1].to_numpy(dtype='<f8')   # through SCALE / OFFSET
        raw[12:start] = parameters.tobytes()
    with open(output, 'wb') as f:
        f.write(raw)
    return {'npar': int(npar), 'nobs': int(nobs), 'numl': int(numl)}
