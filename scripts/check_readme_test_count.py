#!/usr/bin/env python3
"""Fail if README.md's advertised test count disagrees with the suite.

Why this exists. On 2026-10-07 five repositories were audited and ALL FIVE advertised a stale
test count: 42 where there were 58, 45 where there were 60, 97 where there were 150. The number
had been true when written and nothing re-checked it, which is the same defect those repositories
had just been audited for: a claim with no check behind it.

It FAILS CLOSED, deliberately, in three ways that a looser version would let pass:

  - a README with NO advertised count fails, rather than passing because there was nothing to
    compare. "Nothing to check" is the most common way a check stops checking.
  - a collection that errors fails, rather than being read as zero tests.
  - EVERY occurrence must match, not the first. Two of the five repositories state the count
    twice, and one of them had both stale.
  - a test FILE that contributes no collected test fails. This one was learned the hard way,
    hours after the rest shipped: agent-graph's tests/test_rag.py opens with
    `pytest.importorskip("ragevallab")`, so without the optional `rag` extra the whole module
    vanishes at collection with NO error and no mention. pytest says "58 tests collected" and
    means it. An earlier version of this check read that 58, agreed with a README that had
    just been edited to match it, and would have frozen the wrong number in place; CI, which
    installs `.[dev,rag]`, collects 67. A count is only meaningful if the environment is
    complete, and the only evidence of incompleteness is a file on disk that produced nothing.

The pattern is `\\b(\\d+) tests\\b`, plural and word-bounded, so prose like "300 test claims"
is not mistaken for a declaration. Write a historical count without the word, or this will
rightly object to it.

    python scripts/check_readme_test_count.py          # check
    python scripts/check_readme_test_count.py --selftest
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

DECLARED = re.compile(r"\b(\d+) tests\b")
COLLECTED = re.compile(r"^(\d+) tests? collected", re.M)
# A file may opt out of the empty-file rule by saying so in its own text.
EXEMPT = "check-readme-test-count: exempt"


def collect(root: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-p", "no:cacheprovider"],
                       cwd=root, capture_output=True, text=True)
    m = COLLECTED.search(p.stdout)
    if not m:
        raise SystemExit(
            "FAIL: could not read a test count from pytest --collect-only.\n"
            "A collection that does not report a number is not zero tests, it is an error.\n"
            + (p.stdout or p.stderr)[-1500:])
    return int(m.group(1)), p.stdout


def silent_empty_test_files(root: pathlib.Path, stdout: str) -> list[str]:
    """Test files on disk that contributed no collected test.

    A module skipped by `pytest.importorskip` leaves no error and no line of output, so the
    only way to notice is to compare the collection against the filesystem.
    """
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    seen = {line.split("::", 1)[0].strip() for line in stdout.splitlines() if "::" in line}
    missing, exempt = [], []
    for f in sorted(tests_dir.rglob("test_*.py")):
        rel = f.relative_to(root).as_posix()
        if any(s == rel or s.endswith("/" + rel) or s.endswith(rel) for s in seen):
            continue
        # A module can be legitimately gated on something CI provides in a SEPARATE job, such
        # as a database service. That is a human decision, so it is declared in the file
        # itself and printed here. An exemption nobody can see is how a check stops checking.
        if EXEMPT in f.read_text():
            exempt.append(rel)
        else:
            missing.append(rel)
    if exempt:
        print(f"note: {len(exempt)} test file(s) exempt by declaration and NOT counted: "
              + ", ".join(exempt))
    return missing


def check(root: pathlib.Path) -> int:
    readme = root / "README.md"
    if not readme.is_file():
        print("FAIL: no README.md to check", file=sys.stderr)
        return 1
    actual, stdout = collect(root)
    empty = silent_empty_test_files(root, stdout)
    if empty:
        print(f"FAIL: {len(empty)} test file(s) contributed no collected test: "
              + ", ".join(empty) + "\n"
              "       Usually an optional extra is missing, so a module guarded by\n"
              "       pytest.importorskip vanished without an error. The count from this\n"
              "       environment is NOT the project's count. Install every extra CI installs\n"
              "       and re-run.", file=sys.stderr)
        return 1
    declared = [int(n) for n in DECLARED.findall(readme.read_text())]
    if not declared:
        print(f"FAIL: README.md advertises no test count, so nothing was checked.\n"
              f"       The suite collects {actual}. State it as '{actual} tests', or this\n"
              f"       check is decorative.", file=sys.stderr)
        return 1
    wrong = [n for n in declared if n != actual]
    if wrong:
        print(f"FAIL: README.md says {wrong} tests; the suite collects {actual}.\n"
              f"       {len(declared)} count(s) found, {len(wrong)} wrong. Update every one.",
              file=sys.stderr)
        return 1
    print(f"ok: README.md and the suite agree on {actual} tests "
          f"({len(declared)} mention{'s' if len(declared) != 1 else ''})")
    return 0


def selftest() -> int:
    """Prove the check can fail, against a README this script writes itself."""
    import tempfile
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text(
            "def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\n")
        cases = [
            ("3 tests, all green", 0, "a correct count passes"),
            ("4 tests, all green", 1, "a wrong count fails"),
            ("3 tests here and 4 tests there", 1, "a second wrong count fails"),
            ("no number at all", 1, "a missing count fails rather than passing"),
            ("300 test claims from SciFact", 1, "singular prose is not a declaration"),
        ]
        for text, want, why in cases:
            (root / "README.md").write_text(text)
            got = check(root)
            flag = "ok  " if got == want else "BAD "
            if got != want:
                bad += 1
            print(f"  {flag}{why}: expected exit {want}, got {got}")
        # the case that was learned late: a module skipped whole by importorskip leaves
        # NO error, so the count looks healthy and is wrong.
        (root / "README.md").write_text("3 tests, all green")
        (root / "tests" / "test_optional.py").write_text(
            'import pytest\n'
            'pytest.importorskip("a_module_that_is_not_installed")\n'
            'def test_z(): pass\n')
        got = check(root)
        flag = "ok  " if got == 1 else "BAD "
        if got != 1:
            bad += 1
        print(f"  {flag}a silently skipped test file fails, though pytest reports no error: "
              f"expected exit 1, got {got}")
    print("selftest FAILED" if bad else "selftest passed: the check can fail")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv
                     else check(pathlib.Path(__file__).resolve().parent.parent))
