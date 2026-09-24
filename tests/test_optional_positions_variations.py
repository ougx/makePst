"""Additional B02 variations checked after implementing the positional guard."""
import pytest

from makepst.sections import Section


def test_zero_is_still_a_supplied_positional_value():
    section = Section('* example', [('earlier', 'later')])
    with pytest.raises(ValueError, match='earlier'):
        section.render({'later': 0})


def test_named_token_does_not_hide_a_gap_before_another_numeric_value():
    section = Section('* example', [('earlier', 'named', 'later')])
    values = {'named': 'named(2)=17', 'later': 4.25}
    with pytest.raises(ValueError, match='earlier'):
        section.render(values)
    assert values == {'named': 'named(2)=17', 'later': 4.25}
    assert section.defaults == {}
