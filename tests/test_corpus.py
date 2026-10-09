"""The corpus is the specification.

A detector must fire on the planted defect AND stay quiet on its near miss.
Both directions are required before a detector is called live.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from deadgate.detectors import scan_workflow

CORPUS = pathlib.Path(__file__).parent.parent / "corpus"
BROKEN = sorted((CORPUS / "broken").glob("*.yml"))
GOOD = sorted((CORPUS / "good").glob("*.yml"))

assert BROKEN, "no broken fixtures found"
assert GOOD, "no good fixtures found"


def _expected(path: pathlib.Path) -> str:
    return path.read_text().split("EXPECT:")[1].split()[0].strip()


@pytest.mark.parametrize("path", BROKEN, ids=lambda p: p.stem)
def test_known_broken_fires(path: pathlib.Path) -> None:
    want = _expected(path)
    got = {f.detector for f in scan_workflow(yaml.safe_load(path.read_text()))}
    assert want in got, f"{path.name}: expected {want}, got {sorted(got) or 'nothing'}"


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.stem)
def test_known_good_stays_silent(path: pathlib.Path) -> None:
    """A good fixture may be scoped to ONE detector with `# SILENT: D1`.

    Some shapes are correct for one detector's question and genuinely suspect for
    another's. Asserting that nothing at all fires forces a choice between a false
    negative and a false positive, so the fixture says which question it answers.
    """
    text = path.read_text()
    got = scan_workflow(yaml.safe_load(text))
    if "# SILENT:" in text:
        scoped = text.split("# SILENT:")[1].split()[0].strip()
        offending = [f for f in got if f.detector == scoped]
        assert not offending, (
            f"{path.name} must not fire {scoped}, but got "
            f"{[f'{f.detector}:{f.job}' for f in offending]}"
        )
        return
    assert not got, (
        f"{path.name} is a near miss and must not fire, but got "
        f"{[f'{f.detector}:{f.job}' for f in got]}"
    )


# --- D4 severity tiering -------------------------------------------------------
# Skipping a deploy because no release was cut is intended. Skipping the tests because
# the path filter crashed is not. The tier has to separate them or D4 is wallpaper.

import yaml as _yaml
from deadgate.detectors import scan_workflow as _scan

_PR_TEST = """
on: [pull_request]
jobs:
  detect:
    runs-on: ubuntu-latest
    outputs: {rust: "${{ steps.f.outputs.rust }}"}
    steps: [{id: f, run: 'echo rust=true >> $GITHUB_OUTPUT'}]
  test:
    needs: [detect]
    if: ${{ needs.detect.outputs.rust == 'true' }}
    runs-on: ubuntu-latest
    steps: [{run: cargo test}]
"""

_RELEASE_DEPLOY = """
on:
  push:
    tags: ['v*']
jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs: {go: "${{ steps.f.outputs.go }}"}
    steps: [{id: f, run: 'echo go=true >> $GITHUB_OUTPUT'}]
  publish:
    needs: [prepare]
    if: ${{ needs.prepare.outputs.go == 'true' }}
    runs-on: ubuntu-latest
    steps: [{run: npm publish}]
"""

_PR_UNCLEAR = """
on: [pull_request]
jobs:
  detect:
    runs-on: ubuntu-latest
    outputs: {x: "${{ steps.f.outputs.x }}"}
    steps: [{id: f, run: 'echo x=true >> $GITHUB_OUTPUT'}]
  widget:
    needs: [detect]
    if: ${{ needs.detect.outputs.x == 'true' }}
    runs-on: ubuntu-latest
    steps: [{run: make widget}]
"""


def test_d4_pr_verification_job_caps_at_medium_without_protection_data():
    """D4 no longer reaches HIGH from the job's NAME.

    This test asserted HIGH, inherited from `_gate_severity`, which returns HIGH when the
    name matches test/lint/check and the workflow runs on pull_request. The finding it
    labelled claims "any branch protection requiring it passes with nothing tested", and
    whether the job is required is the one thing a workflow file cannot say.

    Changed on evidence, not taste: in a pre-registered hand-labelled sample of 40 of this
    tool's own surviving HIGH findings, D4 scored 0 defensible out of 18, and every arguable
    case failed on exactly that point. HIGH is now reserved for the escalation path, where
    branch protection has attributed the job to a confirmed required check.

    MEDIUM here because this fixture's workflow has no fan-in gate at all, so nothing
    anywhere would notice the skip.
    """
    f = [x for x in _scan(_yaml.safe_load(_PR_TEST)) if x.detector == "D4"]
    assert f and f[0].severity == "MEDIUM", f
    assert "cannot say" in f[0].repro, "the repro must state the condition, not assert it"


def test_d4_release_publish_job_is_low():
    """A tag-triggered publish that skips when no release was cut is working as intended."""
    f = [x for x in _scan(_yaml.safe_load(_RELEASE_DEPLOY)) if x.detector == "D4"]
    assert f and f[0].severity == "LOW", f


def test_d4_unclear_pr_job_is_medium_not_high():
    """When the file cannot say whether it is a check, do not claim it is."""
    f = [x for x in _scan(_yaml.safe_load(_PR_UNCLEAR)) if x.detector == "D4"]
    assert f and f[0].severity == "MEDIUM", f


# --- D1 and D3 tiering ---------------------------------------------------------

_D1_PR_TEST = """
on: [pull_request]
jobs:
  heavy:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps: [{run: pytest -m slow}]
  test:
    needs: [heavy]
    runs-on: ubuntu-latest
    steps: [{run: echo done}]
"""

_D1_RELEASE = """
on:
  push:
    tags: ['v*']
jobs:
  prep:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    steps: [{run: make prep}]
  publish:
    needs: [prep]
    runs-on: ubuntu-latest
    steps: [{run: npm publish}]
"""

_D3_EXPORTED = """
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Compute coverage
        run: |
          COV=$(coverage report | tail -1)
          echo "cov=$COV" >> "$GITHUB_OUTPUT"
"""

_D3_CLEANUP = """
on: [pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Log the tree
        run: ls -la | head -40
"""


def _of(doc_text, det):
    return [x for x in _scan(_yaml.safe_load(doc_text)) if x.detector == det]


def test_d1_pr_verification_is_high():
    f = _of(_D1_PR_TEST, "D1")
    assert f and f[0].severity == "HIGH", f


def test_d1_release_pipeline_is_low():
    f = _of(_D1_RELEASE, "D1")
    assert f and f[0].severity == "LOW", f


def test_d3_exported_result_is_high():
    """The masked value leaves the step, so something downstream consumes it."""
    f = _of(_D3_EXPORTED, "D3")
    assert f and f[0].severity == "HIGH", f


def test_d3_fire_and_forget_is_not_high():
    """A logging pipeline masks nothing anybody reads."""
    f = _of(_D3_CLEANUP, "D3")
    assert f and f[0].severity == "MEDIUM", f


# ------------------------------------------------------------- what the corpus did NOT specify
# An audit found that the corpus suite checks WHICH detector fires on which file, and nothing
# else. Job attribution could be blanked, every `repro` emptied, and three severity guards
# deleted, with all 121 tests green. The corpus is called the specification in the README, so
# these close the gap between what it specifies and what the tool promises.

def _findings(doc):
    return list(scan_workflow(doc))


def test_every_finding_names_the_job_it_is_about():
    """cli.py prints `file::job` and the API tier looks the job up to resolve branch
    protection, so a blank job is a finding nobody can act on. `job=name` could be replaced
    with `job=""` on D1 and D2 with the suite green."""
    for path in BROKEN:
        doc = yaml.safe_load(path.read_text())
        jobs = (doc or {}).get("jobs") or {}
        for f in _findings(doc):
            assert f.job, f"{path.name}: {f.detector} finding has no job"
            assert f.job in jobs, f"{path.name}: {f.detector} names job {f.job!r}, not in {sorted(jobs)}"


def test_every_finding_carries_a_reproduction():
    """The README: 'Every finding carries a reproduction. A finding without one is an opinion,
    and this tool does not emit opinions.' Nothing read Finding.repro, so every repro could be
    blanked on every detector at once."""
    for path in BROKEN:
        doc = yaml.safe_load(path.read_text())
        for f in _findings(doc):
            assert f.repro.strip(), f"{path.name}: {f.detector} on {f.job} has an empty repro"
            assert len(f.repro) > 30, f"{path.name}: {f.detector} repro is too thin to act on"
            assert f.detail.strip(), f"{path.name}: {f.detector} on {f.job} has no detail"


def test_a_ship_job_on_a_pull_request_is_still_LOW():
    """The ship-job guard was unreachable from the test written for it: that fixture is
    tag-triggered, so `not _on_pull_request` already returned LOW one line later and deleting
    the guard changed nothing. This reaches the guard itself."""
    doc = yaml.safe_load("""
on: [pull_request]
jobs:
  prepare:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - run: echo prep
  publish:
    needs: [prepare]
    runs-on: ubuntu-latest
    steps:
      - run: twine upload dist/*
""")
    hits = [f for f in _findings(doc) if f.detector == "D1" and f.job == "publish"]
    assert hits, "expected a D1 finding on the publish job"
    assert all(f.severity == "LOW" for f in hits), (
        f"a release job is not a merge gate even on a PR: got {[f.severity for f in hits]}")


def test_a_check_job_on_a_pull_request_is_HIGH():
    """The other side of the same guard, so neither branch can be deleted silently."""
    doc = yaml.safe_load("""
on: [pull_request]
jobs:
  prepare:
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - run: echo prep
  verify-tests:
    needs: [prepare]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
""")
    hits = [f for f in _findings(doc) if f.detector == "D1" and f.job == "verify-tests"]
    assert hits and all(f.severity == "HIGH" for f in hits), (
        f"a PR verification job that can silently not run is HIGH: got {[f.severity for f in hits]}")


def test_d3_outside_a_pull_request_is_LOW():
    """b4 is `on: [push]` and does fire D3, but the corpus asserted only the detector id, so
    D3's first severity line was dead in the suite. cli.py hides LOW without --all, so this
    guard decides whether the finding is printed at all."""
    b4 = next(p for p in BROKEN if p.name.startswith("b4_"))
    doc = yaml.safe_load(b4.read_text())
    d3 = [f for f in _findings(doc) if f.detector == "D3"]
    assert d3, "b4 should still fire D3"
    assert all(f.severity == "LOW" for f in d3), (
        f"a push-only workflow is not a merge gate: got {[f.severity for f in d3]}")


@pytest.mark.parametrize("cond", [
    "${{ always() }}",
    "${{ !cancelled() }}",
    "${{ success() || failure() }}",
    "${{ failure() || success() }}",
])
def test_conditions_that_never_skip_raise_no_alarm(cond):
    """_NEVER_SKIPS exists because the detectors were wrong against a real 25k-star repo while
    the corpus was green. Two of its four alternatives had no fixture, so they could be removed
    and the false-alarm class they prevent would come straight back."""
    doc = yaml.safe_load(f"""
on: [pull_request]
jobs:
  build:
    if: "{cond}"
    runs-on: ubuntu-latest
    steps:
      - run: make
  verify-tests:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
""")
    d1 = [f for f in scan_workflow(doc) if f.detector == "D1"]
    assert not d1, f"{cond} runs in normal operation; flagging it is the false alarm g7 exists for"


def test_a_genuinely_skip_prone_condition_still_fires():
    """The other side, so the allowlist cannot be widened to swallow everything."""
    doc = yaml.safe_load("""
on: [pull_request]
jobs:
  build:
    if: "${{ github.actor != 'dependabot[bot]' }}"
    runs-on: ubuntu-latest
    steps:
      - run: make
  verify-tests:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
""")
    assert [f for f in scan_workflow(doc) if f.detector == "D1"], (
        "a condition that can evaluate false must still be reported")
