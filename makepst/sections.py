"""Schemas for the control-style sections of a PEST control file.

Each section is a list of lines, each line a tuple of field names in the
order PEST expects them.  One table drives both rendering (Pst -> text) and
parsing (text -> Pst), so the writer and the reader cannot disagree about
where a field lives.

The fields, including the PEST_HP name=value tokens and flags, are those PEST 17.2's pestchek
(pestchek.F) reads. A token this schema does not know - a PEST_HP variable added after that
release, say - is kept with the line it came from and written back there (see Section.parse).
"""
import math
import re
import warnings

import numpy as np

FLOATFMT = '.11g'

# the PEST release whose pestchek source the control-data schema and the rules in rules.py were read from
PESTCHEK_SOURCE_VERSION = '17.2'

# name=value tokens: PEST finds them anywhere on their line, so they take no positional slot
KEYED = {'run_slow_fac', 'run_abandon_fac', 'win_mrun_hours', 'uptestmin', 'uptestlim',
         'jcowarnthresh', 'jcozerothresh', 'zerosenval', 'hardstophours', 'softstophours', 'reg2measrat'}

# derived from the data tables at write time; never taken from the user
COMPUTED = {'npar', 'nobs', 'npargp', 'nprior', 'nobsgp', 'ntplfle', 'ninsfle', 'pestmode'}

MODES = ('estimation', 'regularisation', 'prediction', 'pareto')


def normalize_mode(mode):
    m = str(mode).strip().lower()
    for full in MODES:
        if m and full.startswith(m[:4]):
            return full
    raise ValueError(f'unknown pestmode {mode!r}; expected one of {MODES}')


def fmt(v):
    """Render a control value as PEST text; '' means 'not set'."""
    if v is None:
        return ''
    if isinstance(v, (bool, np.bool_)):
        return str(int(v))
    if isinstance(v, (int, np.integer)):
        # beyond 11 digits an integer is really a float that Excel rounded (1e15 -> 1000000000000000)
        return str(int(v)) if abs(int(v)) < 10 ** 11 else f'{float(v):{FLOATFMT}}'
    if isinstance(v, (float, np.floating)):
        return '' if math.isnan(v) else f'{float(v):{FLOATFMT}}'
    s = str(v).strip()
    return '' if s.lower() in ('', 'nan', '<na>') else s   # 'none' is a valid PARTRANS


def is_number(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _num(tok):
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok


def _onoff(name):
    return {name, 'no' + name}


def token_name(tok):
    """The variable a control-data token names: 'win_mrun_hours' for 'win_mrun_hours=2', the word itself for a flag."""
    return re.split(r'[=(]', tok, maxsplit=1)[0].lower()


def extra_token(name, value):
    """Text of a kept unknown token: `name=value`, or the bare word when the value is the name (a flag)."""
    s = fmt(value)
    return name if s.lower() == name else f'{name}={s}'


class Section:
    def __init__(self, header, lines, defaults=None, flags=None, comments=None, derive=None):
        self.header = header
        self.lines = lines
        self.defaults = defaults or {}
        self.flags = flags or {}        # field -> accepted text values (identifies the field on parse)
        self.comments = comments or {}  # line index -> trailing '#' comment written after the line
        self.derive = derive            # callable(values) filling values that depend on other values

    @property
    def name(self):
        return self.header[2:]

    @property
    def fields(self):
        return [k for line in self.lines for k in line]

    def values(self, user):
        """Defaults overlaid with the user's values for this section's fields."""
        v = dict(self.defaults)
        for k in self.fields:
            if k in user and fmt(user[k]) != '':
                v[k] = user[k]
        if self.derive:
            self.derive(v)
        return v

    def flag_of(self, keys, low):
        """The field among `keys` that the word `low` sets, identified by its text, or None."""
        for k in keys:
            if low in self.flags.get(k, ()) or (k == 'obsreref' and re.fullmatch(r'obsreref_\d+', low)):
                return k
        return None

    def render(self, user, extra=None):
        """Section text; `extra` maps a line number (1-based) to kept unknown tokens written at its end."""
        v = self.values(user)
        extra = extra or {}
        out = [self.header]
        for i, keys in enumerate(self.lines):
            fields = []
            missing = []
            for k in keys:
                s = fmt(v.get(k))
                # Flags and name=value tokens identify themselves; all other
                # values need every earlier positional slot on this line.
                named = k in KEYED or (s != '' and all(
                    '=' in tok and re.split(r'[=(]', tok, maxsplit=1)[0].lower() == k
                    for tok in s.split()))
                positional = k not in self.flags and not named
                if s == '':
                    if positional:
                        missing.append(k)
                    continue
                if positional and missing:
                    raise ValueError(
                        f'{self.header}, line {i + 1}: {k} requires preceding positional '
                        f'field(s) {", ".join(missing)}; supply explicit values to '
                        'prevent settings shifting into the wrong positions')
                if k in KEYED:
                    s = f'{k}={s}'
                fields.append(f'{s:<10}')
            fields += extra.get(i + 1, [])
            line = ' '.join(fields).rstrip()
            if i in self.comments:
                line = f'{line:<80}  {self.comments[i]}'
            out.append(line)
        for n in sorted(k for k in extra if k > len(self.lines)):
            out.append(' '.join(extra[n]))
        return '\n'.join(out) + '\n'

    def parse(self, text_lines):
        """Parse the section body (header excluded) into a {field: value} dict.

        Tokens are matched, in order of preference: name=value tokens, text
        flags identified by their value, then numeric tokens positionally.
        A token none of these explains is kept, not dropped: '_extra' maps it,
        as (name, value), to its line number (1-based), so the writer can put it
        back where it was. A newer PEST_HP than this schema may know it.
        """
        values, extra = {}, {}
        where = {k: n for n, keys in enumerate(self.lines, 1) for k in keys}
        for i, text in enumerate(text_lines):
            keys = self.lines[i] if i < len(self.lines) else ()
            # PEST_HP accepts spaces around the '=' of a name=value token ('run_slow_fac = 3')
            tokens = re.sub(r'\s*=\s*', '=', text.split('#', 1)[0]).split()
            positional = [k for k in keys if k not in self.flags and k not in KEYED]
            for tok in tokens:
                low = tok.lower()
                if '=' in tok:
                    base = token_name(tok)
                    if base in KEYED:
                        if base in values:
                            warnings.warn(f'{self.header}: {base.upper()} appears twice; the last value is kept')
                        elif base not in keys:
                            warnings.warn(f'{self.header}: {tok!r} belongs on line {where[base]}, not line {i + 1}; '
                                          f'it is written there')
                        values[base] = _num(tok.split('=', 1)[1])
                    elif base in keys:
                        values[base] = (values.get(base, '') + ' ' + tok).strip()
                    else:
                        name, value = tok.split('=', 1)
                        extra.setdefault(i + 1, []).append((name.lower(), _num(value)))
                    if base in positional:
                        positional.remove(base)
                    continue
                flag = self.flag_of(keys, low)
                if flag is not None:
                    values[flag] = low
                    continue
                if positional:
                    values[positional.pop(0)] = _num(tok)
                else:
                    extra.setdefault(i + 1, []).append((low, _num(tok)))
        if extra:
            values['_extra'] = extra
        return values


CONTROL = Section(
    '* control data',
    lines=[
        ('rstfle', 'pestmode'),
        ('npar', 'nobs', 'npargp', 'nprior', 'nobsgp', 'maxcompdim', 'derzerolim'),
        ('ntplfle', 'ninsfle', 'precis', 'dpoint', 'numcom', 'jacfile', 'messfile', 'obsreref',
         'orr_not_first'),
        ('rlambda1', 'rlamfac', 'phiratsuf', 'phiredlam', 'numlam', 'jacupdate',
         'lamforgive', 'derforgive', 'run_slow_fac', 'run_abandon_fac', 'win_mrun_hours', 'uptestmin', 'uptestlim'),
        ('relparmax', 'facparmax', 'facorig', 'iboundstick', 'upvecbend', 'absparmax'),
        ('phiredswh', 'noptswitch', 'splitswh', 'doaui', 'dosenreuse', 'boundscale',
         'jcowarnthresh', 'jcozerothresh', 'zerosenval'),
        ('noptmax', 'phiredstp', 'nphistp', 'nphinored', 'relparstp', 'nrelpar',
         'phistopthresh', 'lastrun', 'phiabandon', 'hardstophours', 'softstophours'),
        ('icov', 'icor', 'ieig', 'ires', 'jcosave', 'verboserec', 'jcosaveitn',
         'reisaveitn', 'parsaveitn', 'parsaverun', 'rrfsave'),
    ],
    defaults={
        'rstfle': 'restart',
        'precis': 'single', 'dpoint': 'point', 'numcom': 1, 'jacfile': 0, 'messfile': 0,
        'rlambda1': 10, 'rlamfac': 2.0, 'phiratsuf': 0.3, 'phiredlam': 0.03, 'numlam': 10,
        'relparmax': 0.8, 'facparmax': 5.0, 'facorig': 0.001,
        'phiredswh': 0.1,
        # noptmax 0: one model run, no Jacobian; -1/-2: Jacobian only (see PEST manual)
        'noptmax': 5, 'phiredstp': 0.005, 'nphistp': 4, 'nphinored': 4,
        'relparstp': 0.005, 'nrelpar': 4, 'phistopthresh': 0, 'lastrun': 1, 'phiabandon': -1,
        'icov': 1, 'icor': 1, 'ieig': 1, 'verboserec': 'verboserec', 'parsaveitn': 'parsaveitn',
    },
    flags={
        'rstfle': _onoff('restart'),
        'pestmode': set(MODES) | {'regularization'},
        'precis': {'single', 'double'},
        'dpoint': _onoff('point'),
        'obsreref': _onoff('obsreref'),
        'orr_not_first': {'orr_not_first'},
        'lamforgive': _onoff('lamforgive'),
        'derforgive': _onoff('derforgive'),
        'doaui': _onoff('aui'),
        'dosenreuse': _onoff('senreuse'),
        'boundscale': _onoff('boundscale'),
        'jcosave': _onoff('jcosave'),
        'verboserec': _onoff('verboserec'),
        'jcosaveitn': _onoff('jcosaveitn'),
        'reisaveitn': _onoff('reisaveitn'),
        'parsaveitn': _onoff('parsaveitn'),
        'parsaverun': _onoff('parsaverun'),
        'rrfsave': _onoff('rrfsave'),
    },
    comments={
        1: '# npar nobs npargp nprior nobsgp [maxcompdim] [derzerolim]',
        6: '# noptmax phiredstp nphistp nphinored relparstp nrelpar [phistopthresh] [lastrun] [phiabandon]',
    },
)

SVD = Section(
    '* singular value decomposition',
    lines=[('svdmode',), ('maxsing', 'eigthresh'), ('eigwrite',)],
    # maxsing: singular values kept before truncation; eigthresh >= 5e-7 for numerical stability
    defaults={'svdmode': 1, 'maxsing': 10000, 'eigthresh': 5e-7, 'eigwrite': 0},
)

LSQR = Section(
    '* lsqr',
    lines=[('lsqrmode',), ('lsqr_atol', 'lsqr_btol', 'lsqr_conlim', 'lsqr_itnlim'), ('lsqrwrite',)],
    defaults={'lsqrmode': 1, 'lsqr_atol': 1e-4, 'lsqr_btol': 1e-4, 'lsqr_conlim': 1000, 'lsqrwrite': 0},
)

AUI = Section(
    '* automatic user intervention',
    lines=[
        ('maxaui', 'auistartopt', 'noauiphirat', 'auirestitn'),
        ('auisensrat', 'auiholdmaxchg', 'auinumfree'),
        ('auiphiratsuf', 'auiphirataccept', 'nauinoaccept'),
    ],
    defaults={'auistartopt': 2, 'noauiphirat': 0.89, 'auirestitn': 3,
              'auisensrat': 5.0, 'auiholdmaxchg': 1, 'auinumfree': 3,
              'auiphiratsuf': 0.8, 'auiphirataccept': 0.95, 'nauinoaccept': 3},
)

SVDA = Section(
    '* svd assist',
    lines=[('basepestfile',), ('basejacfile',),
           ('svda_mulbpa', 'svda_scaladj', 'svda_extsuper', 'svda_supdercalc', 'svda_par_excl')],
    defaults={'svda_mulbpa': 1, 'svda_scaladj': 1, 'svda_extsuper': 2, 'svda_supdercalc': 1, 'svda_par_excl': 0},
)


def _derive_regul(v):
    if fmt(v.get('phimaccept')) == '':
        v['phimaccept'] = 1.05 * float(v['phimlim'])


REGUL = Section(
    '* regularisation',
    lines=[
        ('phimlim', 'phimaccept', 'fracphim', 'memsave', 'reg2measrat'),
        ('wfinit', 'wfmin', 'wfmax', 'linreg', 'regcontinue'),
        ('wffac', 'wftol', 'iregadj', 'noptregadj', 'regweightrat', 'regsingthresh'),
    ],
    defaults={'phimlim': 0.1, 'fracphim': 0.3,
              'wfinit': 1.0, 'wfmin': 1e-15, 'wfmax': 1e15,
              'wffac': 1.3, 'wftol': 1e-3, 'iregadj': 1},
    flags={'memsave': _onoff('memsave'), 'linreg': _onoff('linreg'), 'regcontinue': _onoff('regcontinue')},
    derive=_derive_regul,
)

ALL_SECTIONS = (CONTROL, SVD, LSQR, AUI, SVDA, REGUL)

# every control-style section keyed by its lower-case header text
SECTIONS = {s.name: s for s in ALL_SECTIONS}
SECTIONS['regularization'] = REGUL

# all fields the CONTROL sheet may set, in file order, with the section that owns each
FIELD_SECTION = {k: s for s in ALL_SECTIONS for k in s.fields}
