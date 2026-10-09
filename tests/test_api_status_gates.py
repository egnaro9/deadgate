"""Class 7: a gate that reads upstream status from the GitHub Actions API, not `needs`.

The first six false-positive classes were all "the expression exists and my pattern
missed it", and each was fixed by widening a pattern. This one is different in kind.

WordPress/gutenberg's `*-status-check` jobs run

    - name: Check for any failures of required jobs
      env: { GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
      run: node .github/workflows/scripts/ci-status-check.js --ignore '<names>'

and the script pages `GET /repos/{repo}/actions/runs/{run_id}/jobs`, failing if any job
concluded as anything other than success or skipped. The word `result` never appears and
`needs` is never consulted, so NO pattern over the workflow file can find it: the evidence
lives in a file this tool does not parse. The gate is in fact MORE robust than the idiom
deadgate hunts for, because it also fails closed when a job never finished, which
`needs.*.result` cannot detect.

Measured: 10 of the 54 D1 findings in the 179-HIGH corpus were this shape, all false, all
one repository, and correcting them moved a hand-labelled census from 57% to 76% false.

So D1's claim ("never reads needs.X.result") is UNVERIFIABLE from the workflow alone once
a step shells out. The resolution is three-way rather than a wider regex:

  * the script is readable and reads status  -> suppress, having actually read it
  * the script is readable and does not      -> keep the claim at full severity
  * the script cannot be read                -> demote and say the claim is unverified

The token is what keeps this narrow. Reading another job's conclusion needs credentials,
so a script invoked WITHOUT a token in scope is not excused; otherwise every job that
runs ./build.sh would be demoted.
"""
import textwrap

import yaml

from deadgate.detectors import (
    _API_STATUS_READ,
    d1_skippable_upstream,
    d2_fanin_without_result_check,
)

# Verbatim from WordPress/gutenberg, including the SPACES inside `${ ... }`. The first
# version of _API_STATUS_READ used `[^\s"']*`, which cannot cross those spaces. It
# matched a string invented for a smoke test and never the artifact, and only a control
# run against the real file caught it. The spacing here is the point of the fixture.
REAL_URL = ('const url = `https://api.github.com/repos/${ GITHUB_REPOSITORY }'
            '/actions/runs/${ GITHUB_RUN_ID }/jobs?per_page=100&page=${ page }`;')

WORKFLOW = textwrap.dedent("""
    on: pull_request
    jobs:
      unit-js:
        if: ${{ github.event_name == 'pull_request' }}
        runs-on: ubuntu-latest
        steps: [{run: npm test}]
      unit-status-check:
        needs: [unit-js]
        if: ${{ ! cancelled() }}
        runs-on: ubuntu-latest
        steps:
          - name: Check for any failures of required jobs
            env:
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
            run: node .github/workflows/scripts/ci-status-check.js --ignore 'Unit Tests'
    """)


def _jobs():
    return yaml.safe_load(WORKFLOW)["jobs"]


def _tree(tmp_path, script_body=None):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    if script_body is not None:
        (wf / "scripts").mkdir()
        (wf / "scripts" / "ci-status-check.js").write_text(script_body)
    return tmp_path


RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# The same gate with its check INLINE instead of in a script. The guard cannot apply, so
# this is the severity the finding carries with no class-7 logic involved. Comparing
# against it tests the guard's effect rather than the severity model's absolute output,
# which depends on branch protection this fixture does not have.
INLINE = WORKFLOW.replace(
    "run: node .github/workflows/scripts/ci-status-check.js --ignore 'Unit Tests'",
    "run: npm run ci-status-check")


def _baseline():
    doc = yaml.safe_load(INLINE)
    out = d1_skippable_upstream(doc["jobs"], doc, None)
    assert len(out) == 1, out
    return out[0].severity


def _run(tmp_path, workflow=WORKFLOW, script_body=None, tree=True):
    doc = yaml.safe_load(workflow)
    base = _tree(tmp_path, script_body) if tree else None
    return d1_skippable_upstream(doc["jobs"], doc, base)


def test_the_regex_matches_the_real_artifact_not_a_tidier_one():
    """Guards the exact bug a control caught: whitespace inside the interpolation."""
    assert _API_STATUS_READ.search(REAL_URL)
    # and the tidier spelling that passed while the real one failed
    assert _API_STATUS_READ.search("actions/runs/${id}/jobs")


def test_the_baseline_gate_is_reported_at_all(tmp_path):
    """Without this the suppression tests below could pass on an empty list."""
    assert _baseline() in RANK


def test_a_readable_script_that_reads_the_api_suppresses_the_finding(tmp_path):
    assert _run(tmp_path, script_body=f"// gutenberg\n{REAL_URL}\n") == []


def test_a_readable_script_that_does_not_check_status_keeps_the_baseline(tmp_path):
    """The other direction. Delegation alone must not be an excuse."""
    out = _run(tmp_path, script_body="console.log('I only print things');\n")
    assert len(out) == 1
    assert out[0].severity == _baseline()
    assert "could not be verified" not in out[0].detail


def test_an_unreadable_script_is_demoted_and_says_so(tmp_path):
    """Script absent: the claim is unverifiable, so it is demoted, not asserted."""
    out = _run(tmp_path, script_body=None)
    assert len(out) == 1
    assert RANK[out[0].severity] < RANK[_baseline()], (out[0].severity, _baseline())
    assert "could not be verified" in out[0].detail


def test_no_tree_at_all_is_demoted_too(tmp_path):
    """Called as a library with no base, the claim is equally unverifiable."""
    out = _run(tmp_path, tree=False)
    assert len(out) == 1
    assert RANK[out[0].severity] < RANK[_baseline()]
    assert "could not be verified" in out[0].detail


def test_a_script_without_a_token_is_not_excused(tmp_path):
    """The narrowing condition. Without credentials a script cannot read run status, so
    an ordinary build script must not buy an exemption."""
    doc = yaml.safe_load(WORKFLOW)
    # Deleted structurally, not by string replace: dedent had already changed the
    # indentation my first replace() targeted, so it silently matched nothing and the
    # test exercised the WITH-token case. The assert below is what caught that.
    del doc["jobs"]["unit-status-check"]["steps"][0]["env"]
    assert "GITHUB_TOKEN" not in yaml.dump(doc), "the fixture must actually drop the token"
    base = _tree(tmp_path, None)
    out = d1_skippable_upstream(doc["jobs"], doc, base)
    assert len(out) == 1
    assert out[0].severity == _baseline()
    assert "could not be verified" not in out[0].detail


def test_a_job_that_is_not_a_named_gate_is_untouched(tmp_path):
    """Scoped to jobs that announce themselves as gates, so an ordinary job running a
    script with a token keeps its finding and its wording."""
    out = _run(tmp_path, workflow=WORKFLOW.replace("unit-status-check", "deploy-staging"),
               script_body=None)
    assert len(out) == 1
    assert "could not be verified" not in out[0].detail


# D2 carries the same claim and the same guard. Its class-7 path was added by symmetry
# with D1, and a mutation sweep found BOTH its suppression and its demotion unkilled --
# no test reached them. Checked for reachability before writing these rather than
# assuming: a gate on `always()` that delegates to a script does produce a D2 finding,
# and the demotion does fire, so this is live code that was untested, not dead code.
# `!cancelled()` does NOT satisfy _truthy_always, which is why gutenberg's own jobs
# reach D1 and never D2.
D2_WORKFLOW = textwrap.dedent("""
    on: pull_request
    jobs:
      unit-js:
        runs-on: ubuntu-latest
        steps: [{run: npm test}]
      required-status-check:
        needs: [unit-js]
        if: always()
        runs-on: ubuntu-latest
        steps:
          - name: Check for any failures of required jobs
            env:
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
            run: node .github/workflows/scripts/ci-status-check.js --ignore 'X'
    """)

D2_INLINE = D2_WORKFLOW.replace(
    "run: node .github/workflows/scripts/ci-status-check.js --ignore 'X'",
    "run: npm run ci-status-check")


def _d2(workflow, base):
    return d2_fanin_without_result_check(yaml.safe_load(workflow)["jobs"], base)


def test_d2_baseline_reports_the_always_gate():
    out = _d2(D2_INLINE, None)
    assert len(out) == 1 and out[0].severity in RANK, out


def test_d2_suppresses_when_the_script_reads_the_api(tmp_path):
    base = _tree(tmp_path, f"// gutenberg\n{REAL_URL}\n")
    assert _d2(D2_WORKFLOW, base) == []


def test_d2_demotes_an_unverifiable_delegation(tmp_path):
    base = _tree(tmp_path, script_body=None)
    out = _d2(D2_WORKFLOW, base)
    assert len(out) == 1
    assert RANK[out[0].severity] < RANK[_d2(D2_INLINE, None)[0].severity]
    assert "could not be verified" in out[0].detail


def test_d2_keeps_severity_when_the_script_does_not_check_status(tmp_path):
    base = _tree(tmp_path, "console.log('nope');\n")
    out = _d2(D2_WORKFLOW, base)
    assert len(out) == 1
    assert out[0].severity == _d2(D2_INLINE, None)[0].severity
    assert "could not be verified" not in out[0].detail


# ---------------------------------------------------------------------------
# A job that can never run. Not a new idiom, just a case the detector asserted
# something about anyway: 4 of the 44 surviving D1 HIGH findings were jobs whose own
# `if:` is a literal false, so they never execute and cannot report success on
# anything. Found by hand-labelling the whole D1 stratum rather than sampling it.
from deadgate.detectors import _never_runs

DISABLED = textwrap.dedent("""
    on: pull_request
    jobs:
      test-linux:
        if: ${{ github.event_name == 'pull_request' }}
        runs-on: ubuntu-latest
        steps: [{run: cargo test}]
      check-cache:
        needs: [test-linux]
        if: %s
        runs-on: ubuntu-latest
        steps: [{run: echo checking}]
    """)


def test_never_runs_recognises_every_spelling_yaml_produces():
    # A bare `false` becomes a bool; `${{ false }}` stays a string. Both are in the
    # corpus, so recognising only one of them would leave half the findings standing.
    for form in (False, "false", "FALSE", "${{ false }}", "${{ false  }}"):
        assert _never_runs(form), form


def test_never_runs_does_not_swallow_a_real_condition():
    """The negative direction. `false || true` RUNS, and must not be mistaken for
    a disabled job just because the word false appears in it."""
    for form in (True, None, "always()", "${{ false || true }}",
                 "${{ github.event_name == 'push' }}", "${{ !cancelled() }}"):
        assert not _never_runs(form), form


def test_a_disabled_dependent_produces_no_finding():
    for form in ("${{ false }}", "false"):
        jobs = yaml.safe_load(DISABLED % form)["jobs"]
        assert d1_skippable_upstream(jobs, {}, None) == [], form


def test_the_same_job_enabled_still_produces_one():
    """Without this the test above could pass because the fixture is broken."""
    jobs = yaml.safe_load(DISABLED % "${{ !cancelled() }}")["jobs"]
    assert len(d1_skippable_upstream(jobs, {}, None)) == 1
