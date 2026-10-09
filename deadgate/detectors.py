"""Structural detectors for CI checks that cannot fail.

Every detector reports a FINDING only when the defect is STRUCTURAL and decidable from
the workflow file alone. Anything needing branch-protection state or runtime history is
out of scope here and belongs to the API tier.

Design rule taken from gate_mutation_sweep.py: a detector must fire on the planted defect
AND stay quiet on a near miss. A detector that fires on everything gets the whole tool
switched off, which is worse than not shipping it.
"""
from __future__ import annotations

import pathlib
import re
import os
from dataclasses import dataclass

# The DEADGATE_NAIVE experiment flag was removed in 0.1.2. It ran the detectors without the
# suppressions that tracing real repositories forced on them, so the cost of each could be
# measured on one corpus. It stopped doing that: 0.1.2's suppressions (the wildcard and
# whole-context reads, and searching the whole job) live in the shared path rather than behind
# the flag, so both arms reported an identical D1 and the pre-narrowing column silently became
# a copy of the current one. A flag whose name promises an isolation it no longer provides is
# worse than no flag. Version against version on one cache is the comparison that answers
# whether a fix helped, and that is what MEASUREMENT.md now carries.

FILTERS = {"grep", "jq", "head", "tail", "tee", "awk", "sed", "cut", "sort", "uniq", "wc", "tr"}


@dataclass(frozen=True)
class Finding:
    detector: str
    job: str
    title: str
    detail: str
    repro: str
    severity: str = "HIGH"


def _truthy_always(cond) -> bool:
    """Does this if: condition reduce to always()?"""
    if cond is None:
        return False
    s = str(cond).strip()
    s = re.sub(r"^\$\{\{\s*|\s*\}\}$", "", s).strip()
    return s == "always()"


# Conditions that are NOT skip-prone. A job guarded by one of these runs in normal
# operation, so treating it as "might skip" is a false alarm. Found by running against
# promptfoo main.yml, where package-acceptance carries `if: ${{ !cancelled() }}` and
# the corpus had nothing like it.
_NEVER_SKIPS = re.compile(r"^(always\(\)|!\s*cancelled\(\)|success\(\)\s*\|\|\s*failure\(\)|"
                          r"failure\(\)\s*\|\|\s*success\(\))$")


# A job that can NEVER run. All three spellings appear in the corpus:
#   rspack      check-cache   if: ${{ false }}   (a cache check kept but disabled)
#   bootc       test-coreos   if: false          (disabled with a comment explaining why)
#   hyperswitch runner_alpha  if: false          (optional connector tests, off)
# A job that never runs cannot report success on anything, so every finding about it is
# vacuous. Four of the 44 surviving D1 HIGH findings were this. YAML turns a bare `false`
# into a bool and leaves `${{ false }}` a string, so both forms have to be recognised.
_LITERAL_FALSE = re.compile(r"^\s*(?:\$\{\{)?\s*(?:false|0)\s*(?:\}\})?\s*$", re.I)


def _never_runs(cond) -> bool:
    """True when a job's `if:` is a literal false, in any of its spellings."""
    if cond is False:
        return True
    return isinstance(cond, str) and bool(_LITERAL_FALSE.match(cond))


def _skip_prone(cond) -> bool:
    """A job-level if: that can realistically evaluate false and skip the job."""
    if cond is None:
        return False
    s = re.sub(r"^\$\{\{\s*|\s*\}\}$", "", str(cond).strip()).strip()
    return not _NEVER_SKIPS.match(s)


_NEEDS_READ = re.compile(r"needs\.(?:\*|[A-Za-z0-9_\-]+)\.(?:result|outputs)")

# The THIRD spelling, and the one that reads every upstream at once without containing the
# word "result" anywhere: serialise the whole context and inspect it outside the expression.
#
#     env:  NEEDS_JSON: ${{ toJSON(needs) }}
#     run:  echo "$NEEDS_JSON" | jq -r 'to_entries[] | select(.value.result != "success" ...)'
#
# Found on astral-sh/ruff, whose `required-checks-passed` gate is written exactly this way:
# 0.1.1 reported 18 false HIGH against it, 9 D1 and 9 D4, one per job in the gate's needs list.
# Fixing the wildcard form in 0.1.1 and stopping there was the error; the lesson is that a
# gate can consult its upstreams without naming any of them.
_NEEDS_WHOLE_CONTEXT = re.compile(r"to_?json\s*\(\s*needs\s*\)", re.I)


def _job_blob(job) -> str:
    """The WHOLE job serialised, for the question "does this job consult its upstreams?".

    Placement, not spelling, was the fourth false-positive class. A job that calls a reusable
    workflow has `uses:` and no `steps:`, and passes state through JOB-level `with:`:

        update-tracker:
          uses: ./.github/workflows/update_tracking_issue.yml
          if: ${{ always() }}
          needs: [check-sdist]
          with: { job_status: "${{ needs.check-sdist.result }}" }   # <- read, and missed

    The predecessor of this, `_steps_text`, covered step-level `with` and env but not
    the job-level `with` that only exists for reusable calls, so scikit-learn's gate
    read as checking nothing. It was superseded here and is now deleted. Serialising the
    whole job ends the game of enumerating surfaces: after needs.*.result, toJSON(needs) and
    this, the lesson is that the expression can live anywhere the schema allows.
    """
    import json as _json
    try:
        return _json.dumps(job, default=str)
    except Exception:
        return str(job)


def _reads_any_upstream_state(text: str) -> bool:
    """True when the text consults any upstream's result or outputs, by name, by wildcard, or
    by serialising the whole `needs` context."""
    return bool(_NEEDS_READ.search(text) or _NEEDS_WHOLE_CONTEXT.search(text))


# A job that announces itself as the thing branch protection points at. Narrow on purpose:
# `check` alone is far too broad, and matched `check-changes`, which is change detection.
_GATE_NAME = re.compile(
    r"\b(required|all-?checks?-?pass(?:ed)?|status-?check|merge-?queue|ci-?required|gate)\b|"
    r"-required\b|\brequired-", re.I)


# ---------------------------------------------------------------------------
# Class 7: upstream status read through the GitHub Actions API, not `needs`.
#
# WordPress/gutenberg's *-status-check jobs run
#     node .github/workflows/scripts/ci-status-check.js --ignore '<names>'
# which pages GET /repos/{repo}/actions/runs/{run_id}/jobs and fails if any job
# concluded as anything other than success or skipped. The word `result` never
# appears and `needs` is never consulted, so no pattern over the workflow can
# find it -- the evidence is in a file this tool does not parse. Ten of the 54
# D1 findings in the 179-HIGH corpus were this, all false, all one repository.
#
# The first six classes were "the expression exists and my regex missed it" and
# were fixed by widening a pattern. This one is different in kind: D1's claim
# ("never reads needs.X.result") is UNVERIFIABLE from the workflow alone once a
# step shells out. So the fix narrows the claim instead of widening a pattern.
#
# Narrow on purpose, and the token is what makes it narrow: reading another
# job's conclusion requires credentials. A script invoked WITHOUT a token in
# scope cannot be checking run status, so it is not excused here. Without that
# condition this would demote every job that runs ./build.sh.
_SCRIPT_CALL = re.compile(
    r"(?:^|[|;&]|\b(?:node|python3?|ruby|bash|sh|deno|bun|tsx|pwsh)\s+)"
    r"(\.{0,2}[\w./-]*[\w-]+\.(?:js|mjs|cjs|ts|py|rb|sh|bash|ps1))\b")

# Reading the current run's jobs, or any check/status, from the API.
_API_STATUS_READ = re.compile(
    # `[^\s]` was WRONG here and the control caught it: the real gutenberg script
    # interpolates as `actions/runs/${ GITHUB_RUN_ID }/jobs`, with SPACES inside the
    # braces, so a whitespace-forbidding class could never match the artifact. It
    # matched the string I invented for the smoke test, which is the whole lesson.
    r'actions/runs/[^\n"\'`]{0,160}?/jobs'
    r'|\bgh\s+run\s+view\b'                  # gh run view --json jobs
    r'|listJobsForWorkflowRun|getWorkflowRun'  # octokit
    r'|\bcheck-runs\b'                         # the checks API
    r'|\bcommits/[^\n"\'`]{0,160}?/status\b',  # combined status
    re.I)


_ONE_STEP_DOWN = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}


def _demote(sev: str) -> str:
    """One tier down, from wherever the finding actually sits.

    The class-7 guard first wrote `"MEDIUM" if sev == "HIGH" else sev`, which became a
    silent no-op the moment D1 stopped producing a structural HIGH: the guard still ran,
    still appended its note, and changed nothing. Two tests caught it by asserting the
    demoted severity was strictly below the baseline rather than equal to a literal.
    """
    return _ONE_STEP_DOWN.get(sev, sev)


def _token_in_scope(job: dict, step: dict) -> bool:
    """Is a GitHub token available to this step?

    Either env (job or step) or a `permissions:` block granting it, since
    `secrets.GITHUB_TOKEN` is reachable by any step that names it.
    """
    for env in (job.get("env"), step.get("env")):
        if isinstance(env, dict) and any(
                "GITHUB_TOKEN" in str(k).upper() or "GITHUB_TOKEN" in str(v).upper()
                for k, v in env.items()):
            return True
    return "GITHUB_TOKEN" in _job_blob(step).upper()


def _delegated_status_scripts(job: dict) -> list[str]:
    """Script paths this job shells out to with a token in scope.

    These are the steps whose behaviour this tool cannot see. Returning the paths
    rather than a bool lets the caller go and read them when the tree is available.
    """
    out = []
    for step in (job.get("steps") or []):
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if not isinstance(run, str) or not _token_in_scope(job, step):
            continue
        for m in _SCRIPT_CALL.finditer(run):
            out.append(m.group(1))
    return out


def _script_reads_status(paths: list[str], base: "pathlib.Path | None") -> bool | None:
    """Does one of these scripts read upstream status from the API?

    True  -- read it and it does; the job is a working gate and D1 must stay quiet.
    False -- read them all and none does; the claim stands at full severity.
    None  -- could not read them, so the claim is unverifiable and gets demoted.

    Verifying beats guessing: when the tree is there this turns class 7 from a
    false positive into a correct suppression, with the evidence actually read.
    """
    if base is None or not paths:
        return None
    seen = False
    for rel in paths:
        # One candidate, not two. The first spelling here was
        # `base / rel.lstrip("./")`, which strips a CHARACTER SET and so turned
        # ".github/..." into "github/...". A second `base / rel` candidate masked
        # that, and the mutation reverting removeprefix to lstrip SURVIVED because
        # pathlib already normalises a leading "./" -- the extra candidate could
        # never contribute. Dead code found by its own surviving mutant, again.
        try:
            cand = base / rel
            if cand.is_file():
                seen = True
                if _API_STATUS_READ.search(cand.read_text(errors="replace")):
                    return True
        except OSError:
            continue
    return False if seen else None


def d1_skippable_upstream(jobs: dict, doc: dict | None = None,
                          base: pathlib.Path | None = None) -> list[Finding]:
    """A conditional job that something depends on, where the dependent never checks the result.

    GitHub documents that a skipped job reports Success and does not prevent a merge even
    as a required check, and that a skipped conclusion is treated as success for dependent
    checks. So skipping the real work silently satisfies the gate.
    """
    found = []
    conditional = {n for n, j in jobs.items()
                   if isinstance(j, dict) and _skip_prone(j.get("if"))}
    _chain_watched = _jobs_whose_whole_chain_a_gate_watches(jobs)
    # A dependent whose own if: reads the upstream's outputs is deliberately gated on it:
    # the change-detection pattern, which is intentional, and whose real failure mode is
    # D4's. That exclusion happens in _job_blob, which serialises the whole job. An
    # explicit second check here was DEAD CODE and is removed; the A/B that was supposed
    # to prove it worked reported a difference of exactly zero and found it instead.
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        if not needs:
            continue
        # A dependent that can never run cannot falsely report success.
        if _never_runs(job.get("if")):
            continue
        if name in _chain_watched:
            continue
        needs_list = needs if isinstance(needs, list) else [needs]
        skippable = [n for n in needs_list if n in conditional]
        if not skippable:
            continue
        text = _job_blob(job)
        if _reads_any_upstream_state(text):
            continue
        sev = _d1_severity(name, job, doc or {}, _workflow_has_any_gate(jobs))
        # Class 7: the job may consult the API instead of `needs`, in a script this
        # tool cannot parse. Only considered when the job announces itself as a gate,
        # so an ordinary job running ./build.sh is untouched.
        unverified = ""
        if _GATE_NAME.search(name):
            verdict = _script_reads_status(_delegated_status_scripts(job), base)
            if verdict is True:
                continue
            if verdict is None and _delegated_status_scripts(job):
                sev = _demote(sev)
                unverified = (" NOTE: this job shells out to a script with a token in"
                              " scope, so whether it checks upstream status could not be"
                              " verified from the workflow alone.")
        for up in skippable:
            found.append(Finding(
                detector="D1",
                severity=sev,
                job=name,
                title="gate satisfied by a skipped job",
                detail=(f"job '{name}' depends on '{up}', which is conditional. A skipped job "
                        f"reports Success, and '{name}' never reads needs.{up}.result."
                        + unverified),
                repro=(f"Open a PR where '{up}'s if: condition is false. '{up}' skips, "
                       f"'{name}' succeeds, and nothing ran."),
            ))
    return found


# Heads that cannot meaningfully fail, so masking their status masks nothing.
# echo and printf were the original two; the rest come from sampled false positives
# (`arch | sed`, `printf | sed`) and from commands that only fail on absurd input.
_CANNOT_FAIL = re.compile(
    r"^\s*(echo|printf|true|arch|pwd|hostname|whoami|id|uname|seq|yes|basename|dirname)\b")


# A pipeline whose status IS the test. `if cmd | grep -q x; then` is not a masked exit
# status, it is the idiom for asking a question, and the author wants exactly the filter's
# verdict. Eight of the nineteen D3 false positives across two hand-labelled samples were
# this shape, in `if`, `elif`, `while` and `until` conditions and behind a leading `!`.
_CONDITION_LINE = re.compile(r"^\s*(if|elif|while|until)\b|^\s*!\s")

# A value that is only printed. `echo "size: $(du -h x | cut -f1)"` masks du's status, and
# what that costs is a wrong number in a log. Without a redirect into $GITHUB_OUTPUT,
# $GITHUB_ENV or a file, nothing downstream can read it, so nothing downstream can be
# misled by it.
_ONLY_PRINTED = re.compile(r"^\s*echo\b(?!.*(>>|>|\$GITHUB_OUTPUT|\$GITHUB_ENV))")



def _substitution_bodies(line: str) -> list[str]:
    """Bodies of $( ... ), innermost first, so a pipe inside one is analysed on its own."""
    out, stack = [], []
    i = 0
    # `< len(line)`, not `< len(line) - 1`. The old bound never examined the FINAL
    # character, so a substitution closing at end of line was never closed and this
    # returned nothing. `X=$(cmd | filter)` almost always ends its line, which is the
    # shape this function exists for, so the narrowing it feeds could hardly ever fire.
    while i < len(line):
        if line[i] == "$" and i + 1 < len(line) and line[i + 1] == "(":
            stack.append(i + 2); i += 2; continue
        if line[i] == ")" and stack:
            out.append(line[stack.pop():i])
        i += 1
    return out


def _upstream_can_fail(line: str) -> bool:
    """Can the command BEFORE the final pipe actually fail?

    Two shapes are benign and together they were 10 of 20 sampled findings:
      echo "literal" | cut                      the head cannot fail
      echo "X=$(echo literal | cut)" >> file    the pipe lives INSIDE a substitution whose
                                                own head is echo, and splitting the whole
                                                line on its last pipe misses that.
    """
    for body in _substitution_bodies(line):
        if "|" in body:
            head = body.rsplit("|", 1)[0].strip()
            if _CANNOT_FAIL.match(head) and "$(" not in head and "`" not in head:
                return False
    head = line.rsplit("|", 1)[0].strip()
    if "$(" in head or "`" in head:
        # the outer head holds a substitution we already judged benign above
        return True
    return not _CANNOT_FAIL.match(head)


def _result_is_emptiness_checked(var: str, body: str) -> bool:
    """Did the author guard the empty case themselves, in the same step?

    `X=$(find ... | head -1)` followed by `if [ -z "$X" ]; then exit 1` is a handled
    case, not a defect. Reporting it anyway is how a tool gets uninstalled.
    """
    if not var:
        return False
    return bool(re.search(rf"-[zn]\s+\"?\$\{{?{re.escape(var)}\}}?", body))


# Can the masked status reach anything at all?
#
# `_d3_severity` decided HIGH from `exit 1` or GITHUB_OUTPUT appearing ANYWHERE in the
# step, and from the assigned variable being dereferenced ANYWHERE in it. Both are too
# coarse: one `exit 1` at the bottom of a step promoted every log-extraction pipeline
# above it, and "dereferenced" counted uses that fail loudly on an empty value.
#
# Measured on a complete hand-labelled census of all 125 D3 HIGH findings: 51.2% were
# false. These two predicates remove 38 of the 64 false ones and lose ZERO of the 61 true
# ones, taking HIGH to 29.9% false. Every rule below was evaluated against those labels
# before being written here, and two earlier versions were discarded because they cost
# true positives: one treated `echo "d=${d}" >> "$GITHUB_OUTPUT"` as "only printed", and
# one missed `export V=$(...)` and `for f in $(...)` as consumption.

# The output of this line is captured, redirected, or exported, so a wrong value escapes.
_LINE_CONSUMES = re.compile(
    r">>?\s*\S"                                            # any redirect
    r"|\$\{?GITHUB_(OUTPUT|ENV)\b"                          # crosses the step boundary
    r"|\btee\b"                                            # tee writes a file
    r"|\$\("                                                # captured by a substitution
    r"|^\s*(?:export|local|declare|readonly)?\s*[A-Za-z_][A-Za-z0-9_]*=")

_PRINT_ONLY = re.compile(r"^\s*(echo|printf)\b")
_BARE_TEST = re.compile(r"^\s*(test\s|\[\s|\[\[\s)")
_IS_CONDITION = re.compile(r"^\s*(if|elif|while|until)\b|\bif\s+\[|&&|\|\|")


def _every_use_is_safe(var: str, body: str, assign_line: str) -> bool:
    """True when every use of `var` either fails loudly on empty or cannot matter.

    Deliberately conservative: anything not provably safe keeps the finding. A bare
    command argument is NOT safe, because the corpus has it both ways. `helm push "$PKG"`
    fails loudly on an empty argument; `ninja -C "$DIR" $TARGETS` silently builds the
    default target and succeeds. The workflow cannot tell those apart, so both are kept.

    The three safe shapes, each confirmed by running bash rather than by reasoning:
      * never dereferenced at all
      * only printed, with no redirect (a redirect is export, not print)
      * a BARE `test x = y` or `[ x = y ]` statement, whose non-zero status is the step's.
        Note `if [ "$X" -gt 1 ]` is NOT this: `set -e` is suspended in an if-condition, so
        an empty X makes `[` error, the else branch is taken, and the check passes
        silently. That distinction was wrong in the first census pass and cost two labels.
    """
    rest = body.replace(assign_line, "", 1)
    uses = [ln.strip() for ln in rest.splitlines()
            if re.search(rf"\$\{{?{re.escape(var)}\b", ln)]
    if not uses:
        return True
    for use in uses:
        if re.search(rf"\$\{{{re.escape(var)}:[-=]", use):
            continue                                        # ${var:-default} applied
        redirects = re.search(r">>?\s*\S|\$\{?GITHUB_(OUTPUT|ENV)\b", use)
        if _PRINT_ONLY.match(use) and not redirects:
            continue
        if "GITHUB_STEP_SUMMARY" in use and not re.search(r"\$\{?GITHUB_(OUTPUT|ENV)\b", use):
            continue                                        # a markdown summary is cosmetic
        if _BARE_TEST.match(use) and not _IS_CONDITION.match(use):
            continue
        return False
    return True


def _masked_status_can_matter(line: str, body: str) -> bool:
    """Could this masked exit status change any outcome?"""
    if not _LINE_CONSUMES.search(line):
        return False
    # The assignment must CAPTURE the pipeline, so the RHS has to open a command
    # substitution. `WINEDEBUG=-all timeout 300 make check | tee x.log` is an inline
    # ENV PREFIX, not a capture: reading it as one made the variable look unused and
    # suppressed two real findings where a test command's failure is masked by tee.
    # The simulation missed this because it only applied the assignment check to lines
    # already classified as captures; the real detector applies it to every line.
    assigned = re.match(
        r"\s*(?:export|local|declare|readonly)?\s*([A-Za-z_][A-Za-z0-9_]*)=[\"']?\$\(", line)
    if assigned and _every_use_is_safe(assigned.group(1), body, line):
        return False
    return True


def _d3_severity(step: dict, job_name: str, job: dict, doc: dict, line: str) -> str:
    """Does the masked exit status actually decide anything?

    A pipeline whose result is exported, or which sits in a step that can fail the build,
    masks a decision. A pipeline in a cleanup or logging step masks nothing anybody reads.
    Tracing twenty D3 findings by hand, the ones that mattered all had a consumer and the
    ones that did not were fire-and-forget.
    """
    body = str(step.get("run") or "")
    if not _on_pull_request(doc):
        return "LOW"
    exported = "GITHUB_OUTPUT" in body or "GITHUB_ENV" in body
    decides = bool(re.search(r"\bexit\s+[1-9]|::error::", body))
    assigned = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
    consumed = bool(assigned and re.search(rf"\$\{{?{re.escape(assigned.group(1))}\b",
                                           body.replace(line, "", 1)))
    if (decides or exported or consumed) and _masked_status_can_matter(line, body):
        return "HIGH"
    return "MEDIUM"


def _logical_lines(body: str):
    """Shell lines, joined where a pipeline continues onto the next one.

    D3 read one PHYSICAL line at a time, and a pipeline is not always one line. Three
    shapes in a hand-labelled sample broke it, all the same root cause:

        x=$(printf '%s' "$B" | tr -d '\r' \\      backslash continuation
              | sed -n 's/^A://p')

        ad="$(git log ... |                        an unclosed $( carries on
              cut -f2 | sort | uniq -d)"

    Split per line, the detector sees `cut -f2 | sort | uniq -d)"` and believes the head is
    `cut`, when the real head is `git log`. It then both misjudges whether that head can
    fail and misses that the line is a continuation of an `if`. Joining first is the only
    way the rest of the analysis is looking at a command.
    """
    buf = ""
    for raw in body.splitlines():
        buf = (buf + " " + raw.strip()) if buf else raw
        t = buf.rstrip()
        if t.endswith("\\"):
            buf = t[:-1]
            continue
        if t.endswith("|") or buf.count("$(") > buf.count(")"):
            continue
        yield buf
        buf = ""
    if buf:
        yield buf


def d3_pipe_masked_exit(jobs: dict, doc: dict | None = None) -> list[Finding]:
    """A run: step whose exit status is the LAST command in a pipe, with no pipefail.

    The step's status reports the filter's success, not the real command's. An outage
    reads as a pass.
    """
    found = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not run or "|" not in str(run):
                continue
            body = str(run)
            if re.search(r"set\s+[-a-z]*o?\s*[-a-z]*pipefail|set\s+-o\s+pipefail", body):
                continue
            for line in _logical_lines(body):
                line = line.strip()
                if "|" not in line or line.startswith("#") or "||" in line:
                    continue
                if _CONDITION_LINE.search(line) or _ONLY_PRINTED.search(line):
                    continue
                tail = line.rsplit("|", 1)[1].strip().split()
                if not tail:
                    continue
                if tail[0] in FILTERS and _upstream_can_fail(line):
                    assigned = (re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line) or [None, ""])[1]
                    if _result_is_emptiness_checked(assigned, body):
                        continue
                    label = step.get("name") or line[:40]
                    found.append(Finding(
                        detector="D3",
                        severity=_d3_severity(step, name, job, doc or {}, line),
                        job=name,
                        title="exit status masked by a pipe",
                        detail=(f"step '{label}' in job '{name}' ends a pipeline with "
                                f"'{tail[0]}' and does not set pipefail. The step's status is "
                                f"{tail[0]}'s, so a failure upstream of the pipe passes."),
                        repro=(f"Make the command before '| {tail[0]}' fail. The step still "
                               f"succeeds. Add 'set -o pipefail' and it fails correctly."),
                    ))
                    break
    return found


# Severity for D4 is decided by TWO questions the workflow file can answer:
#   does this workflow run on pull_request, so the job is a candidate required check, and
#   is the gated job a VERIFICATION job, so skipping it means nothing was checked.
# Skipping a deploy because no release was cut is intended. Skipping the tests because the
# path filter crashed is not. Without the branch-protection API this is the honest ceiling,
# and the tier says which question it could not answer.
_CHECK_JOB = re.compile(
    r"\b(test|tests|lint|check|checks|verify|typecheck|type-check|coverage|audit|"
    r"security|e2e|unit|integration|spec|validate|ci)\b", re.I)
_SHIP_JOB = re.compile(
    r"\b(deploy|publish|release|upload|notify|docs|announce|changelog|tag|sign|"
    r"docker|image|artifact)\b", re.I)


def _on_pull_request(doc: dict) -> bool:
    on = (doc or {}).get("on") or (doc or {}).get(True)
    if isinstance(on, str):
        return on == "pull_request"
    if isinstance(on, list):
        return "pull_request" in on
    if isinstance(on, dict):
        return "pull_request" in on or "pull_request_target" in on
    return False


def _gate_severity(job_name: str, job: dict, doc: dict) -> str:
    name = f"{job_name} {job.get('name') or ''}"
    if _SHIP_JOB.search(name) and not _CHECK_JOB.search(name):
        return "LOW"          # a release step, where skipping is usually the intent
    if not _on_pull_request(doc):
        return "LOW"          # never runs on a PR, so it is not a merge gate
    if _CHECK_JOB.search(name):
        return "HIGH"         # a PR verification job that can silently not run
    return "MEDIUM"


def _needs_of(job) -> list[str]:
    n = job.get("needs") if isinstance(job, dict) else None
    if not n:
        return []
    return list(n) if isinstance(n, list) else [str(n)]


# `!cancelled()` runs on failure just as `always()` does, so a gate conditioned on it
# still sees an upstream's own `failure` result. spiceai/spiceai's e2e-gate is written
# that way, and excluding it left a finding standing whose whole chain that gate
# watches. Only `cancelled()` behaves differently, and a cancelled run is not a merge.
_ALWAYS_CONJUNCT = re.compile(r"\balways\s*\(\s*\)|!\s*cancelled\s*\(\s*\)")


def _runs_like_a_gate(cond) -> bool:
    """Looser than `_truthy_always`, and ONLY for deciding whether a gate covers its needs.

    A fan-in gate is routinely conditioned on more than always():

        if: ${{ always() && github.ref != 'refs/heads/main' }}      # astral-sh/ruff

    `_truthy_always` requires the condition to reduce to exactly always(), which is right for
    D2's own trigger ("this job always runs and still checks nothing") and wrong here: a gate
    that runs on pull requests still catches the failure D4 describes, and branch protection is
    about pull requests. Requiring the bare form left nine false D4/HIGH on ruff after the D1
    half was already fixed.

    Deliberately lenient, and the leniency is bounded: D4 exists to find a gating job that
    NOTHING looks at. A gate that exists but is conditioned is a weaker finding than HIGH, so
    suppressing it is the safer error. A condition that pins the gate to main only would not
    cover pull requests, which this does not model; that is a known limit, not an oversight.
    """
    return cond is not None and bool(_ALWAYS_CONJUNCT.search(str(cond)))


def _upstream_chain(jobs: dict, job: str) -> set[str]:
    """Every job the skip of `job` could transitively originate from."""
    seen, stack = set(), [job]
    while stack:
        for up in _needs_of(jobs.get(stack.pop(), {})):
            if up not in seen:
                seen.add(up)
                stack.append(up)
    return seen


def _jobs_whose_whole_chain_a_gate_watches(jobs: dict) -> set[str]:
    """Jobs for which a fan-in gate can see every failure that could skip them.

    D1's premise is that an upstream skips, the dependent silently skips with it, and a
    skipped job reports Success. That premise needs the SKIP to be invisible. A gate that
    needs the dependent AND every job in its upstream chain sees any real failure in that
    chain directly, by that job's own `failure` result, so the only skip that still gets
    through is a condition deliberately evaluating false, which is the author's intent and
    not a defect.

    Note which direction the transitivity runs, because the opposite form was WRONG and a
    surviving mutant proved it: suppressing a job because its PARENT is covered is unsound,
    since `needs.*.result` reports only direct needs and a failed grandparent makes the
    parent skip rather than fail. This requires the gate to need the whole chain ITSELF,
    which is strictly stricter than reading one level.

    Verified against three findings hand-labelled false for exactly this reason
    (sgl-project/sglang pr-test-mlx x2, spiceai/spiceai test-bigquery): in each one the
    gate's `needs` contains the entire chain, so nothing in it can fail unseen. Gates that
    allow a `skipped` result do not weaken this, and nearly all of them do allow it: a
    path-filtered workflow would otherwise always fail.
    """
    gates = [(n, set(_needs_of(j))) for n, j in jobs.items()
             if isinstance(j, dict) and _runs_like_a_gate(j.get("if"))
             and _reads_any_upstream_state(_job_blob(j))]
    if not gates:
        return set()
    covered = set()
    for name in jobs:
        want = _upstream_chain(jobs, name) | {name}
        if any(want <= (watched | {gate}) for gate, watched in gates):
            covered.add(name)
    return covered


def _workflow_has_any_gate(jobs: dict) -> bool:
    """Does ANY job here consult upstream results at all?"""
    return any(isinstance(j, dict) and _runs_like_a_gate(j.get("if"))
               and _reads_any_upstream_state(_job_blob(j)) for j in jobs.values())


def _d1_severity(name: str, job: dict, doc: dict, has_gate: bool) -> str:
    """D1 severity, capped at MEDIUM for the same reason D4 is.

    D1's finding asserts that a required check is satisfied by a skipped job. Whether the
    job is a required check is precisely what a workflow file cannot say, so a structural
    HIGH was a guess from a word in the job's name.

    Measured before changing it, by hand-labelling the ENTIRE D1 stratum rather than
    sampling it (54 findings, 29 jobs, so a census was cheaper than an argument about two
    disagreeing samples): 65% false per finding even after the API-gate and never-runs
    fixes. Eleven of the survivors are a single reporting job. That is not a tier that can
    keep claiming HIGH on its own authority.

    HIGH is therefore reserved for the escalation path in `protection.py`, where the job has
    been attributed to a confirmed required check. Without that, MEDIUM when nothing in the
    workflow consults upstream results at all, and LOW when the workflow does gate but not
    over this upstream.
    """
    base = _gate_severity(name, job, doc)
    if base == "LOW":
        return "LOW"
    return "MEDIUM" if not has_gate else "LOW"


# D4 was REMOVED in 0.1.5, not demoted.
#
# It scored 0 defensible findings out of 18 in a pre-registered hand-labelled
# sample, and it was the largest detector by volume: 932 findings over 93
# repositories, 47% of everything this tool emitted. Its premise needed a
# branch-protection fact no workflow file carries, so 0.1.3 capped it below HIGH
# and left it reporting. A detector that has never once been right is not
# improved by saying it quietly; LOW still costs a reader attention, and 724 LOW
# findings cost a lot of it. Removal is the honest form of a zero.
# D2 was REMOVED in 0.1.6, on a CENSUS rather than a sample.
#
# Every one of its 27 findings across 275 repositories was hand-labelled: 0 true,
# 27 false, 0 arguable. Its trigger, always() + needs + no result read, is the
# signature of a correctly written reporting job, not of a broken gate: report,
# summary, cost, merge-reports, aggregate_reports, release_lease, and a cleanup
# job that restores an environment policy and MUST run whatever happened.
#
# The structural argument is stronger than the rate. D2 reached MEDIUM only when
# _GATE_NAME matched the job; all 27 findings were LOW, so ZERO matched. By its own
# severity logic it never once found a job it believed was a gate, while the text of
# every finding it emitted said that job's branch protection was decorative.
#
# D4 went on 0 of 18 sampled. D2 goes on 0 of 27 CENSUSED, the whole population.
DETECTORS = (d1_skippable_upstream, d3_pipe_masked_exit)


def active_detectors():
    return DETECTORS


def scan_workflow(doc: dict, base: pathlib.Path | None = None) -> list[Finding]:
    jobs = (doc or {}).get("jobs") or {}
    if not isinstance(jobs, dict):
        return []
    out = []
    for fn in active_detectors():
        if fn is d1_skippable_upstream:
            out.extend(fn(jobs, doc, base))
        else:
            out.extend(fn(jobs, doc))
    return out
