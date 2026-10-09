"""The correct fan-in gate, which this tool used to report as a broken one.

GitHub's documented idiom for "did anything upstream fail" is the WILDCARD form:

    if: always()
    run: |
      if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then exit 1; fi

Every detector that asked "does this job read needs.*.result" matched only
`needs.<name>.result`, through `[A-Za-z0-9_-]+`, which cannot match "*". So the one
spelling the documentation recommends was the one spelling the tool could not see, and the
finding's own text said "never reads needs.*.result" while failing to match that exact
string.

Measured on Arize-ai/openinference (1.2k stars, 22 workflows), whose go/java/python/
typescript `ci-required` jobs each read it twice: 25 findings and 13 HIGH before, 11 and 1
after, counting every severity. Twelve of the thirteen HIGH were false; the survivor is an
unrelated D3. All 149 tests passed both before and after, so nothing here was covered.
That is why these exist: a linter that cries wolf on correct CI is worse than no linter,
and this one shipped to PyPI before the gap was found.
"""
import textwrap

import pytest

import yaml

from deadgate.detectors import (
    d1_skippable_upstream,
)

CORRECT_GATE = textwrap.dedent("""
    jobs:
      changes:
        runs-on: ubuntu-latest
        outputs:
          py: ${{ steps.f.outputs.py }}
        steps:
          - id: f
            run: echo "py=true" >> "$GITHUB_OUTPUT"
      ci:
        needs: [changes]
        if: ${{ needs.changes.outputs.py == 'true' }}
        runs-on: ubuntu-latest
        steps:
          - run: pytest
      ci-required:
        needs: [changes, ci]
        if: always()
        runs-on: ubuntu-latest
        steps:
          - name: Check results
            run: |
              if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then
                exit 1
              fi
              if [[ "${{ contains(needs.*.result, 'cancelled') }}" == "true" ]]; then
                exit 1
              fi
""")


def jobs_of(src):
    return yaml.safe_load(src)["jobs"]
















# ---------------------------------------------------------------------------
# the THIRD spelling: serialise the whole context instead of naming anything
# ---------------------------------------------------------------------------
#
# A gate can consult every upstream without the word "result" appearing in any expression:
# bind toJSON(needs) into env and inspect it in the shell. astral-sh/ruff's
# `required-checks-passed` is written exactly this way. Against it, 0.1.1 reported 21 HIGH of
# which 18 were false, nine D1 and nine D4, one per job in the gate's needs list. Fixing the
# wildcard form and stopping there was the error.

RUFF_SHAPED_GATE = textwrap.dedent("""
    jobs:
      determine_changes:
        runs-on: ubuntu-latest
        outputs:
          code: ${{ steps.f.outputs.code }}
        steps:
          - id: f
            run: echo "code=true" >> "$GITHUB_OUTPUT"
      cargo-test-linux:
        needs: [determine_changes]
        if: ${{ needs.determine_changes.outputs.code == 'true' }}
        runs-on: ubuntu-latest
        steps: [{run: "cargo test"}]
      required-checks-passed:
        if: ${{ always() && github.ref != 'refs/heads/main' }}
        needs: [determine_changes, cargo-test-linux]
        runs-on: ubuntu-latest
        steps:
          - name: Check required jobs passed
            env:
              NEEDS_JSON: ${{ toJSON(needs) }}
            run: |
              failing=$(echo "$NEEDS_JSON" | jq -r 'to_entries[]
                | select(.value.result != "success" and .value.result != "skipped")
                | "\\(.key): \\(.value.result)"')
              if [ -n "$failing" ]; then echo "$failing"; exit 1; fi
""")












# ---------------------------------------------------------------------------
# the fourth class, which is placement rather than spelling
# ---------------------------------------------------------------------------

REUSABLE_CALL_GATE = textwrap.dedent("""
    jobs:
      check-sdist:
        runs-on: ubuntu-latest
        steps: [{run: "python -m build --sdist"}]
      update-tracker:
        uses: ./.github/workflows/update_tracking_issue.yml
        if: ${{ always() }}
        needs: [check-sdist]
        with:
          job_status: ${{ needs.check-sdist.result }}
""")









# ---------------------------------------------------------------------------
# D2: a reporting job's always() is the design, not the defect
# ---------------------------------------------------------------------------
#
# D2 fired on `always() + needs + no result read`, which is also the signature of every
# summary, report and cleanup job ever written. Measured in a pre-registered sample of 10
# D2 HIGH findings: NINE were reporting jobs, named report, e2e-log-summary,
# aggregate_reports, summary, cost, publish and release_lease. Zero were defensible.
# A lease-release job MUST run on always(); a summary that only ran on success would
# summarise nothing.







def test_all_checks_pass_matches_without_the_trailing_ed():
    # Regression. The pattern was `all-?checks?-?passed?`, and in a regex `passed?` is
    # "passe" with an optional "d", NOT "pass" with an optional "ed". So the single most
    # common spelling of this gate, `all-checks-pass`, did not match while
    # `all-checks-passed` did. A regex that looks right and is not.
    import deadgate.detectors as D
    assert D._GATE_NAME.search("all-checks-pass")
    assert D._GATE_NAME.search("all-checks-passed")








# ---------------------------------------------------------------------------
# D3: a corpus of shapes, from two hand-labelled samples
# ---------------------------------------------------------------------------
#
# D3 was never touched between 0.1.0 and 0.1.3, and both samples measured it at roughly
# half false: 8 of 17, then 11 of 20. The false ones were not random. They were three
# shapes, and every one of them is in the table below alongside the real findings they
# must not take with them.

D3_CASES = [
    # (label, run body, should_flag)
    ("if-condition echo|grep", 'if echo "$l" | grep -qxF "$n"; then\n  echo hit\nfi', False),
    ("if-condition with !", "if ! printf '%s' \"$V\" | grep -qE '^[0-9]'; then\n  exit 1\nfi", False),
    ("if-condition after &&", 'if [ -f "$f" ] && git check-attr filter "$f" | grep -q lfs; then\n  echo y\nfi', False),
    ("substitution head printf", "esc=$(printf '%s' \"$p\" | sed 's/x/y/')", False),
    ("substitution head arch", "ARCH=$(arch | sed -e 's/a/b/')", False),
    ("substitution head echo", 'py=cp$(echo "$V" | tr -d .)', False),
    ("value is only printed", 'echo "AAB size: $(du -h "$AAB" | cut -f1)"', False),
    ("while-condition", 'while docker ps | grep -q starting; do sleep 1; done', False),
    ("until-condition", 'until curl -s localhost | grep -q ok; do sleep 1; done', False),
    ("bare leading bang", '! git status --porcelain | grep -q .', False),
    # a pipeline is not always one line, and reading one physical line at a time made the
    # detector believe the head was whatever happened to start that line
    ("backslash continuation",
     "declared=$(printf '%s' \"$B\" | tr -d '\\r' \\\n  | sed -n 's/^A://p')", False),
    ("if continued with &&",
     'if [ -n "$f" ] \\\n  && printf \'%s\' "$d" | grep -Fxq "$f"; then\n  echo y\nfi', False),
    # the real ones, which must survive every narrowing above
    ("digest into GITHUB_OUTPUT",
     'digest="$(docker buildx imagetools inspect x | jq -r \'.digest\')"\n'
     'echo "d=$digest" >> "$GITHUB_OUTPUT"', True),
    ("score feeds a threshold",
     "SCORE=$(cat .lhci/lhr-*.json | jq -r '.categories.accessibility.score')\n"
     'if (( $(echo "$SCORE < 0.9" | bc -l) )); then exit 1; fi', True),
    ("sha256 of a release artifact",
     'echo "digest=$(cat "$T" | sha256sum | cut -d \' \' -f 1)" >> "$GITHUB_OUTPUT"', True),
]


@pytest.mark.parametrize("label,run,should_flag",
                         D3_CASES, ids=[c[0].replace(" ", "_") for c in D3_CASES])
def test_d3_shapes(label, run, should_flag):
    import deadgate.detectors as D
    src = {"on": ["push"], "jobs": {"j": {"runs-on": "ubuntu-latest",
                                          "steps": [{"name": "s", "run": run}]}}}
    got = bool([f for f in D.scan_workflow(src) if f.detector == "D3"])
    assert got is should_flag, f"{label}: flagged={got}, expected {should_flag}"


def test_substitution_bodies_sees_one_that_closes_the_line():
    # The off-by-one. The scan bound was `len(line) - 1`, so the FINAL character was never
    # examined and a `$(...)` closing at end of line never closed. `X=$(cmd | filter)`
    # almost always ends its line, which is precisely the shape the narrowing exists for,
    # so it could hardly ever fire. Everything above depends on this being right.
    import deadgate.detectors as D
    assert D._substitution_bodies("x=$(echo a | cut -f1)") == ["echo a | cut -f1"]
    assert D._substitution_bodies("x=$(echo a | cut -f1) ") == ["echo a | cut -f1"]


def test_a_pipeline_split_across_lines_is_judged_by_its_real_head():
    # The head here is `git log`, which can fail, not `cut`, which starts the second line.
    # Per-physical-line reading saw `cut` and got both the can-fail judgement and the
    # condition check wrong. This must still FLAG, so the joining cannot be a blanket mute.
    import deadgate.detectors as D
    run = ('ad="$(git log a..b --name-status |\n'
           '  cut --fields 2 | sort | uniq --repeated)"\n'
           'echo "$ad" >> "$GITHUB_OUTPUT"')
    src = {"on": ["push"], "jobs": {"j": {"runs-on": "ubuntu-latest",
                                          "steps": [{"name": "s", "run": run}]}}}
    assert [f.detector for f in D.scan_workflow(src) if f.detector == "D3"] == ["D3"]


def test_logical_lines_joins_the_three_continuation_shapes():
    # Asserted as a property, not as exact spacing: each shape must come back as ONE
    # logical line containing both halves, and unrelated lines must stay separate.
    import deadgate.detectors as D
    for src in ("a | \\\n  b", "x=$(a |\n  b)", "a |\n  b"):
        out = list(D._logical_lines(src))
        assert len(out) == 1, f"{src!r} -> {out}"
        assert "a" in out[0] and "b" in out[0], out
    assert list(D._logical_lines("a\nb")) == ["a", "b"]
    # a continuation caused ONLY by the unclosed $(, with no trailing pipe and no
    # backslash, so each of the three joining reasons is exercised on its own
    only_paren = 'ad="$(git log a..b\n  --name-status | cut -f2)"'
    out = list(D._logical_lines(only_paren))
    assert len(out) == 1 and "git log" in out[0] and "cut" in out[0], out
