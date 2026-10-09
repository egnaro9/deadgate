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


# ---------------------------------------------------------------------------
# the fifth class: a gate that spells its needs out longhand
# ---------------------------------------------------------------------------
#
# A gate can ask about every upstream without the wildcard and without toJSON, by naming
# each one: needs.a.result, needs.b.result, one expression per job. That is the same
# coverage written longhand, and it was invisible. Found by hand-labelling a 40-finding
# sample of 0.1.2's survivors: 6 of the 16 false positives were this, across scikit-learn,
# open-gsd, BasedHardware/omi and elie222/inbox-zero.

ENUMERATING_GATE = textwrap.dedent("""
    jobs:
      changes:
        runs-on: ubuntu-latest
        steps: [{run: "true"}]
      preflight:
        runs-on: ubuntu-latest
        steps: [{run: "true"}]
      test:
        needs: [changes, preflight]
        if: ${{ needs.changes.outputs.code == 'true' }}
        runs-on: ubuntu-latest
        steps: [{run: "pytest"}]
      required-tests:
        needs: [changes, preflight, test]
        if: ${{ always() }}
        runs-on: ubuntu-latest
        steps:
          - run: |
              test "${{ needs.changes.result }}" = success
              test "${{ needs.preflight.result }}" = success
              test "${{ needs.test.result }}" = success
    """)


def test_a_gate_that_enumerates_its_needs_by_name_covers_them():
    src = ENUMERATING_GATE
    assert "needs.*.result" not in src and "toJSON" not in src, "fixture must use the longhand form"
    assert d4_outputs_gate_without_result_check(jobs_of(src)) == []


def test_the_coverage_is_per_name_and_not_all_or_nothing():
    # The precision that stops this fix from becoming the same bug pointing the other way.
    # Drop ONE name from the gate and the job gated on that upstream comes back, while the
    # others stay suppressed. A gate naming eight of nine needs covers eight.
    src = "\n".join(l for l in ENUMERATING_GATE.splitlines()
                    if "needs.changes.result" not in l)
    assert "needs.changes.result" not in src
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    ups = sorted(f.detail.split("'")[3] for f in found)
    assert ups == ["changes"], f"only the unnamed upstream should return, got {ups}"


def test_naming_a_job_the_gate_does_not_need_covers_nothing():
    # A result read for a job outside the gate's own needs says nothing about that job,
    # because the gate does not wait for it. The intersection with `needs` is load-bearing.
    src = ENUMERATING_GATE.replace("needs.changes.result", "needs.unrelated.result")
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    assert [f.detail.split("'")[3] for f in found] == ["changes"]


def test_a_gate_naming_a_job_outside_its_needs_does_not_cover_that_job():
    # The `& direct` intersection, isolated. The gate names needs.changes.result but does
    # NOT need `changes`, so it does not wait for it and cannot report on it. Without the
    # intersection the mention alone would suppress the finding about `build`.
    src = textwrap.dedent("""
        jobs:
          changes:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          other:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          build:
            needs: [changes]
            if: ${{ needs.changes.outputs.code == 'true' }}
            runs-on: ubuntu-latest
            steps: [{run: "make"}]
          gate:
            needs: [other]
            if: ${{ always() }}
            runs-on: ubuntu-latest
            steps:
              - run: echo "${{ needs.changes.result }}"
        """)
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    assert [f.detail.split("'")[3] for f in found] == ["changes"], (
        "a gate that does not need `changes` cannot cover it, however it is mentioned")


def test_reading_an_upstreams_outputs_is_not_reading_its_result():
    # The `.result` anchor, isolated. A gate that reads needs.changes.outputs.* consults a
    # VALUE the job produced, which tells it nothing about whether the job failed: a failed
    # job produces no outputs and the read is empty, which is the whole defect D4 describes.
    src = ENUMERATING_GATE.replace("needs.changes.result", "needs.changes.outputs.code")
    found = d4_outputs_gate_without_result_check(jobs_of(src))
    assert [f.detail.split("'")[3] for f in found] == ["changes"]


# ---------------------------------------------------------------------------
# D4 severity: MEDIUM when nothing gates, LOW when something does
# ---------------------------------------------------------------------------

def _d4(src):
    import deadgate.detectors as D
    d = yaml.safe_load(src)
    return [f for f in D.scan_workflow(d) if f.detector == "D4"]


D4_NO_GATE = textwrap.dedent("""
    on: [pull_request]
    jobs:
      changes:
        runs-on: ubuntu-latest
        steps: [{run: "true"}]
      test:
        needs: [changes]
        if: ${{ needs.changes.outputs.code == 'true' }}
        runs-on: ubuntu-latest
        steps: [{run: "pytest"}]
    """)


def test_d4_is_medium_when_no_job_in_the_workflow_gates_at_all():
    # The strongest D4 case: nothing anywhere consults an upstream result, so the skip is
    # genuinely unobserved. 126 of the 196 D4 HIGH on the corpus were this shape.
    f = _d4(D4_NO_GATE)
    assert [x.severity for x in f] == ["MEDIUM"], f


def test_d4_drops_to_low_when_the_workflow_gates_but_not_over_this_upstream():
    # Weaker on purpose. 55% of real gates hand the decision to a script this tool cannot
    # read, so "a gate exists but misses this one upstream" is the claim most likely wrong.
    src = D4_NO_GATE + (
        "  gate:\n"
        "    needs: [test]\n"
        "    if: ${{ always() }}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: test "${{ needs.test.result }}" = success\n')
    assert "gate:" in yaml.safe_load(src)["jobs"] or "gate" in yaml.safe_load(src)["jobs"], \
        "the gate must actually be nested under jobs:"
    f = _d4(src)
    assert [x.severity for x in f] == ["LOW"], f


def test_d4_never_claims_branch_protection_as_fact():
    # The repro used to assert "any branch protection requiring it passes with nothing
    # tested". That is conditional on which checks are required, which a workflow cannot say.
    for x in _d4(D4_NO_GATE):
        assert "cannot say" in x.repro
        assert "passes with nothing tested" not in x.repro


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

def _d2(jobname, run='echo "all good"'):
    import deadgate.detectors as D
    src = textwrap.dedent(f"""
        on: [pull_request]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps: [{{run: "make"}}]
          {jobname}:
            needs: [build]
            if: always()
            runs-on: ubuntu-latest
            steps:
              - run: {run}
        """)
    return [f for f in D.scan_workflow(yaml.safe_load(src)) if f.detector == "D2"]


def test_d2_reporting_jobs_are_low():
    for n in ("report", "e2e-log-summary", "aggregate-reports", "cost",
              "release-lease", "publish", "notify", "upload-artifacts"):
        f = _d2(n)
        assert f and f[0].severity == "LOW", f"{n}: {f}"


def test_d2_jobs_that_name_themselves_a_gate_stay_medium():
    for n in ("ci-required", "required-tests", "status-check", "merge-queue",
              "all-checks-pass", "all-checks-passed"):
        f = _d2(n)
        assert f and f[0].severity == "MEDIUM", f"{n}: {f}"


def test_all_checks_pass_matches_without_the_trailing_ed():
    # Regression. The pattern was `all-?checks?-?passed?`, and in a regex `passed?` is
    # "passe" with an optional "d", NOT "pass" with an optional "ed". So the single most
    # common spelling of this gate, `all-checks-pass`, did not match while
    # `all-checks-passed` did. A regex that looks right and is not.
    import deadgate.detectors as D
    assert D._GATE_NAME.search("all-checks-pass")
    assert D._GATE_NAME.search("all-checks-passed")


def test_a_gate_name_beats_a_reporting_word():
    # "required-report" is a gate that also reports. The gate reading wins, because being
    # wrong about a gate costs more than being wrong about a report.
    f = _d2("required-report")
    assert f and f[0].severity == "MEDIUM", f


def test_d2_an_unrecognised_job_name_is_low_not_medium():
    # The `else` branch, isolated. A job that neither names itself a gate nor looks like a
    # report still only reaches LOW, because the MEDIUM claim needs a reason and "we could
    # not tell what this job is" is not one.
    f = _d2("widget-shuffler")
    assert f and f[0].severity == "LOW", f


def test_check_alone_is_not_a_gate_name():
    # The gate pattern is deliberately narrow. `check` on its own matches change-detection
    # jobs, which are not gates and whose always() is ordinary. sgl-project/sglang's
    # `check-changes` was one of the nine false D2 findings in the sample, so widening
    # `status-check` to `check` would put it straight back.
    for n in ("check-changes", "check-files", "checkout-helper"):
        f = _d2(n)
        assert f and f[0].severity == "LOW", f"{n}: {f}"
    # and the narrow forms still work
    for n in ("status-check", "ci-required"):
        f = _d2(n)
        assert f and f[0].severity == "MEDIUM", f"{n}: {f}"


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
