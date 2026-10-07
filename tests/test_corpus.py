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


def test_d4_pr_verification_job_is_high():
    f = [x for x in _scan(_yaml.safe_load(_PR_TEST)) if x.detector == "D4"]
    assert f and f[0].severity == "HIGH", f


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
