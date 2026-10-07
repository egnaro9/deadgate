"""The sandbox must prove BOTH directions, and must never execute repository text.

Every case here plants the condition a gate claims to catch and asserts the gate rejects
it, then plants the near miss and asserts the gate accepts it. A gate that rejects both
is LEAKY and a gate that accepts both is DEAD; neither is a working gate.
"""
from __future__ import annotations

import pytest

from deadgate.predicates import extract
from deadgate.sandbox import DEAD, FIRED, INCONCLUSIVE, LEAKY, exercise

CORRECT_GATES = [
    '[ "$COVERAGE" -ge 90 ]',
    '[ "$FAILURES" -eq 0 ]',
    '[ -s coverage.json ]',
    '[ -f dist/app.tgz ]',
    'grep -q "BUILD OK" build.log',
    '[ "$BRANCH" = "main" ]',
    '[ -n "$TOKEN" ]',
]


@pytest.mark.parametrize("shell", CORRECT_GATES, ids=lambda s: s[:26])
def test_a_correct_gate_fires(shell):
    """A gate that works must come back FIRED, not DEAD and not LEAKY."""
    preds = extract(shell)
    assert preds, f"nothing extracted from {shell}"
    r = exercise(preds[0])
    assert r.verdict == FIRED, f"{shell} -> {r.verdict}: {r.detail} (script={r.reconstructed})"


def test_off_by_one_threshold_is_caught_at_the_boundary():
    """The gate that should be -gt but is -ge accepts exactly N.

    This is the case no amount of reading finds and only execution does.
    """
    r = exercise(extract('[ "$COV" -ge 90 ]')[0])
    assert r.verdict == FIRED
    assert r.plant_exit != 0 and r.near_exit == 0
    # the mutation really was adjacent
    assert int(r.predicate.near_miss) - int(r.predicate.plant) == 1


def test_unmodellable_shape_is_inconclusive_not_success():
    """A shape we cannot reconstruct must NEVER be reported as a working gate."""
    from deadgate.predicates import Predicate
    p = Predicate("JQ_PREDICATE", "jq -e '.x'", "stdin json", "<false>", "<true>")
    r = exercise(p)
    assert r.verdict == INCONCLUSIVE
    assert r.verdict not in (FIRED,)


def test_repository_text_is_never_executed():
    """The reconstruction must not contain the attacker-controlled fragment."""
    from deadgate.predicates import Predicate
    evil = Predicate("NUM_THRESHOLD", 'rm -rf / # [ "$X" -ge 1 ]', "$X", "0", "1",
                     note="boundary of '-ge 1'.")
    r = exercise(evil)
    assert "rm -rf" not in r.reconstructed
    assert r.verdict in (FIRED, DEAD, LEAKY, INCONCLUSIVE)


def test_injection_through_a_variable_name_is_refused():
    from deadgate.predicates import Predicate
    bad = Predicate("NUM_THRESHOLD", "x", "$(touch /tmp/pwned)", "0", "1",
                    note="boundary of '-ge 1'.")
    r = exercise(bad)
    assert r.verdict == INCONCLUSIVE
    assert r.reconstructed == ""
