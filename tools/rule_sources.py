"""Keep `makepst validate` in step with the programs its rules were read from.

    python tools/rule_sources.py pestpp [--ref develop] [--source DIR] [--write]
    python tools/rule_sources.py pestchek --source DIR [--write]

Each subcommand reads the upstream source, extracts what makePst's rules were taken from, and compares it
with the snapshot the rules were written against (tools/snapshots/). It prints what changed and exits 1 when
anything did (2 when the source cannot be read), so it can run on a schedule. Nothing in makepst/ is edited:
an extracted fact is not a rule. The report says which module to revise; after revising it, `--write`
records the new source as the reviewed snapshot, and tests/test_rule_sources.py holds the code to it.

pestpp    PEST++ is on GitHub (usgs/pestpp). The option names, aliases and value types are read from
          PestppOptions::assign_*value_by_key* in src/libs/pestpp_common/pest_data_structs.cpp (types of
          `convert_ip` targets from pest_data_structs.h) and the version from src/libs/common/config_os.h.
          The snapshot is also checked against makepst/pestpp.py itself, so a registry edited by hand
          without re-extraction, or an extraction not yet carried into the registry, is reported.
pestchek  PEST is not on GitHub; point --source at the pest_source folder of a PEST download. The version
          comes from version.inc; from pestchek.F and cheksub.F the control-data keywords PEST finds by
          name (INDEX(CLINE,'...'), VARTEXT='...', the stop-hours reader) and the message catalogue (every
          FORMAT string, i.e. every error and warning pestchek can report). A new keyword or message is a
          rule to consider porting; a removed one, a rule to reconsider. The keywords are also checked
          against makepst/sections.py, so a keyword the schema does not know is reported.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAPSHOTS = os.path.join(HERE, 'snapshots')
sys.path.insert(0, ROOT)

PESTPP_REPO = 'usgs/pestpp'
PESTPP_FILES = {'cpp': 'src/libs/pestpp_common/pest_data_structs.cpp',
                'h': 'src/libs/pestpp_common/pest_data_structs.h',
                'version': 'src/libs/common/config_os.h'}
_CTYPES = {'int': 'int', 'long': 'int', 'double': 'float', 'float': 'float', 'bool': 'bool', 'string': 'str'}


# ---------------------------------------------------------------------- PEST++
def _get(url):
    req = urllib.request.Request(url)
    token = os.environ.get('GITHUB_TOKEN')
    if token and url.startswith('https://api.github.com/'):      # 60 anonymous API calls an hour otherwise
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', errors='replace')


def fetch_pestpp(ref):
    """{'cpp', 'h', 'version'} texts and the commit `ref` resolves to, from GitHub."""
    commit = json.loads(_get(f'https://api.github.com/repos/{PESTPP_REPO}/commits/{ref}'))
    sha = commit['sha']
    texts = {k: _get(f'https://raw.githubusercontent.com/{PESTPP_REPO}/{sha}/{path}') for k, path in PESTPP_FILES.items()}
    return texts, {'commit': sha[:12], 'date': commit['commit']['committer']['date'][:10]}


def read_pestpp(folder):
    """The same texts from a local checkout of the PEST++ repository."""
    return {k: open(os.path.join(folder, path), encoding='utf-8', errors='replace').read()
            for k, path in PESTPP_FILES.items()}, {'commit': 'local', 'date': ''}


def _conditions(text):
    """(names, body) for every `if (...)` / `else if (...)` testing `key == "..."`, in order."""
    out = []
    for m in re.finditer(r'\bif\s*\(', text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {'(': 1, ')': -1}.get(text[i], 0)
            i += 1
        cond = text[m.end():i - 1]
        names = re.findall(r'\bkey\s*==\s*"([A-Z0-9_]+)"', cond)
        if names and not re.search(r'\bvalue\s*==', cond):
            out.append([names, i])
    for n, (names, start) in enumerate(out):                    # a branch's body runs to the next branch
        end = out[n + 1][1] if n + 1 < len(out) else len(text)
        out[n][1] = text[start:end]
    return out


def _member_types(header):
    types = {}
    for ctype, name in re.findall(r'^\s*(?:std::)?(int|long|double|float|bool|string)\s+(\w+)\s*;', header, re.M):
        types[name] = _CTYPES[ctype]
    for name in re.findall(r'^\s*(?:std::)?vector<\s*(?:std::)?string\s*>\s+(\w+)\s*;', header, re.M):
        types[name] = 'list[str]'
    return types


def _value_type(body, members):
    if 'parse_string_arg_to_bool' in body:
        return 'bool'
    if re.search(r'\btokenize\s*\(', body):
        item = re.search(r'convert_cp\s*<\s*(int|double|float)\s*>', body)
        if not item:                        # `double v; ... convert_ip(t, v); x.push_back(v)`
            local = {n: t for t, n in re.findall(r'\b(int|double|float)\s+(\w+)\s*;', body)}
            target = re.search(r'convert_ip\s*\(\s*\w+\s*,\s*(\w+)\s*\)', body)
            item = local.get(target.group(1)) if target else None
            return f'list[{_CTYPES[item]}]' if item else 'list[str]'
        return f'list[{_CTYPES[item.group(1)]}]'
    m = re.search(r'convert_ip\s*\(\s*(?:org_)?value\s*,\s*(\w+)\s*\)', body)
    if m:
        return members.get(m.group(1), 'str')
    return 'str'


def extract_pestpp(texts):
    """{'version', 'options': {name: type}, 'aliases': {alias: name}, 'choices': {name: [...]}} from the source."""
    cpp = texts['cpp']
    start = cpp.index('PestppOptions::assign_value_by_key(')
    stop = cpp.index('ControlInfo::assign_value_by_key(')          # the PestppOptions assigners come first
    members = _member_types(texts['h'])
    options, aliases, choices, deprecated = {}, {}, {}, []
    for names, body in _conditions(cpp[start:stop]):
        name = names[0].lower()
        if name in options or name in aliases:
            continue
        options[name] = _value_type(body, members)
        for alias in names[1:]:
            aliases.setdefault(alias.lower(), name)
        # the words the branch accepts: `value == "X"`, or the same test on a local copy of the value
        values = sorted({v.lower() for v in re.findall(r'\b(?!key\b)\w+\s*==\s*"([A-Z0-9_]+)"', body)})
        if values:
            choices[name] = values
        # accepted and ignored: the branch only prints that it is deprecated
        if re.search(r'deprecat', body, re.I) and not re.search(r'convert_|parse_|\w+\s*=[^=]', body):
            deprecated.append(name)
    version = re.search(r'PESTPP_VERSION\s+"([^"]+)"', texts['version'])
    return {'version': version.group(1) if version else None, 'options': dict(sorted(options.items())),
            'aliases': dict(sorted(aliases.items())), 'choices': dict(sorted(choices.items())),
            'deprecated': sorted(deprecated)}


def registry_view():
    """What makepst/pestpp.py holds now, in the extraction's terms."""
    from makepst import pestpp
    names = {int: 'int', float: 'float', bool: 'bool', str: 'str',
             list[float]: 'list[float]', list[int]: 'list[int]', list[str]: 'list[str]'}
    options = {k: names[v['type']] for k, v in pestpp.PESTPP_OPTIONS.items()}
    return {'version': pestpp.PESTPP_REGISTRY_VERSION, 'options': options, 'aliases': dict(pestpp.ALIASES),
            'choices': {k: sorted(v['choices']) for k, v in pestpp.PESTPP_OPTIONS.items() if 'choices' in v},
            'deprecated': sorted(k for k, v in pestpp.PESTPP_OPTIONS.items() if v.get('deprecated'))}


def compare_pestpp(old, new, label_old, label_new, hand_choices=False):
    """Lines describing how `new` differs from `old` (both extraction dicts).

    `hand_choices`: `new` is the registry, which may restrict values PEST++'s parser takes as any text
    (ies_subset_how is checked later, in the ies code); only choices the source itself tests are compared.
    """
    out = []
    if old.get('version') != new.get('version'):
        out.append(f'version: {label_old} {old.get("version")} -> {label_new} {new.get("version")}')
    o, n = old['options'], new['options']
    for k in sorted(set(n) - set(o)):
        out.append(f'option added in {label_new}: {k} ({n[k]})')
    for k in sorted(set(o) - set(n)):
        out.append(f'option not in {label_new}: {k}')
    for k in sorted(set(o) & set(n)):
        if o[k] != n[k]:
            out.append(f'type of {k}: {o[k]} in {label_old}, {n[k]} in {label_new}')
    oa, na = old['aliases'], new['aliases']
    for k in sorted(set(oa) | set(na)):
        if oa.get(k) != na.get(k):
            out.append(f'alias {k}: {oa.get(k)} in {label_old}, {na.get(k)} in {label_new}')
    for k in sorted(set(old.get('choices', {})) | set(new.get('choices', {}))):
        a, b = old.get('choices', {}).get(k), new.get('choices', {}).get(k)
        if a != b and not (hand_choices and a is None):
            out.append(f'values of {k}: {a} in {label_old}, {b} in {label_new}')
    od, nd = set(old.get('deprecated', [])), set(new.get('deprecated', []))
    for k in sorted(nd - od):
        out.append(f'deprecated in {label_new}: {k}')
    for k in sorted(od - nd):
        out.append(f'no longer deprecated in {label_new}: {k}')
    return out


# ---------------------------------------------------------------------- PEST (pestchek)
PESTCHEK_FILES = ('pestchek.F', 'cheksub.F')


def _fortran_lines(text):
    """Source lines with fixed-form continuation lines joined (column 6 non-blank)."""
    out = []
    for line in text.splitlines():
        if line[:1] in ('c', 'C', '*', '!'):
            continue
        if len(line) > 5 and line[:5].strip() == '' and line[5] not in (' ', '0') and out:
            out[-1] += line[6:].strip()
        else:
            out.append(line.rstrip())
    return out


def _format_text(stmt):
    """The literal text of a FORMAT statement, with A/I/F edit descriptors shown as '...'."""
    parts = re.findall(r"'((?:[^']|'')*)'|\b\d*[AIFEGD]\d*(?:\.\d+)?\b", stmt)
    text = ''.join(p.replace("''", "'") if p else '...' for p in parts)
    return re.sub(r'(\.\.\.)+', '...', re.sub(r'\s+', ' ', text)).strip()


def extract_pestchek(folder):
    """{'version', 'keywords', 'messages'} from a PEST pest_source folder."""
    texts = {f: open(os.path.join(folder, f), errors='replace').read() for f in PESTCHEK_FILES}
    version = re.search(r"aversion\s*=\s*'([^']+)'", open(os.path.join(folder, 'version.inc')).read(), re.I)
    keywords, messages = set(), set()
    for f, text in texts.items():
        lines = _fortran_lines(text)
        for line in lines:
            keywords.update(k.strip().lower() for k in re.findall(r"INDEX\s*\(\s*[DC]LINE\s*,\s*'([^']+)'", line, re.I))
            keywords.update(k.lower() for k in re.findall(r"VARTEXT\s*=\s*'([^']+)'", line, re.I))
            keywords.update(k.lower() for k in re.findall(r"AVAR\s*=\s*'([^']+)'", line, re.I))
            m = re.match(r'^\s*\d+\s+FORMAT\s*\((.*)\)\s*$', line, re.I)
            if m:
                text_ = _format_text(m.group(1))
                if len(re.sub(r'[^A-Za-z]', '', text_)) >= 12:          # skip layout-only formats
                    messages.add(text_)
    # section headers and other structural strings are found the same way; keep names only
    keywords = {k for k in keywords if re.fullmatch(r'[a-z_][a-z0-9_]*', k)}
    return {'version': version.group(1) if version else None, 'keywords': sorted(keywords),
            'messages': sorted(messages)}


def schema_view():
    """Every control-data word makepst's schema knows, and the ones it leaves out on purpose."""
    from makepst.rules import PESTCHEK_IGNORED
    from makepst.sections import ALL_SECTIONS
    known = set()
    for sec in ALL_SECTIONS:
        known.update(sec.fields)
        for words in sec.flags.values():
            known.update(words)
    return known, set(PESTCHEK_IGNORED)


def compare_pestchek(old, new):
    out = []
    if old.get('version') != new.get('version'):
        out.append(f'version: snapshot {old.get("version")} -> source {new.get("version")}')
    for k in sorted(set(new['keywords']) - set(old['keywords'])):
        out.append(f'keyword added: {k}')
    for k in sorted(set(old['keywords']) - set(new['keywords'])):
        out.append(f'keyword removed: {k}')
    for m in sorted(set(new['messages']) - set(old['messages'])):
        out.append(f'message added: {m}')
    for m in sorted(set(old['messages']) - set(new['messages'])):
        out.append(f'message removed: {m}')
    return out


# ---------------------------------------------------------------------- driver
def _snapshot_path(name):
    return os.path.join(SNAPSHOTS, f'{name}.json')


def _load(name):
    path = _snapshot_path(name)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _save(name, data):
    os.makedirs(SNAPSHOTS, exist_ok=True)
    with open(_snapshot_path(name), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write('\n')


def run_pestpp(args):
    texts, where = read_pestpp(args.source) if args.source else fetch_pestpp(args.ref)
    new = extract_pestpp(texts)
    new.update(source=f'{PESTPP_REPO}@{where["commit"]}', date=where['date'],
               digest=hashlib.sha256(texts['cpp'].encode()).hexdigest()[:16])
    old = _load('pestpp')
    print(f'PEST++ {new["version"]} ({new["source"]}, {new["date"]}): {len(new["options"])} options, '
          f'{len(new["aliases"])} aliases')
    report = compare_pestpp(old, new, 'snapshot', 'source') if old else ['no snapshot yet']
    # the registry in makepst/pestpp.py must say what the reviewed snapshot says
    reviewed = new if args.write else old
    if reviewed:
        report += [f'makepst/pestpp.py: {line}' for line in
                   compare_pestpp(reviewed, registry_view(), 'snapshot', 'pestpp.py', hand_choices=True)]
    return _finish('pestpp', new, report, args.write,
                   'revise _TYPES / ALIASES / _CONSTRAINTS and PESTPP_REGISTRY_VERSION in makepst/pestpp.py')


def run_pestchek(args):
    new = extract_pestchek(args.source)
    new.update(source=os.path.basename(os.path.normpath(args.source)))
    old = _load('pestchek')
    print(f'PEST {new["version"]} pestchek: {len(new["keywords"])} keywords, {len(new["messages"])} messages')
    report = compare_pestchek(old, new) if old else ['no snapshot yet']
    from makepst.sections import PESTCHEK_SOURCE_VERSION
    known, ignored = schema_view()
    for k in sorted(set(new['keywords']) - known - ignored):
        report.append(f'makepst/sections.py: keyword pestchek reads is not in the schema: {k}')
    reviewed = new if args.write else old
    if reviewed and reviewed.get('version') != PESTCHEK_SOURCE_VERSION:
        report.append(f'makepst/sections.py: PESTCHEK_SOURCE_VERSION is {PESTCHEK_SOURCE_VERSION}, '
                      f'the reviewed snapshot is PEST {reviewed.get("version")}')
    return _finish('pestchek', new, report, args.write,
                   'port the changed rules into makepst/rules.py (control data: makepst/sections.py) '
                   'and set PESTCHEK_SOURCE_VERSION')


def _finish(name, new, report, write, advice):
    for line in report:
        print(f'  {line}')
    if write:
        _save(name, new)
        path = _snapshot_path(name)
        try:
            path = os.path.relpath(path, ROOT)
        except ValueError:                           # another drive on Windows
            pass
        print(f'snapshot written: {path}')
        report = [line for line in report if line.startswith('makepst/')]     # the source is now reviewed
    if not report:
        print('in step')
        return 0
    print(f'to do: {advice}; then run again with --write' if not write else 'to do: the makepst/ lines above')
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pestpp', help='PEST++ option registry (makepst/pestpp.py)')
    p.add_argument('--ref', default='develop', help='branch, tag or commit of usgs/pestpp (default develop)')
    p.add_argument('--source', help='a local PEST++ checkout instead of GitHub')
    p.add_argument('--write', action='store_true', help='record the source as the reviewed snapshot')
    p.set_defaults(func=run_pestpp)
    c = sub.add_parser('pestchek', help="PEST's pestchek rules (makepst/rules.py, makepst/sections.py)")
    c.add_argument('--source', required=True, help='the pest_source folder of a PEST download')
    c.add_argument('--write', action='store_true', help='record the source as the reviewed snapshot')
    c.set_defaults(func=run_pestchek)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError) as e:     # unreachable source, or code no longer shaped as expected
        print(f'could not read the source: {type(e).__name__}: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
