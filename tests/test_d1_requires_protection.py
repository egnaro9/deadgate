"""D1 is reported ONLY when branch protection confirms the job is a required check.

D1's finding text says "a skipped job reports Success, and X never reads
needs.Y.result", which describes X running without its dependency. GitHub documents that
it does not run:

    "If a job fails or is skipped, all jobs that need it are skipped unless the jobs use
     a conditional expression that causes the job to continue."
    -- docs.github.com, jobs.<job_id>.needs

Measured on the 275-repo corpus, 373 D1 findings:

    255  68%  the dependent would be SKIPPED too, so the text is wrong and what remains
              is "a skipped required check counts as success", which needs protection data
    101  27%  the dependent does run, but it is release_lease, postSlackMessageOnFailure,
              ci-timing, merge-reports, consolidate-metrics: the reporting and notifier
              family the D2 census measured at 0 true of 27
     10   3%  WordPress/gutenberg status checks, known class-7 false positives
    ---
     ~7        candidates, precision never established

An uncapped 40-finding sample of the current population labelled about 92% false.

D4 was REMOVED for making an unverifiable branch-protection claim. D1 makes the same
claim, but unlike D4 the tool can check it: `--repo` reads the required contexts and
`resolve()` attributes a job to one. So the claim is gated on its evidence rather than
the detector deleted. Without `--repo`, D1 says nothing.
"""
import textwrap

import pytest

from deadgate.cli import main
from deadgate.protection import Protection, PROTECTED

WORKFLOW = textwrap.dedent("""
    on: [pull_request]
    jobs:
      build:
        if: ${{ github.event_name == 'push' }}
        runs-on: ubuntu-latest
        steps: [{run: make}]
      verify-tests:
        needs: [build]
        if: ${{ always() }}
        runs-on: ubuntu-latest
        steps: [{run: pytest}]
    """)


@pytest.fixture
def tree(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW)
    return tmp_path


def _run(tree, capsys, argv_extra=()):
    rc = main([str(tree), "--all", *argv_extra])
    return rc, capsys.readouterr().out


def test_d1_says_nothing_without_protection_data(tree, capsys):
    _, out = _run(tree, capsys)
    assert "[D1]" not in out, out


def test_the_workflow_really_does_trigger_d1(tree):
    """Without this the test above passes because the fixture produces no finding."""
    import yaml
    from deadgate.detectors import d1_skippable_upstream
    jobs = yaml.safe_load(WORKFLOW)["jobs"]
    assert [f.detector for f in d1_skippable_upstream(jobs, yaml.safe_load(WORKFLOW))] == ["D1"]


def test_d1_is_reported_when_protection_confirms_the_check(tree, capsys, monkeypatch):
    """With --repo and a required context that matches, the claim is checkable and made."""
    import deadgate.cli as cli
    monkeypatch.setattr(cli, "gh_api", lambda: object())
    monkeypatch.setattr(cli, "selftest", lambda api: (True, ""))
    monkeypatch.setattr(cli, "repo_meta", lambda repo, api: ("main", True))
    monkeypatch.setattr(cli, "fetch", lambda *a, **k: Protection(
        state=PROTECTED, required=frozenset({"verify-tests"}), complete=True))
    _, out = _run(tree, capsys, ("--repo", "o/r"))
    assert "[D1]" in out, out
    assert "REQUIRED" in out


def test_d1_stays_silent_when_protection_says_the_job_is_not_required(tree, capsys, monkeypatch):
    """The other direction. Protection data that does NOT attribute the job is not a
    licence to report it; it is evidence the claim is wrong."""
    import deadgate.cli as cli
    monkeypatch.setattr(cli, "gh_api", lambda: object())
    monkeypatch.setattr(cli, "selftest", lambda api: (True, ""))
    monkeypatch.setattr(cli, "repo_meta", lambda repo, api: ("main", True))
    monkeypatch.setattr(cli, "fetch", lambda *a, **k: Protection(
        state=PROTECTED, required=frozenset({"something-else"}), complete=True))
    _, out = _run(tree, capsys, ("--repo", "o/r"))
    assert "[D1]" not in out, out


def test_d3_is_unaffected_by_the_d1_gate(tmp_path, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    wf.joinpath("ci.yml").write_text(textwrap.dedent("""
        on: [pull_request]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  echo "d=$(sha256sum f | cut -d ' ' -f 1)" >> "$GITHUB_OUTPUT"
        """))
    main([str(tmp_path), "--all"])
    assert "[D3]" in capsys.readouterr().out
