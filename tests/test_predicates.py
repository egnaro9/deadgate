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


# --------------------------------------------------------------- the table says what it means
# An audit of this suite found that the -le, -lt and -ne rows could each be INVERTED with all
# 121 tests green. predicates.py calls this table the project's accumulated knowledge, and a
# table nothing evaluates is a table that can quietly mean the opposite. These tests do not
# read the table; they evaluate the shell condition the row claims to characterise.

import subprocess

import pytest

from deadgate.predicates import extract, is_gate_step


def _shell_true(cond: str, var: str, value: str) -> bool:
    """Does the real shell consider `cond` true when var=value? Settles it by running it."""
    proc = subprocess.run(["/bin/sh", "-c", f'{var}="{value}"; if {cond}; then exit 0; fi; exit 1'],
                          capture_output=True, text=True)
    return proc.returncode == 0


@pytest.mark.parametrize("op,n", [
    ("-ge", 90), ("-gt", 90), ("-le", 3), ("-lt", 5), ("-eq", 2), ("-ne", 2),
])
def test_plant_violates_and_near_miss_satisfies(op, n):
    """The plant must make the gate's condition FALSE and the near miss must keep it TRUE.

    Inverting any row swaps those, which is exactly the mutation that survived. Evaluated
    against /bin/sh rather than against the table, so the table cannot vouch for itself.
    """
    cond = f'[ "$COUNT" {op} {n} ]'
    preds = extract(f"if {cond}; then echo ok; fi")
    assert preds, f"no predicate extracted from {cond}"
    p = preds[0]
    assert not _shell_true(cond, "COUNT", p.plant), (
        f"{op}: plant {p.plant!r} should FAIL the gate but the shell accepts it")
    assert _shell_true(cond, "COUNT", p.near_miss), (
        f"{op}: near miss {p.near_miss!r} should PASS the gate but the shell rejects it")


@pytest.mark.parametrize("op,n", [("-ge", 90), ("-le", 3), ("-ne", 2)])
def test_plant_and_near_miss_are_adjacent(op, n):
    """Boundary mutation is the point: the pair must differ by exactly one."""
    p = extract(f'if [ "$COUNT" {op} {n} ]; then echo ok; fi')[0]
    assert abs(int(p.plant) - int(p.near_miss)) == 1


# --------------------------------------------------------------- is_gate_step's exit forms
# The `|| exit` and `&& exit` alternatives were dead in this suite while live in the field:
# every existing case was already satisfied by the first three alternatives of the regex.

@pytest.mark.parametrize("shell,expected", [
    ('pytest || exit $?', True),
    ('make check && exit 1', True),
    ('./run.sh || exit "$rc"', True),
    ('exit 1', True),
    ('echo "::error::broken"', True),
    ('assert_clean', True),
    ('echo done', False),
    ('exit 0', False),
    ('cleanup || true', False),
])
def test_is_gate_step_recognises_each_failing_form(shell, expected):
    assert is_gate_step(shell) is expected
