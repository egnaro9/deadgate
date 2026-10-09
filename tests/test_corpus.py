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


# ---------------------------------------------------------------------------
# D4 is REMOVED, not demoted. This test exists so it cannot come back quietly.
#
# It scored 0 defensible findings out of 18 in a pre-registered hand-labelled sample, and
# it was the largest detector by volume: 932 findings over 93 repositories, 47% of
# everything the tool emitted. 0.1.3 capped it below HIGH and left it reporting, which was
# the wrong fix: a detector that has never once been right is not improved by saying it
# quietly, and 724 LOW findings still cost a reader attention.
#
# The shape it fired on is a real hazard and is now UNDETECTED BY DESIGN: if a
# change-detection job FAILS rather than decides, its outputs are unset, the condition is
# false, the dependent skips, and a skipped job reports Success. The reason that is not
# reported is that the file cannot distinguish it from the intended optimisation, and
# whether the skip matters depends on which checks are required, which no workflow states.

D4_SHAPE = """
on: [pull_request]
jobs:
  detect:
    runs-on: ubuntu-latest
    outputs:
      rust: ${{ steps.filter.outputs.rust }}
    steps:
      - id: filter
        run: echo "rust=true" >> "$GITHUB_OUTPUT"
  test:
    needs: [detect]
    if: ${{ needs.detect.outputs.rust == 'true' }}
    runs-on: ubuntu-latest
    steps: [{run: cargo test}]
"""


def test_no_detector_is_registered_as_D4():
    from deadgate.detectors import active_detectors
    assert not [f for f in active_detectors() if "d4" in f.__name__.lower()], \
        "D4 was removed in 0.1.5 on a measured 0-of-18; re-adding needs new evidence"


def test_the_outputs_gate_shape_reports_nothing():
    """The fixture D4 used to own. Undetected by design, asserted so the silence is
    deliberate rather than accidental."""
    assert [f.detector for f in _scan(_yaml.safe_load(D4_SHAPE))] == []


def test_the_corpus_holds_no_fixture_expecting_D4():
    for path in BROKEN:
        assert _expected(path) != "D4", f"{path.name} still expects a removed detector"


# ---------------------------------------------------------------------------
# D2 is REMOVED, on a CENSUS rather than a sample. This test keeps it removed.
#
# Every one of its 27 findings across 275 repositories was hand-labelled: 0 true, 27
# false, 0 arguable. Its trigger, always() + needs + no result read, is the signature of a
# correctly written reporting job: report, summary, cost, merge-reports, aggregate_reports,
# release_lease, and a cleanup job restoring an environment policy that MUST run whatever
# happened.
#
# The structural argument is stronger than the rate. D2 reached MEDIUM only when _GATE_NAME
# matched; all 27 were LOW, so zero matched. By its own severity logic it never once found
# a job it believed was a gate, while every finding it emitted said that job's branch
# protection was decorative.

D2_SHAPE = """
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  summary:
    needs: [test]
    if: always()
    runs-on: ubuntu-latest
    steps: [{run: "echo '## results' >> $GITHUB_STEP_SUMMARY"}]
"""


def test_no_detector_is_registered_as_D2():
    from deadgate.detectors import active_detectors
    assert not [f for f in active_detectors() if "d2" in f.__name__.lower()], \
        "D2 was removed on a 0-of-27 census; re-adding needs new evidence"


def test_a_reporting_job_on_always_reports_nothing():
    """The shape D2 owned. A summary job running on always() is the design."""
    assert [f.detector for f in _scan(_yaml.safe_load(D2_SHAPE))] == []


def test_only_the_two_surviving_detectors_are_registered():
    from deadgate.detectors import active_detectors
    names = sorted(f.__name__ for f in active_detectors())
    assert names == ["d1_skippable_upstream", "d3_pipe_masked_exit"], names
