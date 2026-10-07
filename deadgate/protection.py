"""Read which status checks a branch actually requires.

Two endpoints answer this, and they differ in a way that decides what this tier may claim:

  GET /repos/{o}/{r}/rules/branches/{b}        rulesets. Readable with READ access.
  GET /repos/{o}/{r}/branches/{b}/protection   classic protection. Requires ADMIN.

On a repository you do not own, only the first answers. It reports what RULESETS require and
says nothing about classic protection, so an empty result does not mean the branch is
unprotected. That asymmetry is the whole design:

  a match      PROVES the check is required          -> safe to escalate
  no match     proves nothing unless the set is COMPLETE, which needs admin on both endpoints

So `complete` is tracked separately from `state`. Without it the tier may only escalate.

Completeness cannot be read off the status code. The classic endpoint answers 404 for an
unprotected branch you administer AND 404 for a repository you merely have read access to;
the two differ only in the message text ("Branch not protected" against "Not Found"), which
is prose and not an API contract. So `permissions.admin` from the repository metadata is what
gates it. Matching on the message was tried first and is exactly the kind of check that keeps
passing after the wording changes.

The failure that motivated the UNREADABLE state: the first live call written against these
endpoints came back `403 API rate limit exceeded`, which is shaped exactly like `403 must
have admin rights` and, parsed carelessly, exactly like "nothing is required here". A
transport failure read as a measurement is how a gate reports green on data it never saw.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PROTECTED = "PROTECTED"
UNPROTECTED = "UNPROTECTED"
UNREADABLE = "UNREADABLE"

# Both arrive as HTTP 403. They are not the same fact and they do not have the same remedy:
# one is retryable, the other says this tier cannot help on this repository. A survey that
# collapses them counts an outage as a permission boundary.
THROTTLED = "THROTTLED"
FORBIDDEN = "FORBIDDEN"


def classify(status: int, data) -> str:
    """Why a non-200 happened, in the terms that decide whether retrying can help.

    GitHub signals its PRIMARY rate limit with 403 and a message, and its SECONDARY limits
    with 429. Inspecting the body only on a 403 left 429 classified as a permission boundary,
    which marks a retryable outage as permanently unreadable.
    """
    message = str((data or {}).get("message", "")) if isinstance(data, dict) else ""
    if status == 429 or "rate limit" in message.lower() or "secondary rate" in message.lower():
        return THROTTLED
    if status == 403:
        return FORBIDDEN
    return f"HTTP {status}"


@dataclass(frozen=True)
class Protection:
    state: str
    required: frozenset[str] = frozenset()
    complete: bool = False
    sources: tuple[str, ...] = ()
    detail: str = ""
    throttled: bool = False

    @property
    def retryable(self) -> bool:
        """True when the only reason this is unreadable is that we were rate limited."""
        return self.state == UNREADABLE and self.throttled


def _rules_required(data) -> frozenset[str] | None:
    """Required contexts from the rulesets endpoint, or None if the payload is not a rule list."""
    if not isinstance(data, list):
        return None
    out = set()
    for rule in data:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                out.add(str(check["context"]))
    return frozenset(out)


def _classic_required(data) -> frozenset[str]:
    rsc = (data or {}).get("required_status_checks") or {}
    out = {str(c) for c in (rsc.get("contexts") or [])}
    for check in rsc.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            out.add(str(check["context"]))
    return frozenset(out)


def repo_meta(repo: str, api) -> tuple[str | None, bool]:
    """(default_branch, we_are_admin). Admin is False unless the API says True outright."""
    status, data = api(f"repos/{repo}")
    if status != 200 or not isinstance(data, dict):
        return None, False
    perms = data.get("permissions") or {}
    return data.get("default_branch"), perms.get("admin") is True


def fetch(repo: str, branch: str, api, admin: bool = False) -> Protection:
    """`api(path)` must return (http_status, parsed_json_or_None). No writes are ever issued.

    `admin` must come from repo_meta(). It decides whether a 404 from the classic endpoint
    is the authoritative "not protected" or merely "you cannot see this".
    """
    required: set[str] = set()
    sources: list[str] = []
    notes: list[str] = []
    throttled = False

    # per_page because the default page is 30 and `gh api` does not paginate on its own. A
    # truncated first page would read as the complete required set.
    status, data = api(f"repos/{repo}/rules/branches/{branch}?per_page=100")
    rules_ok = False
    if status == 200:
        parsed = _rules_required(data)
        if parsed is None:
            notes.append("rulesets payload was not a rule list")
        else:
            rules_ok = True
            sources.append("rulesets")
            required |= parsed
    else:
        reason = classify(status, data)
        throttled = throttled or reason == THROTTLED
        notes.append(f"rulesets {reason}")

    status, data = api(f"repos/{repo}/branches/{branch}/protection")
    classic_ok = False
    if status == 200 and isinstance(data, dict):
        classic_ok = True
        sources.append("classic")
        required |= _classic_required(data)
    elif status == 200:
        # A 200 whose body is null, a list, or a number is not a protection object. Counting
        # it as a successful read made an unparseable response look like "nothing required".
        notes.append("classic HTTP 200 with a body that is not a protection object")
    elif status == 404 and admin:
        # With admin, 404 is the documented answer for an unprotected branch.
        classic_ok = True
        sources.append("classic:absent")
    elif status == 404:
        # Same code, no admin: this is "you cannot see it", not "it is not there".
        notes.append("classic HTTP 404 without admin, so not authoritative")
    else:
        reason = classify(status, data)
        throttled = throttled or reason == THROTTLED
        notes.append(f"classic {reason}")

    complete = rules_ok and classic_ok
    if complete:
        # `complete` is the only licence this tier has to say NOT_REQUIRED, so confirm the
        # branch exists before granting it. A branch that is not there answers 404 on the
        # classic endpoint exactly like an unprotected one, and rulesets can still return
        # pattern-matched org rules for it, so even a non-empty required set does not prove
        # the branch is real. Checking this only when the set was empty left that open.
        status, _ = api(f"repos/{repo}/branches/{branch}")
        if status != 200:
            complete = False
            notes.append(f"branch {branch!r} did not confirm ({classify(status, None)}), so "
                         "absence of a match proves nothing")
    if required:
        state = PROTECTED
    elif complete:
        state = UNPROTECTED
    else:
        state = UNREADABLE
    return Protection(state, frozenset(required), complete, tuple(sources), "; ".join(notes),
                      throttled)


def parse_response(stdout: str):
    """Parse `gh api -i` output into (status, json). Status 0 means the read did not happen.

    Separate from gh_api so the thing that actually broke is unit-testable against real
    captured bytes. `gh api -i` ends its header block with a BARE newline, not CRLF.
    """
    import json
    import re

    match = re.search(r"\r?\n\r?\n", stdout)
    if not match:
        return 0, None
    head, body = stdout[:match.start()], stdout[match.end():]
    first = head.splitlines()[0] if head else ""
    parts = first.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if status == 0:
        return 0, None
    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, ValueError):
        # A 200 we cannot read is not a successful read.
        return 0, None


def gh_api(timeout: int = 20):
    """An `api` callable backed by `gh`. Read-only: GET only, no method flag is ever passed.

    Status 0 means "this read did not happen" and every caller treats it as unreadable. A 200
    whose body will not parse returns 0 for that reason: `gh api -i` separates headers with a
    bare newline, not CRLF, and the first version of this function partitioned on CRLF. It
    therefore handed back (200, None) for every repository on earth, the survey built on it
    reported META_FAIL 278/278, and it exited 0. A parse failure that presents as an empty
    result is the defect this tool is named after.
    """
    import subprocess

    def call(path: str):
        try:
            proc = subprocess.run(["gh", "api", "-i", path], capture_output=True, text=True,
                                  timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return 0, None
        return parse_response(proc.stdout)

    return call


def selftest(api=None) -> tuple[bool, str]:
    """Prove the transport parses a known-good response before trusting any survey built on it.

    Without this, a transport bug is indistinguishable from a repository that requires nothing.
    """
    api = api or gh_api()
    status, data = api("repos/octocat/Hello-World")
    if status != 200:
        return False, f"expected HTTP 200 for a public repository, got {status}"
    if not isinstance(data, dict) or "default_branch" not in data:
        return False, f"HTTP 200 but the body did not parse into a repository object: {type(data)}"
    return True, f"ok, default_branch={data['default_branch']!r}"
