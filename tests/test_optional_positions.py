"""Classic control fields must never change meaning when earlier slots are blank."""
from pathlib import Path

import pytest

from makepst import from_text, read_pst, to_text
from makepst.sections import CONTROL, REGUL, Section
from makepst.writer import to_text_v2, write_pst


DATA = Path(__file__).parent / 'data'
GAPS = [
    ('derzerolim', 0.0001, {'maxcompdim': 0}),
    ('upvecbend', 1, {'iboundstick': 0}),
    ('splitswh', 0.5, {'noptswitch': 2}),
    ('regweightrat', 2.5, {'noptregadj': 3}),
    ('regsingthresh', 0.01, {'noptregadj': 3, 'regweightrat': 2.5}),
]


@pytest.mark.parametrize('field,value,predecessors', GAPS)
@pytest.mark.parametrize('blank', [None, '', float('nan')])
def test_missing_positional_predecessors_are_rejected(field, value, predecessors, blank):
    pst = read_pst(DATA / 'demo.pst')
    pst.set_control({field: value, **{k: blank for k in predecessors}})
    with pytest.raises(ValueError) as error:
        to_text(pst)
    message = str(error.value).lower()
    assert field in message
    assert all(k in message for k in predecessors)
    assert 'control data' in message or 'regularisation' in message


@pytest.mark.parametrize('field,value,predecessors', GAPS)
def test_explicit_predecessors_preserve_named_values(field, value, predecessors):
    pst = read_pst(DATA / 'demo.pst')
    requested = {field: value, **predecessors}
    pst.set_control(requested)
    reread = from_text(to_text(pst))
    for name, expected in requested.items():
        assert reread.control[name] == pytest.approx(expected)


def test_middle_gap_is_rejected_even_with_first_predecessor_present():
    with pytest.raises(ValueError, match='regweightrat'):
        REGUL.render({'noptregadj': 4, 'regsingthresh': 0.002})


def test_different_value_and_missing_field_names_use_same_rule():
    section = Section('* example', [('first', 'second', 'third')], defaults={'first': 7})
    with pytest.raises(ValueError, match='second'):
        section.render({'third': -12.75})
    values = section.parse(section.render({'second': 0, 'third': -12.75}).splitlines()[1:])
    assert values == {'first': 7, 'second': 0, 'third': -12.75}


def test_named_tokens_and_flags_do_not_require_positional_fillers():
    pst = read_pst(DATA / 'demo.pst')
    pst.set_control({'absparmax': 'absparmax(1)=0.25 absparmax(2)=30',
                     'uptestlim': 8, 'doaui': 'noaui', 'regcontinue': 'regcontinue'})
    reread = from_text(to_text(pst))
    for field in ('absparmax', 'uptestlim', 'doaui', 'regcontinue'):
        assert reread.control[field] == pst.control[field]
    for field in ('iboundstick', 'upvecbend', 'noptswitch', 'splitswh', 'linreg'):
        assert field not in reread.control


def test_trailing_optional_fields_can_stay_absent():
    values = REGUL.parse(REGUL.render({}).splitlines()[1:])
    assert not {'noptregadj', 'regweightrat', 'regsingthresh'} & values.keys()


def test_invalid_export_does_not_overwrite_existing_file(tmp_path):
    pst = read_pst(DATA / 'demo.pst')
    pst.set_control({'upvecbend': 1})
    output = tmp_path / 'existing.pst'
    output.write_text('existing content\n')
    with pytest.raises(ValueError, match='iboundstick'):
        write_pst(pst, str(output))
    assert output.read_text() == 'existing content\n'
    assert not (tmp_path / 'dump.tpl').exists()


def test_keyword_format_does_not_need_positional_predecessors():
    pst = read_pst(DATA / 'demo.pst')
    pst.set_control({'upvecbend': 1, 'regsingthresh': 0.002})
    text = to_text_v2(pst, 'example')
    body = text.split('* control data keyword\n', 1)[1].split('\n*', 1)[0]
    keywords = dict(line.split(None, 1) for line in body.splitlines() if line.strip())
    assert keywords['upvecbend'] == '1'
    assert keywords['regsingthresh'] == '0.002'
    assert 'iboundstick' not in keywords
    assert 'noptregadj' not in keywords
