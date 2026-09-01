"""Tests for promql.py's label-matcher escaping.

Every route used to build PromQL selectors by hand, with three
different conventions: a manual replace() for the user filter, a
re.escape()-based helper for job-ID lists, and — for path parameters
like a node name or job ID — no escaping at all. These tests pin the
one shared convention that replaced them.
"""

from promql import label_eq, label_in, selector


def test_label_eq_plain_value():
    assert label_eq("instance", "gpu1") == 'instance="gpu1"'


def test_label_eq_escapes_a_quote_in_a_node_name():
    # A node name containing a double quote would otherwise break out of
    # the string literal and alter the selector.
    assert label_eq("instance", 'gpu"1') == 'instance="gpu\\"1"'


def test_label_eq_escapes_a_backslash():
    assert label_eq("user", "dom\\ain") == 'user="dom\\\\ain"'


def test_label_in_sorts_and_joins():
    assert label_in("slurmjobid", {"9", "10"}) == 'slurmjobid=~"^(?:10|9)$"'


def test_label_in_escapes_a_regex_metacharacter_in_a_job_id():
    assert (label_in("slurmjobid", {"a.b", "c"})
            == 'slurmjobid=~"^(?:a\\.b|c)$"')


def test_label_in_escapes_parens_and_plus():
    assert (label_in("slurmjobid", {"a(b)+c"})
            == 'slurmjobid=~"^(?:a\\(b\\)\\+c)$"')


def test_label_in_is_anchored_against_prefix_or_substring_matches():
    # "1" must not match a candidate list containing "10" or "21".
    matcher = label_in("slurmjobid", {"1", "10", "21"})
    assert matcher == 'slurmjobid=~"^(?:1|10|21)$"'


def test_selector_empty_with_no_matchers():
    assert selector() == ""


def test_selector_wraps_one_matcher():
    assert selector(label_eq("instance", "gpu1")) == '{instance="gpu1"}'


def test_selector_joins_multiple_matchers():
    assert (selector(label_eq("a", "1"), label_eq("b", "2"))
            == '{a="1",b="2"}')
