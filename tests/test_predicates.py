"""The shape corpus. Each entry: shell in, the mutation pair the extractor must derive.

This is the asset. The structural detectors are re-derivable from GitHub's own docs;
this table is accumulated knowledge about how gates are written and how they fail.
"""
from __future__ import annotations

import pytest

from deadgate.predicates import extract, is_gate_step

CASES = [
    # (shell, kind, reads, plant, near_miss, boundary)
    ('[ -s coverage.json ] || exit 1',
     "FILE_NONEMPTY", "coverage.json", "<coverage.json empty>", "<coverage.json with one byte>", True),
    ('test -f dist/app.tgz || exit 1',
     "FILE_EXISTS", "dist/app.tgz", "<dist/app.tgz absent>", "<dist/app.tgz present>", False),
    ('if [ "$COVERAGE" -ge 90 ]; then echo ok; else exit 1; fi',
     "NUM_THRESHOLD", "$COVERAGE", "89", "90", True),
    ('[ "$FAILURES" -eq 0 ] || exit 1',
     "NUM_THRESHOLD", "$FAILURES", "1", "0", True),
    ('[ "$COUNT" -gt 5 ] || exit 1',
     "NUM_THRESHOLD", "$COUNT", "5", "6", True),
    ('grep -q "BUILD OK" build.log || exit 1',
     "GREP_PRESENT", "build.log", "<build.log with no occurrence of 'BUILD OK'>",
     "<build.log containing 'BUILD OK'>", False),
    ('jq -e \'.vulnerabilities.high == 0\' audit.json || exit 1',
     "JQ_PREDICATE", "stdin json", "<json where `.vulnerabilities.high == 0` is false or null>",
     "<json where `.vulnerabilities.high == 0` is true>", False),
    ('[ -z "$TOKEN" ] && exit 1',
     "STR_EMPTY", "$TOKEN", "nonempty", "", False),
    ('[ "$BRANCH" = "main" ] || exit 1',
     "STR_EQUAL", "$BRANCH", "main_X", "main", False),
]


@pytest.mark.parametrize("shell,kind,reads,plant,near,boundary", CASES,
                         ids=[c[1] + ":" + c[2] for c in CASES])
def test_shape(shell, kind, reads, plant, near, boundary):
    got = extract(shell)
    assert got, f"extracted nothing from: {shell}"
    p = got[0]
    assert p.kind == kind
    assert p.reads == reads
    assert p.plant == plant, f"plant: want {plant!r} got {p.plant!r}"
    assert p.near_miss == near, f"near_miss: want {near!r} got {p.near_miss!r}"
    assert p.boundary is boundary


def test_threshold_mutations_are_adjacent():
    """The whole point of a boundary mutation: an off-by-one gate is invisible to reading."""
    p = extract('[ "$COV" -ge 90 ]')[0]
    assert int(p.near_miss) - int(p.plant) == 1


def test_non_gate_shell_yields_nothing():
    assert extract("echo building\nnpm run build") == []


@pytest.mark.parametrize("shell,expected", [
    ('npm test || exit 1', True),
    ('echo "::error::bad"', True),
    ('assert.equal(a, b)', True),
    ('npm run build', False),
    ('echo hello', False),
])
def test_is_gate_step(shell, expected):
    assert is_gate_step(shell) is expected
