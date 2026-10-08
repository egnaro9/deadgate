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


def test_a_gate_that_serialises_the_whole_needs_context_is_not_reported():
    jobs = jobs_of(RUFF_SHAPED_GATE)
    assert "needs.*.result" not in RUFF_SHAPED_GATE, "fixture must use the toJSON form only"
    assert d2_fanin_without_result_check(jobs) == []
    assert d1_skippable_upstream(jobs) == []
    assert d4_outputs_gate_without_result_check(jobs) == []


def test_the_context_form_is_found_in_env_not_only_in_a_run_body():
    # toJSON(needs) is bound in env: and read from a shell variable, never appearing in the run
    # body, so a scan of run bodies alone reads the gate as checking nothing. _steps_text
    # already covered step env, step with and job env; a helper added here to "fix" that was
    # dead code, and the mutation sweep proved it by surviving its own deletion.
    jobs = jobs_of(RUFF_SHAPED_GATE)
    gate = jobs["required-checks-passed"]
    assert "toJSON" not in str(gate["steps"][0].get("run"))
    assert "toJSON" in str(gate["steps"][0]["env"])
    assert d4_outputs_gate_without_result_check(jobs) == []


def test_a_gate_conditioned_on_more_than_always_still_counts():
    # `always() && github.ref != ...` is the normal shape. Requiring the bare always() left
    # nine false D4/HIGH on ruff even after the context form was understood.
    assert "always() && github.ref" in RUFF_SHAPED_GATE
    assert d4_outputs_gate_without_result_check(jobs_of(RUFF_SHAPED_GATE)) == []


def test_serialising_a_different_context_does_not_count():
    # toJSON(github) says nothing about upstream jobs. The suppression must key on `needs`,
    # not on the presence of toJSON.
    src = RUFF_SHAPED_GATE.replace("toJSON(needs)", "toJSON(github)")
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    assert [f.detector for f in found] == ["D4"], "toJSON(github) must not suppress anything"


def test_a_gate_with_no_always_at_all_does_not_cover_its_needs():
    # The other half of the leniency. Without always(), a gate does not run when an upstream
    # fails, so it covers nothing and the finding must stand.
    src = RUFF_SHAPED_GATE.replace("if: ${{ always() && github.ref != 'refs/heads/main' }}",
                                   "if: ${{ github.ref != 'refs/heads/main' }}")
    assert "always()" not in src
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    assert [f.detector for f in found] == ["D4"]


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


def test_a_reusable_workflow_call_that_passes_a_result_is_not_reported():
    # scikit-learn's check-sdist.yml. A job calling a reusable workflow has `uses:` and NO
    # `steps:`, and passes state through JOB-level `with:`. The expression is one the detectors
    # already understood; it simply lived where nothing looked, because step-level `with` was
    # scanned and job-level `with` was not. The job reads its upstream, so nothing is reported.
    jobs = jobs_of(REUSABLE_CALL_GATE)
    assert "steps" not in jobs["update-tracker"], "fixture must be a reusable-workflow call"
    assert "needs.check-sdist.result" in str(jobs["update-tracker"]["with"])
    assert d2_fanin_without_result_check(jobs) == []
    assert d1_skippable_upstream(jobs) == []


def test_the_same_call_without_the_result_is_still_reported():
    # The counter-case, so searching the whole job cannot become a blanket mute: drop the
    # result from `with:` and the gate genuinely checks nothing.
    src = REUSABLE_CALL_GATE.replace("job_status: ${{ needs.check-sdist.result }}",
                                     "job_status: unknown")
    assert "needs.check-sdist.result" not in src
    assert [f.detector for f in d2_fanin_without_result_check(jobs_of(src))] == ["D2"]


def test_a_conditional_reusable_call_that_checks_the_result_is_not_a_d4():
    # D4's own read of the result had the same placement blindness: a job can be gated on an
    # upstream's OUTPUTS and still check that upstream's RESULT, with both living at job level
    # because a reusable call has no steps to put them in.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          publish:
            uses: ./.github/workflows/publish.yml
            needs: [changes]
            if: ${{ needs.changes.outputs.release == 'true' }}
            with:
              upstream_status: ${{ needs.changes.result }}
    """)
    assert d4_outputs_gate_without_result_check(jobs_of(src)) == []
    # and without the result, the finding stands
    bare = src.replace("upstream_status: ${{ needs.changes.result }}", "upstream_status: na")
    assert [f.detector for f in d4_outputs_gate_without_result_check(jobs_of(bare))] == ["D4"]


def test_a_gate_that_forwards_the_whole_context_to_a_reusable_workflow_covers_its_needs():
    # The coverage check too: a fan-in gate implemented as a reusable call forwards
    # toJSON(needs) through job-level `with:`, so the whole-context read is not in any step.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          build:
            needs: [changes]
            if: ${{ needs.changes.outputs.code == 'true' }}
            runs-on: ubuntu-latest
            steps: [{run: "make"}]
          gate:
            uses: ./.github/workflows/report.yml
            if: ${{ always() }}
            needs: [changes, build]
            with:
              results: ${{ toJSON(needs) }}
    """)
    assert d4_outputs_gate_without_result_check(jobs_of(src)) == []
    nogate = src.replace("results: ${{ toJSON(needs) }}", "results: none")
    assert [f.detector for f in d4_outputs_gate_without_result_check(jobs_of(nogate))] == ["D4"]
