"""The D1 gate, exercised on the shape of a REAL required-check set.

`test_d1_requires_protection` stubs `fetch` out entirely and hands `resolve` a job key
that is literally equal to the required context. That proves the CLI's gate branch and
nothing about whether a real protection payload can reach it. The composition
`fetch -> derive -> resolve -> REQUIRED` had never fired on real data.

So it was measured. 2026-10-09, all 373 D1 findings on the 275-repo corpus, live
read-only reads through the shipped `protection.fetch`:

    73 repos carry D1 findings
    49 UNREADABLE   the rulesets endpoint returns nothing and we have no admin
    24 PROTECTED    a non-empty required set, every one of them INCOMPLETE
     0 admin        so `complete` was never True on any repository in the corpus

    verdicts over the 373:   UNREADABLE 237   AMBIGUOUS 126   REQUIRED 10

All 10 REQUIRED were WordPress/gutenberg `*-status-check` jobs. They fired only
because the corpus cache holds workflow YAML with no `scripts/` directory beside it.
On a real tree `_script_reads_status` reads `ci-status-check.js`, finds it listing the
run's jobs and inspecting their conclusions, and suppresses all three BEFORE the gate
is consulted. The true firing rate on complete trees is 0 of 373.

That is worth a test in both directions, because the two facts are easy to confuse:
the gate WORKS -- a 'Unit Tests - Status Check' context really does attribute to a job
keyed `unit-status-check` through the name derivation -- and it selects nothing,
because the one candidate it found was correctly suppressed by a different predicate.
A test that only covered the firing case would read as validation of a mode that has
never reported a finding on a real repository.

The payloads below are the real ones, captured from the live API on 2026-10-09 and
trimmed to the fields `fetch` reads.
"""
import json
import textwrap

import pytest

from deadgate.cli import main
from deadgate.protection import PROTECTED, fetch, repo_meta
from deadgate.resolve import REQUIRED, attribute, resolve

# GET /repos/WordPress/gutenberg/rules/branches/trunk, the required_status_checks rule.
GUTENBERG_RULESETS = [
    {"type": "creation"},
    {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": False,
     "required_status_checks": [
        {"context": "All", "integration_id": 15368},
        {"context": "Build Release Artifact", "integration_id": 15368},
        {"context": "End-to-End Tests - Status Check", "integration_id": 15368},
        {"context": "Performance Tests - Status Check", "integration_id": 15368},
        {"context": "Required changes from trunk", "integration_id": 15368},
        {"context": "Run performance tests", "integration_id": 15368},
        {"context": "Storybook Build and Smoke Tests", "integration_id": 15368},
        {"context": "Unit Tests - Status Check", "integration_id": 15368}]}},
]

# The job as gutenberg declares it: a status-check aggregator whose check-run name is a
# string that is NOT its job key, over needs that are individually skippable.
WORKFLOW = textwrap.dedent("""
    on: [pull_request]
    jobs:
      detect-relevant-changes:
        if: ${{ github.event_name == 'pull_request' }}
        runs-on: ubuntu-latest
        outputs:
          has-unit-tests: ${{ steps.changed.outputs.unit }}
        steps: [{id: changed, run: ./detect.sh}]
      unit-js:
        needs: [detect-relevant-changes]
        if: ${{ needs.detect-relevant-changes.outputs.has-unit-tests == 'true' }}
        runs-on: ubuntu-latest
        steps: [{run: npm run test:unit}]
      unit-status-check:
        name: 'Unit Tests - Status Check'
        needs: [detect-relevant-changes, unit-js]
        if: ${{ ! cancelled() }}
        runs-on: ubuntu-latest
        permissions: {contents: read, actions: read}
        steps:
          - name: Checkout repository
            uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
            with: {sparse-checkout: .github/workflows/scripts}
          - name: Check for any failures of required jobs
            env:
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
            run: node .github/workflows/scripts/ci-status-check.js --ignore 'Unit Tests - Status Check'
    """)

# The shape that matters in ci-status-check.js: it lists the run's jobs from the API and
# reads their `conclusion`. Byte-for-byte irrelevant; this is what the predicate looks for.
STATUS_SCRIPT = textwrap.dedent("""
    const response = await fetch(
      `https://api.github.com/repos/${ GITHUB_REPOSITORY }/actions/runs/${ GITHUB_RUN_ID }/jobs`,
      { headers: { Authorization: `Bearer ${ GITHUB_TOKEN }` } }
    );
    const failed = jobs.filter( ( job ) => ! PASSING_CONCLUSIONS.includes( job.conclusion ) );
    """)


def _api(rules_status=200, rules=None, classic=(404, {"message": "Not Found"}), branch=(200, {})):
    """A transport returning the real payload shapes. No admin, as on any public repo."""
    def call(path):
        if "rules/branches" in path:
            return (rules_status, GUTENBERG_RULESETS if rules is None else rules)
        if path.endswith("/protection"):
            return classic
        return branch
    return call


# ------------------------------------------------------------------ the gate can fire

def test_the_real_payload_reads_as_protected_and_incomplete():
    """`fetch` on the live shape: a required set, and no licence to clear anything."""
    prot = fetch("WordPress/gutenberg", "trunk", _api(), admin=False)
    assert prot.state == PROTECTED
    assert "Unit Tests - Status Check" in prot.required
    assert len(prot.required) == 8
    # No admin, so the classic 404 is not authoritative and the set is not complete.
    # This is what makes NOT_REQUIRED unreachable on anyone else's repository.
    assert prot.complete is False


def test_derive_attributes_a_named_job_to_its_required_context():
    """The derivation has to do real work here: the context is a `name:`, not the job key."""
    import yaml
    jobs = yaml.safe_load(WORKFLOW)["jobs"]
    prot = fetch("WordPress/gutenberg", "trunk", _api(), admin=False)
    att = attribute(prot, {".github/workflows/unit-test.yml": jobs})
    assert att.attributed == {
        "Unit Tests - Status Check": (".github/workflows/unit-test.yml::unit-status-check",)}
    # The instrument check: the other seven are external or live in other files, which is
    # legitimate. `suspicious` stays False precisely because one DID attribute.
    assert att.suspicious is False
    assert len(att.unattributed) == 7


def test_resolve_returns_required_on_the_real_payload():
    import yaml
    jobs = yaml.safe_load(WORKFLOW)["jobs"]
    prot = fetch("WordPress/gutenberg", "trunk", _api(), admin=False)
    r = resolve("FINDING", "unit-status-check", jobs["unit-status-check"], prot)
    assert r.verdict == REQUIRED
    assert "Unit Tests - Status Check" in r.why


def test_cli_reports_d1_when_the_script_is_absent(tmp_path, capsys, monkeypatch):
    """End to end through the shipped CLI, with only the HTTP transport faked.

    This is the configuration the corpus measured: workflow YAML with no script beside
    it. The gate fires. It is also the configuration that does not exist in a real
    checkout, which is the next test.
    """
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "unit-test.yml").write_text(WORKFLOW)
    import deadgate.cli as cli
    monkeypatch.setattr(cli, "gh_api", _api)
    monkeypatch.setattr(cli, "selftest", lambda api: (True, "ok"))
    monkeypatch.setattr(cli, "repo_meta", lambda repo, api: ("trunk", False))
    main([str(tmp_path), "--repo", "WordPress/gutenberg"])
    out = capsys.readouterr().out
    assert "[D1] .github/workflows/unit-test.yml::unit-status-check" in out
    assert "REQUIRED: required on the protected branch as 'Unit Tests - Status Check'" in out


def test_script_suppression_beats_the_gate(tmp_path, capsys, monkeypatch):
    """The same tree with the script present. D1 goes silent even though it IS required.

    This is why the measured firing rate is 0 and not 10: suppression is evaluated
    before the finding is built, so a job that reads upstream conclusions from the API
    never reaches the gate. Dropping the script back in flips the assertion above.
    """
    wf = tmp_path / ".github" / "workflows"
    (wf / "scripts").mkdir(parents=True)
    (wf / "unit-test.yml").write_text(WORKFLOW)
    (wf / "scripts" / "ci-status-check.js").write_text(STATUS_SCRIPT)
    import deadgate.cli as cli
    monkeypatch.setattr(cli, "gh_api", _api)
    monkeypatch.setattr(cli, "selftest", lambda api: (True, "ok"))
    monkeypatch.setattr(cli, "repo_meta", lambda repo, api: ("trunk", False))
    main([str(tmp_path), "--repo", "WordPress/gutenberg"])
    out = capsys.readouterr().out
    assert "[D1]" not in out, out
    # And it is suppressed as unattributable-or-suppressed, not silently dropped.
    assert "required checks: 1/8 attributed" in out


def test_the_script_shape_is_what_the_predicate_actually_matches():
    """Guards against the test above passing because the predicate matches nothing.

    `_API_STATUS_READ` was once written with `[^\\s"']*`, which cannot cross the spaces in
    the real `${ GITHUB_RUN_ID }`. It matched an invented smoke-test string and never the
    artifact, so the suppression it gated never ran on any real repository.
    """
    from deadgate.detectors import _API_STATUS_READ
    assert _API_STATUS_READ.search(STATUS_SCRIPT) is not None


def test_the_fixture_carries_the_token_scope_the_predicate_requires():
    """The first draft of WORKFLOW above dropped the step's `env: GITHUB_TOKEN:`.

    `_delegated_status_scripts` then returned [], the suppression never ran, and
    `test_script_suppression_beats_the_gate` failed against a fixture that looked right.
    The real job carries the token on the step, not the job, and that is the field
    `_token_in_scope` reads. Asserting the extraction, not just the file contents, is
    what makes the suppression test capable of failing for the right reason.
    """
    import yaml
    from deadgate.detectors import _delegated_status_scripts
    job = yaml.safe_load(WORKFLOW)["jobs"]["unit-status-check"]
    assert _delegated_status_scripts(job) == [
        ".github/workflows/scripts/ci-status-check.js"]


# ------------------------------------------------------- the clearing path is unreachable

def test_not_required_needs_admin_which_no_public_repo_grants():
    """Why the corpus produced 126 AMBIGUOUS and zero NOT_REQUIRED.

    Without admin a classic 404 proves nothing, `complete` stays False, and `resolve`
    refuses to clear. Measured 2026-10-09: 0 of 73 corpus repos grant admin, and 0 of
    43 repos we DO administer carry any required status check at all (36 answer 404
    "not protected", 7 answer 403 because private-repo protection needs a paid plan).
    So this branch has never been exercised against live data anywhere.
    """
    import yaml
    jobs = yaml.safe_load(WORKFLOW)["jobs"]
    no_match = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": "something-else"}]}}]

    without_admin = fetch("o/r", "main", _api(rules=no_match), admin=False)
    r = resolve("FINDING", "unit-status-check", jobs["unit-status-check"], without_admin)
    assert r.verdict != REQUIRED
    assert "incomplete" in r.why

    with_admin = fetch("o/r", "main", _api(rules=no_match), admin=True)
    assert with_admin.complete is True
    r = resolve("FINDING", "unit-status-check", jobs["unit-status-check"], with_admin)
    assert r.verdict == "NOT_REQUIRED"


def test_json_payload_is_the_shape_the_api_returns():
    """A trimmed fixture that no longer parses like the real thing is not evidence."""
    assert json.loads(json.dumps(GUTENBERG_RULESETS)) == GUTENBERG_RULESETS
    rule = [r for r in GUTENBERG_RULESETS if r["type"] == "required_status_checks"][0]
    assert all("context" in c for c in rule["parameters"]["required_status_checks"])


# ----------------------------------------------------- token scope, found by a survivor

def test_job_level_token_scope_is_what_the_step_blob_cannot_see():
    """The live half of `_token_in_scope`, which had no test in 221.

    A mutation emptying the env loop survived the WHOLE suite. The reason split in two:
    `step.get("env")` could never contribute, because `_job_blob(step)` serialises the
    step including its env; and `job.get("env")` is the only thing the step blob cannot
    see, so dropping it silently stops class 7 suppression for every job that scopes the
    token at the job level. That is a detector going quiet, which is this tool's own
    failure mode.
    """
    from deadgate.detectors import _job_blob, _token_in_scope
    step = {"run": "node .github/workflows/scripts/gate.js"}
    job = {"env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}, "steps": [step]}
    assert _token_in_scope(job, step) is True
    # The discriminator: without the job env there is nothing in the step to find.
    assert "GITHUB_TOKEN" not in _job_blob(step).upper()
    assert _token_in_scope({}, step) is False


def test_step_level_token_scope_still_works_through_the_blob():
    """Removing the redundant branch must not change the answer for a step-scoped token."""
    from deadgate.detectors import _token_in_scope
    step = {"env": {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}, "run": "node gate.js"}
    assert _token_in_scope({}, step) is True
