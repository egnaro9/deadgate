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

import yaml

from deadgate.detectors import (
    d1_skippable_upstream,
    d2_fanin_without_result_check,
    d4_outputs_gate_without_result_check,
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


def test_a_wildcard_fanin_gate_is_not_a_gate_that_cannot_fail():
    jobs = jobs_of(CORRECT_GATE)
    assert d2_fanin_without_result_check(jobs) == []
    assert d1_skippable_upstream(jobs) == []


def test_a_wildcard_gate_covers_the_job_it_transitively_needs():
    # D4's premise is that a failed `changes` skips `ci` and the skip reports Success, so
    # the merge is green with nothing tested. The gate reads every upstream's result, so it
    # sees `changes` fail and the merge is NOT green. Reporting it anyway states an outcome
    # that does not happen.
    assert d4_outputs_gate_without_result_check(jobs_of(CORRECT_GATE)) == []


def test_the_same_workflow_without_the_gate_is_still_reported():
    # The mirror, so the suppression cannot swallow the real defect: delete the gate job
    # and every finding must come back. A suppression with no counter-case is a mute button.
    src = CORRECT_GATE[: CORRECT_GATE.index("  ci-required:")]
    jobs = jobs_of(src)
    assert [f.detector for f in d4_outputs_gate_without_result_check(jobs)] == ["D4"]


def test_a_gate_that_only_names_some_upstreams_still_reports_the_others():
    # The explicit form checks exactly what it names. `changes` is covered, `other` is not,
    # so `other` must still be reported: the wildcard fix must not make the explicit form
    # behave like a wildcard.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          other:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          ci:
            needs: [changes, other]
            if: ${{ needs.changes.outputs.py == 'true' && needs.other.outputs.js == 'true' }}
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo "${{ needs.changes.result }}"
    """)
    ups = sorted(f.detail.split("'")[3] for f in
                 d4_outputs_gate_without_result_check(jobs_of(src)))
    assert ups == ["other"], f"expected only 'other' to be reported, got {ups}"


def test_a_gate_that_reads_nothing_is_reported_even_on_always():
    # always() with no result check at all: the original defect, still a finding. Built
    # explicitly rather than by editing the string above, because a surgical replace that
    # silently misses leaves a test asserting the wrong program.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          ci:
            needs: [changes]
            runs-on: ubuntu-latest
            steps: [{run: "pytest"}]
          ci-required:
            needs: [changes, ci]
            if: always()
            runs-on: ubuntu-latest
            steps:
              - run: echo "all good"
    """)
    jobs = jobs_of(src)
    assert "needs.*.result" not in src, "the fixture must not contain the thing under test"
    assert [f.detector for f in d2_fanin_without_result_check(jobs)] == ["D2"]


def test_a_gate_does_not_cover_a_grandparent_it_never_needed():
    # The boundary that makes the suppression sound. `needs.*.result` reports each DIRECT
    # need and nothing deeper: a failed `changes` makes `ci` SKIP, skipped is not failure,
    # so this gate passes and the failure really is invisible. `prep` must still be
    # reported. A transitive version of the coverage check suppressed this, and no test
    # caught it until the mutation that deleted the walk also survived.
    src = textwrap.dedent("""
        jobs:
          prep:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          ci:
            needs: [prep]
            if: ${{ needs.prep.outputs.go == 'true' }}
            runs-on: ubuntu-latest
            steps: [{run: "pytest"}]
          ci-required:
            needs: [ci]
            if: always()
            runs-on: ubuntu-latest
            steps:
              - run: |
                  if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then exit 1; fi
    """)
    ups = sorted(f.detail.split("'")[3] for f in
                 d4_outputs_gate_without_result_check(jobs_of(src)))
    assert ups == ["prep"], f"a gate over [ci] cannot see prep fail; got {ups}"


def test_a_job_that_wildcard_checks_its_own_upstreams_is_not_reported():
    # The other path: no separate gate job, but the conditional job itself reads
    # needs.*.result in its steps. It is checking the very upstream its `if` depends on, so
    # it is correct and must not be reported.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          ci:
            needs: [changes]
            if: ${{ needs.changes.outputs.py == 'true' }}
            runs-on: ubuntu-latest
            steps:
              - run: |
                  if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then exit 1; fi
                  pytest
    """)
    assert d4_outputs_gate_without_result_check(jobs_of(src)) == []
