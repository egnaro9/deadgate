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
    # There is no severity any more, and this field is kept only so existing callers do
    # not break. Every D3 stratum was censused and the tiers did not rank anything:
    #
    #     HIGH    n= 94   30.9% false  [22.4, 40.8]
    #     LOW     n=415   31.6% false  [27.3, 36.2]   indistinguishable from HIGH
    #     MEDIUM  n=153   55.6% false  [47.6, 63.2]   the only tier that separated, worst
    #
    # HIGH's interval overlaps the merged non-HIGH tier, so the loudest tier was not
    # reliably better than everything else. The one real separation was MEDIUM being
    # WORSE, which came from `_d3_severity` short-circuiting on `_on_pull_request` before
    # it looked at the finding at all: the axis measured the workflow's trigger, not
    # correctness. A severity that hides the better findings behind --all and shouts the
    # worse ones is worse than none.
    severity: str = "FINDING"


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


_NEEDS_NAMED = re.compile(r"needs\.([A-Za-z0-9_\-]+)\.(?:result|outputs)")
_NEEDS_WILDCARD = re.compile(r"needs\.\*\.(?:result|outputs)")


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


def _token_in_scope(job: dict, step: dict) -> bool:
    """Is a GitHub token available to this step?

    JOB-level env, or the step itself naming the token anywhere, since
    `secrets.GITHUB_TOKEN` is reachable by any step that names it.

    `step.get("env")` used to be checked alongside the job's and was REDUNDANT:
    `_job_blob(step)` serialises the step including its env, so everything the step
    branch could match the blob already matched. A mutation that emptied the whole loop
    survived all 221 tests, which is how both halves of that came out: the step branch
    could never contribute, and the JOB branch is load-bearing and had no test. It is
    the `base / rel` lesson a second time, a redundant candidate sitting next to a real
    one and hiding that nothing covered either.
    """
    env = job.get("env")
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
        # All-or-nothing on purpose. A per-name model was built and measured here and is
        # now deleted rather than left sitting unused.
        #
        # Arhan Canli's point in the dev.to thread is right in principle: needs.X.result,
        # needs.* and toJSON(needs) are one rule, so comparing the referenced set against
        # the `needs:` list and reporting only unreferenced upstreams would stop the
        # spelling from mattering, and would stop a gate naming eight of nine needs from
        # suppressing the finding about the ninth.
        #
        # It was implemented and measured against this corpus before being rejected. It
        # surfaces 396 additional findings, D1 going 373 to 769, and classified by job
        # kind they are:
        #
        #     139  35%  ship/publish
        #      96  24%  other
        #      91  23%  reporting/cleanup
        #      66  17%  check/test
        #       4   1%  gate-named
        #
        # 58% are the two families the D2 census measured at 0 true of 27, and 1% are
        # jobs that name themselves a gate. The rule is sound; the population it reaches
        # in D1 is the one already proven false. Revisit if D1's trigger is ever narrowed
        # to jobs that are plausibly gates.
        if _reads_any_upstream_state(text):
            continue
        # Class 7: the job may consult the API instead of `needs`, in a script this
        # tool cannot parse. Only considered when the job announces itself as a gate,
        # so an ordinary job running ./build.sh is untouched.
        # Class 7 still SUPPRESSES when the script provably reads run status. What it
        # used to do on top of that, demote an unverifiable claim a tier, has nowhere to
        # go now that there are no tiers, so it only annotates.
        unverified = ""
        if _GATE_NAME.search(name):
            verdict = _script_reads_status(_delegated_status_scripts(job), base)
            if verdict is True:
                continue
            if verdict is None and _delegated_status_scripts(job):
                unverified = (" NOTE: this job shells out to a script with a token in"
                              " scope, so whether it checks upstream status could not be"
                              " verified from the workflow alone.")
        for up in skippable:
            found.append(Finding(
                detector="D1",
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
    """Bodies of $( ... ), innermost first, so a pipe inside one is analysed on its own.

    Delegates to `_substitution_spans`, which respects shell quoting. The hand-rolled
    scanner this replaced counted parens blind to quotes and escapes, so an escaped paren
    inside a quoted regex closed the substitution early and returned a truncated body.
    It also had an off-by-one before that: `while i < len(line) - 1` never examined the
    final character, so a substitution closing at end of line was never closed at all.
    """
    return [line[a:b] for a, b in _substitution_spans(line)]


def _substitution_spans(text: str) -> list[tuple[int, int]]:
    r"""(start, end) of every `$( ... )` body, innermost first, respecting shell quoting.

    Three things this has to get right, each found by reading a mangled segment in a real
    finding rather than by a test:

    1. Quotes. Counting `$(` and `)` blind made an escaped paren in a quoted regex close
       the substitution early:

           version=$(grep -o 'LLVM_VERSION_\(MAJOR\|MINOR\|PATCH\) [0-9]\+' f | cut -d ' ' -f 2)

       reported its filter as `PATCH\`.

    2. A substitution body is parsed FRESH. Inside double quotes a single quote is
       literal, but inside `$( ... )` nested in double quotes it quotes again, so

           touched="$(sed -nE 's#^(packages/[^/]+)/.*##p' f | sort -u)"

       has its `)` protected by the inner single quotes. Treating the body as still
       double-quoted closed the span at `+)` and produced `touched="/.*##p' f | sort -u)"`.

    3. Process substitution. `<( ... )` and `>( ... )` take a paren too, and ignoring them
       let their closing paren close the enclosing `$(`:

           count_new_js=$(comm -1 -2 <(git diff ...) <(gh pr view ... | jq ...))

       lost everything after the first `<(...)`.
    """
    spans, stack = [], []
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        c = text[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == "\\":
            i += 2                      # escaped character, whatever it is
            continue
        if c == "'" and not in_double:
            in_single = True; i += 1; continue
        if c == '"':
            in_double = not in_double; i += 1; continue
        # `$(`, `<(` and `>(` all open a paren that must be matched. A command
        # substitution body is parsed fresh, so the quote state is saved and reset.
        if c in "$<>" and i + 1 < n and text[i + 1] == "(":
            stack.append((i + 2, in_double, c))
            in_double = False
            i += 2
            continue
        if c == ")" and stack:
            start, saved_double, kind = stack.pop()
            in_double = saved_double
            if kind == "$":
                spans.append((start, i))
        i += 1
    return spans


def _strip_substitutions(text: str) -> str:
    """The text with every top-level `$( ... )` removed, so the OUTER command stands alone."""
    spans = _substitution_spans(text)
    if not spans:
        return text
    top = []
    for a, b in sorted(spans):
        if not top or a > top[-1][1]:
            top.append((a, b))
    out, prev = [], 0
    for a, b in top:
        out.append(text[prev:a - 2])    # drop the "$(" too
        prev = b + 1                    # and the ")"
    out.append(text[prev:])
    return "".join(out)


def _pipeline_candidates(line: str) -> list[tuple[str, str]]:
    """Every pipeline in this line, as (segment, filter), inner ones included.

    A line can hold more than one pipeline, at different nesting depths, and they are not
    equally guilty:

        echo "batch=$(grep -Po '...' list.txt | python3 -c '...')" | tee "$GITHUB_OUTPUT"

    The OUTER pipeline is `echo ... | tee`, whose head is echo and cannot fail. The INNER
    one is `grep | python3`, whose head can. Splitting the whole line on its last pipe
    only ever sees the outer one, so this reported `| tee` while the real masking sat in
    the substitution, and `_upstream_can_fail` then waved it through on the grounds that
    "the outer head holds a substitution we already judged benign" -- which it had not,
    because that loop only returns False for benign substitutions and falls through for
    the rest.

    Judging each pipeline against its OWN head removes the four such false positives in
    the 125-finding census and reports the right filter where one survives.
    """
    out = []
    for body in _substitution_bodies(line):
        if "|" in body:
            seg = body
            tail = seg.rsplit("|", 1)[1].strip().split()
            if tail:
                out.append((seg, tail[0]))
    outer = _strip_substitutions(line)
    if "|" in outer:
        tail = outer.rsplit("|", 1)[1].strip().split()
        if tail:
            out.append((outer, tail[0]))
    return out


def _segment_head_can_fail(segment: str) -> bool:
    """Can the command before this segment's final pipe fail?

    The segment has already been isolated from its enclosing line, so any substitution
    still inside it is part of the head and is judged with it.
    """
    head = segment.rsplit("|", 1)[0].strip()
    inner = _strip_substitutions(head).strip()
    if _CANNOT_FAIL.match(inner or head):
        # The command itself cannot fail. It can still inherit a failure from something
        # it substitutes, so those are judged too rather than assumed benign.
        return any(_segment_head_can_fail(b) if "|" in b else not _CANNOT_FAIL.match(b.strip())
                   for b in _substitution_bodies(head))
    return True


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



# All 6 such findings in D3's MEDIUM census were false, as were the ones in the HIGH
# census, and no labelled true finding has this shape.
#
# The residual case is a head that prints a matching line and THEN fails. That is real but
# narrow, and reporting every assertion to catch it is the trade this corpus says no to.
_TAIL_IS_ASSERTION = re.compile(r"^grep\b.*(?:\s-\w*q|\s--quiet|\s--silent)")


def _tail_is_a_content_assertion(segment: str) -> bool:
    """True when the final filter is a `grep -q`, which fails when its input is empty."""
    tail = segment.rsplit("|", 1)[1].strip() if "|" in segment else ""
    return bool(_TAIL_IS_ASSERTION.match(tail))


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
            # One finding per DISTINCT pipeline in a step, not one per step. The old
            # `break` capped a step at a single finding, so a step with two masked
            # pipelines reported whichever came first.
            seen_here: set = set()
            reported_here = False
            for line in _logical_lines(body):
                line = line.strip()
                if "|" not in line or line.startswith("#") or "||" in line:
                    continue
                if _CONDITION_LINE.search(line) or _ONLY_PRINTED.search(line):
                    continue
                # Every pipeline on the line, inner ones included, each judged against
                # its OWN head. The old code split the whole line on its last pipe, so a
                # benign outer `echo ... | tee` hid a real `grep | python3` inside a
                # substitution, and the `break` below meant the inner one was never
                # reached even in principle.
                for segment, filt in _pipeline_candidates(line):
                    if filt not in FILTERS or not _segment_head_can_fail(segment):
                        continue
                    if _tail_is_a_content_assertion(segment):
                        continue
                    assigned = (re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line) or [None, ""])[1]
                    if _result_is_emptiness_checked(assigned, body):
                        continue
                    if (name, id(step), segment, filt) in seen_here:
                        continue
                    seen_here.add((name, id(step), segment, filt))
                    tail = [filt]
                    label = step.get("name") or line[:40]
                    found.append(Finding(
                        detector="D3",
                        job=name,
                        title="exit status masked by a pipe",
                        detail=(f"step '{label}' in job '{name}' ends a pipeline with "
                                f"'{tail[0]}' and does not set pipefail. The step's status is "
                                f"{tail[0]}'s, so a failure upstream of the pipe passes."),
                        repro=(f"Make the command before '| {tail[0]}' fail. The step still "
                               f"succeeds. Add 'set -o pipefail' and it fails correctly."),
                    ))
                    # One finding per step, still. The `break` was blamed for hiding a
                    # real inner pipeline behind a benign outer one, but that was the
                    # other bug: the outer was CHOSEN because the whole line was split on
                    # its last pipe. With candidates ordered innermost-first and each
                    # judged against its own head, the first qualifying pipeline is the
                    # guilty one, so stopping here loses nothing.
                    #
                    # Removing it was tried and measured: D3 HIGH went 94 to 125, roughly
                    # 31 additional pipelines inside steps that already reported one. None
                    # of those 31 is in the hand-labelled census, so their precision is
                    # unknown, and shipping an unmeasured 33% increase in the loudest tier
                    # is the move this whole measurement series exists to avoid.
                    reported_here = True
                    break
                if reported_here:
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
