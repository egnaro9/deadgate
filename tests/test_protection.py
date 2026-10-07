"""The API tier must never turn an unread answer into a clean one.

Every test here is a fail-open the implementation actually exhibited or would have exhibited.
"""
import pytest

from deadgate.checknames import AMBIGUOUS, EXACT, derive
from deadgate.protection import (PROTECTED, Protection, UNPROTECTED, UNREADABLE, fetch,
                                 parse_response, repo_meta, selftest)
from deadgate.resolve import (AMBIGUOUS as R_AMBIGUOUS, NOT_REQUIRED, REQUIRED, attribute,
                              resolve, workflow_is_callable)

RULES = "rules/branches"
CLASSIC = "/protection"


def api(rules, classic):
    """A fake transport. (status, payload) per endpoint."""
    def call(path):
        return rules if RULES in path else classic
    return call


def api3(rules, classic, branch):
    def call(path):
        if RULES in path:
            return rules
        if path.endswith("/protection"):
            return classic
        return branch
    return call


def req(*contexts):
    return (200, [{"type": "required_status_checks",
                   "parameters": {"required_status_checks":
                                  [{"context": c, "integration_id": 15368} for c in contexts]}}])


# ---------------------------------------------------------------- transport, not measurement

def test_rate_limit_403_is_not_unprotected():
    """The first live call this module ever made came back 403 rate-limit."""
    p = fetch("o/r", "main", api((403, {"message": "API rate limit exceeded"}),
                                 (403, {"message": "API rate limit exceeded"})))
    assert p.state == UNREADABLE and not p.complete


def test_transport_failure_is_not_unprotected():
    p = fetch("o/r", "main", api((0, None), (0, None)))
    assert p.state == UNREADABLE and not p.complete


def test_404_without_admin_is_not_authoritative():
    """ruff answers 404 on the classic endpoint with permissions.admin false."""
    p = fetch("o/r", "main", api((200, []), (404, {"message": "Not Found"})), admin=False)
    assert p.state == UNREADABLE and not p.complete


def test_404_with_admin_is_authoritative():
    """Only once the branch itself is confirmed to exist; see
    test_a_branch_that_does_not_exist_does_not_clear_every_finding."""
    p = fetch("o/r", "main", api3((200, []), (404, {"message": "Branch not protected"}),
                                  (200, {"name": "main"})), admin=True)
    assert p.state == UNPROTECTED and p.complete


def test_rulesets_readable_but_classic_hidden_is_incomplete():
    """A match still proves required; an absence proves nothing."""
    p = fetch("o/r", "main", api(req("test"), (403, {})), admin=False)
    assert p.state == PROTECTED and p.required == frozenset({"test"}) and not p.complete


def test_repo_meta_never_infers_admin():
    assert repo_meta("o/r", lambda p: (200, {"default_branch": "main", "permissions": {}})) \
        == ("main", False)
    assert repo_meta("o/r", lambda p: (200, {"default_branch": "main"})) == ("main", False)
    assert repo_meta("o/r", lambda p: (403, None)) == (None, False)


def test_selftest_catches_a_200_that_does_not_parse():
    """The real bug: gh api -i uses bare newlines, so every body failed to parse."""
    ok, why = selftest(lambda p: (200, None))
    assert not ok and "did not parse" in why
    assert selftest(lambda p: (200, {"default_branch": "main"}))[0]


def test_both_rule_sources_merge():
    classic = (200, {"required_status_checks": {"contexts": ["legacy"],
                                                "checks": [{"context": "modern"}]}})
    p = fetch("o/r", "main", api(req("ruleset"), classic), admin=True)
    assert p.required == frozenset({"legacy", "modern", "ruleset"}) and p.complete


# ---------------------------------------------------------------- name derivation

@pytest.mark.parametrize("key,job,context,hit", [
    ("test", {}, "test", True),
    ("test", {}, "test-extra", False),
    ("test", {"strategy": {"matrix": {"os": ["a", "b"]}}}, "test (a)", True),
    ("test", {"strategy": {"matrix": {"os": ["a"]}}}, "testing (a)", False),
    # A computed matrix still names its legs `base (...)`, so the prefix stays decidable.
    ("test", {"strategy": {"matrix": "${{ fromJson(needs.x.outputs.y) }}"}}, "test (99)", True),
    ("t", {"name": "Unit tests"}, "Unit tests", True),
    # A static name IS suffixed with the leg values. Verified on the authoritative surface:
    # promptfoo's main branch requires the context `Check Python (3.9)` while its workflow
    # declares a static `name: Check Python` over a python-version matrix. The opposite rule
    # was asserted here first, and it made every leg of such a job match nothing.
    ("t", {"name": "Unit", "strategy": {"matrix": {"os": ["a"]}}}, "Unit (a)", True),
    ("t", {"name": "Unit", "strategy": {"matrix": {"os": ["a"]}}}, "Unit", True),
    ("t", {"name": "Unit"}, "Unit (a)", False),
    # A name that already references the matrix is NOT suffixed: promptfoo's
    # `Build on Node ${{ matrix.node }}` is required as the bare `Build on Node 24.x`.
    ("t", {"name": "Build on Node ${{ matrix.node }}", "strategy": {"matrix": {"node": ["24.x"]}}},
     "Build on Node 24.x", True),
    ("t", {"name": "Unit ${{ matrix.os }}", "strategy": {"matrix": {"os": ["a"]}}}, "Unit a", True),
    ("call", {"uses": "./.github/workflows/r.yml"}, "call / build", True),
    # A SKIPPED reusable call emits the bare caller name with no callee suffix, so asserting
    # the caller never produces a check of its own was wrong, and wrong in the clearing
    # direction. Skipped jobs are this tool's target population.
    ("call", {"uses": "./.github/workflows/r.yml"}, "call", True),
    # A matrixed reusable call is `caller (values) / callee`. Checking uses: before the
    # matrix emitted only `caller / `, so every matrixed reusable call matched nothing.
    ("call", {"uses": "r.yml", "strategy": {"matrix": {"os": ["a"]}}}, "call (a) / build", True),
    ("call", {"uses": "r.yml"}, "caller-other / build", False),
])
def test_derivation_matches_real_check_names(key, job, context, hit):
    d = derive(key, job)
    assert d.confidence == EXACT
    assert d.matches(context) is hit


@pytest.mark.parametrize("job", [
    {"name": "build ${{ github.event.inputs.target }}"},
    {"name": "x ${{ matrix.os }}", "strategy": {"matrix": "${{ fromJson(x) }}"}},
    {"name": "x ${{ matrix.absent }}", "strategy": {"matrix": {"os": ["a"]}}},
    # include/exclude add and remove legs by rules not worth guessing at. An under-enumerated
    # name set fails to match a context that IS required, which clears the finding.
    {"name": "x ${{ matrix.os }}", "strategy": {"matrix": {"os": ["a"], "include": [{"os": "z"}]}}},
    {"name": "x ${{ matrix.os }}", "strategy": {"matrix": {"os": ["a"], "exclude": [{"os": "a"}]}}},
])
def test_unresolvable_names_are_ambiguous_not_absent(job):
    assert derive("j", job).confidence == AMBIGUOUS


def test_matching_on_an_ambiguous_derivation_is_refused():
    """A silent False here would clear the finding; the caller must handle it explicitly."""
    with pytest.raises(ValueError):
        derive("j", {"name": "x ${{ github.ref }}"}).matches("anything")


# ---------------------------------------------------------------- resolution

def test_required_escalates_medium():
    p = Protection(PROTECTED, frozenset({"test"}), True, ("classic",))
    assert resolve("MEDIUM", "test", {}, p).severity == "HIGH"


def test_unreadable_leaves_severity_alone():
    p = Protection(UNREADABLE, frozenset(), False, (), "classic HTTP 403")
    assert resolve("MEDIUM", "test", {}, p).severity == "MEDIUM"


def test_incomplete_set_cannot_clear_a_finding():
    p = Protection(PROTECTED, frozenset({"other"}), False, ("rulesets",))
    r = resolve("MEDIUM", "test", {}, p)
    assert r.verdict == R_AMBIGUOUS and r.severity == "MEDIUM"


def test_complete_set_clears_a_finding():
    p = Protection(PROTECTED, frozenset({"other"}), True, ("classic", "rulesets"))
    r = resolve("MEDIUM", "test", {}, p)
    assert r.verdict == NOT_REQUIRED and r.severity == "LOW"


def test_ambiguous_name_cannot_clear_a_finding():
    p = Protection(PROTECTED, frozenset({"other"}), True, ("classic",))
    r = resolve("MEDIUM", "j", {"name": "x ${{ github.ref }}"}, p)
    assert r.verdict == R_AMBIGUOUS and r.severity == "MEDIUM"


@pytest.mark.parametrize("structural", ["HIGH", "LOW"])
def test_only_medium_moves(structural):
    assert resolve(structural, "test", {},
                   Protection(PROTECTED, frozenset({"test"}), True, ("classic",))
                   ).severity == structural
    assert resolve(structural, "test", {},
                   Protection(UNPROTECTED, frozenset(), True, ("classic:absent",))
                   ).severity == structural


# ---------------------------------------------------------------- the instrument check

def test_broken_derivation_is_visible_as_unattributed():
    p = Protection(PROTECTED, frozenset({"Unit tests (3.11)"}), True, ("classic",))
    att = attribute(p, {"ci.yml": {"test": {}}})
    assert att.unattributed == ("Unit tests (3.11)",) and att.suspicious


def test_undecidable_jobs_are_counted_not_dropped():
    p = Protection(PROTECTED, frozenset({"test"}), True, ("classic",))
    att = attribute(p, {"ci.yml": {"test": {}, "weird": {"name": "x ${{ github.ref }}"}}})
    assert att.undecidable_jobs == ("ci.yml::weird",) and not att.suspicious


# ---------------------------------------------------------------- the transport's own parsing

GH_REAL = ("HTTP/2.0 200 OK\n"
           "Access-Control-Allow-Origin: *\n"
           "Content-Type: application/json; charset=utf-8\n"
           "\n"
           '{"default_branch":"main","permissions":{"admin":true}}\n')


def test_parses_the_bare_newline_header_block_gh_actually_emits():
    """Captured from `gh api -i`. The first implementation split on CRLF and matched nothing."""
    status, data = parse_response(GH_REAL)
    assert status == 200 and data["default_branch"] == "main"


def test_parses_crlf_too():
    status, data = parse_response(GH_REAL.replace("\n", "\r\n"))
    assert status == 200 and data["default_branch"] == "main"


@pytest.mark.parametrize("stdout,why", [
    ("", "empty output"),
    ("HTTP/2.0 200 OK\nContent-Type: application/json\n", "headers but no body separator"),
    ("HTTP/2.0 200 OK\n\nnot json at all", "200 with an unparseable body"),
    ("HTTP/2.0 200 OK\n\n", "200 with an empty body"),
    ("gh: could not connect\n\nwhatever", "no status line"),
])
def test_an_unreadable_response_reports_status_zero(stdout, why):
    """Status 0 is what every caller treats as unreadable. Returning the real status here is
    how a parse failure became `(200, None)` and then `META_FAIL 278/278, exit 0`."""
    assert parse_response(stdout)[0] == 0, why


def test_a_real_404_body_still_parses():
    status, data = parse_response(
        'HTTP/2.0 404 Not Found\nContent-Type: application/json\n\n{"message":"Not Found"}')
    assert status == 404 and data["message"] == "Not Found"


def test_throttling_is_distinguished_from_forbidden():
    """Both are 403. One is retryable; the other is a permission boundary. A survey that
    conflates them reports an outage as a fact about the repository."""
    rl = (403, {"message": "API rate limit exceeded for user ID 1"})
    p = fetch("o/r", "main", api(rl, rl))
    assert p.state == UNREADABLE and p.throttled and p.retryable

    nope = (403, {"message": "Must have admin rights to Repository."})
    q = fetch("o/r", "main", api(nope, nope))
    assert q.state == UNREADABLE and not q.throttled and not q.retryable
    assert "FORBIDDEN" in q.detail


def test_a_protected_result_is_never_marked_retryable():
    p = fetch("o/r", "main", api(req("test"), (403, {"message": "API rate limit exceeded"})))
    assert p.state == PROTECTED and p.throttled and not p.retryable


@pytest.mark.parametrize("values,expected", [
    ([True, False], {"t true", "t false"}),          # GitHub renders YAML booleans lowercase
    ([3, 3.5], {"t 3", "t 3.5"}),
    ([None], {"t"}),                                  # null renders empty, and the name is trimmed
])
def test_matrix_values_render_the_way_github_renders_them(values, expected):
    d = derive("t", {"name": "t ${{ matrix.v }}", "strategy": {"matrix": {"v": values}}})
    assert expected <= d.names, f"{sorted(d.names)}"


def test_a_skipped_job_keeps_the_raw_template():
    """Observed live: a job skipped by its job-level if: emits ONE check run whose name is the
    unexpanded template, matrix expressions left literal. Those jobs are the target population."""
    d = derive("t", {"name": "Linux AppImage ${{ matrix.build_type }}",
                     "strategy": {"matrix": {"build_type": ["console", "tiles"]}}})
    assert d.matches("Linux AppImage ${{ matrix.build_type }}")
    assert d.matches("Linux AppImage console")


@pytest.mark.parametrize("body", [
    {"message": "You have exceeded a secondary rate limit"},
    {},                     # a 429 with no usable message is still a 429
    None,
])
def test_429_is_throttling_on_status_alone(body):
    p = fetch("o/r", "main", api((429, body), (429, body)))
    assert p.state == UNREADABLE and p.throttled and p.retryable


def test_429_is_throttling_not_a_permission_boundary():
    """GitHub signals secondary rate limits with 429. Treating it as FORBIDDEN marks a
    retryable outage permanently unreadable."""
    p = fetch("o/r", "main", api((429, {"message": "You have exceeded a secondary rate limit"}),
                                 (429, {"message": "slow down"})))
    assert p.state == UNREADABLE and p.throttled and p.retryable


def test_a_branch_that_does_not_exist_does_not_clear_every_finding():
    """404 from the classic endpoint looks identical for an unprotected branch and a branch
    that is not there. A typo in --branch must not read as 'nothing is required here'."""
    p = fetch("o/r", "typo", api3((200, []), (404, {"message": "Branch not protected"}),
                                  (404, {"message": "Branch not found"})), admin=True)
    assert p.state == UNREADABLE and not p.complete
    assert "did not confirm" in p.detail


def test_a_real_unprotected_branch_still_clears():
    p = fetch("o/r", "main", api3((200, []), (404, {"message": "Branch not protected"}),
                                  (200, {"name": "main"})), admin=True)
    assert p.state == UNPROTECTED and p.complete


def test_rules_request_asks_for_more_than_one_page():
    """The default page is 30 and `gh api` does not paginate, so a truncated first page would
    read as the complete required set."""
    seen = []

    def spy(path):
        seen.append(path)
        return (200, []) if RULES in path else (403, {})

    fetch("o/r", "main", spy)
    assert any("per_page=100" in p for p in seen if RULES in p)


# ---------------------------------------------------------------- the callee-naming blind spot

def test_a_uses_callable_workflow_cannot_name_its_own_checks():
    """When another workflow `uses:` this one, GitHub names the check `<caller> / <job>`, and
    the caller is not knowable from this file. Naming it unprefixed cleared findings on jobs
    that WERE required gates, and the attribution check could not see it: the caller's own
    prefix credited those contexts, so `suspicious` stayed False while the finding was hidden."""
    d = derive("build", {}, workflow_callable=True)
    assert d.confidence == AMBIGUOUS
    p = Protection(PROTECTED, frozenset({"call-ci / build"}), True, ("classic", "rulesets"))
    assert resolve("MEDIUM", "build", {}, p, workflow_callable=True).severity == "MEDIUM"
    # and without the flag it would have been cleared, which is the defect
    assert resolve("MEDIUM", "build", {}, p).severity == "LOW"


@pytest.mark.parametrize("on,expected", [
    ("workflow_call", True),
    (["workflow_call", "push"], True),
    ({"workflow_call": None, "pull_request": None}, True),
    ({"pull_request": None}, False),
    (None, False),
])
def test_workflow_is_callable(on, expected):
    assert workflow_is_callable({"on": on} if on is not None else {}) is expected


# ---------------------------------------------------------------- completeness gating

def test_completeness_requires_a_confirmed_branch_even_when_checks_are_required():
    """Rulesets can return pattern-matched org rules for a branch that does not exist, so a
    non-empty required set does not prove the branch is real. Confirming existence only when
    the set was empty left NOT_REQUIRED reachable on a typo."""
    p = fetch("o/r", "typo", api3(req("test"), (200, {"required_status_checks": {"contexts": []}}),
                                  (404, {"message": "Branch not found"})), admin=True)
    assert p.state == PROTECTED and not p.complete
    assert resolve("MEDIUM", "other", {}, p).severity == "MEDIUM"


@pytest.mark.parametrize("body", [None, [], 0, ""])
def test_a_classic_200_that_is_not_a_protection_object_is_not_a_read(body):
    """`_classic_required` returns an empty set for any of these, so counting the response as
    a successful read turned an unparseable 200 into 'nothing is required here'."""
    p = fetch("o/r", "main", api3((200, []), (200, body), (200, {"name": "main"})), admin=True)
    assert p.state == UNREADABLE and not p.complete


def test_a_non_list_rulesets_payload_is_not_an_empty_rule_set():
    p = fetch("o/r", "main", api3((200, {"message": "nope"}), (403, {}), (200, {"name": "main"})))
    assert p.state == UNREADABLE and not p.complete


def test_a_computed_matrix_value_cannot_be_enumerated():
    """Without the guard, derive() would claim EXACT over a literal '${{ ... }}' value."""
    d = derive("t", {"name": "x ${{ matrix.v }}", "strategy": {"matrix": {"v": ["${{ env.A }}"]}}})
    assert d.confidence == AMBIGUOUS
