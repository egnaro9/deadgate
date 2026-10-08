"""Structural detectors for CI checks that cannot fail.

Every detector reports a FINDING only when the defect is STRUCTURAL and decidable from
the workflow file alone. Anything needing branch-protection state or runtime history is
out of scope here and belongs to the API tier.

Design rule taken from gate_mutation_sweep.py: a detector must fire on the planted defect
AND stay quiet on a near miss. A detector that fires on everything gets the whole tool
switched off, which is worse than not shipping it.
"""
from __future__ import annotations

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


def _skip_prone(cond) -> bool:
    """A job-level if: that can realistically evaluate false and skip the job."""
    if cond is None:
        return False
    s = re.sub(r"^\$\{\{\s*|\s*\}\}$", "", str(cond).strip()).strip()
    return not _NEVER_SKIPS.match(s)


def _steps_text(job: dict) -> str:
    """All text a job's steps can reference, so we can look for needs.* reads."""
    out = []
    for step in job.get("steps") or []:
        if isinstance(step, dict):
            out.append(str(step.get("run", "")))
            out.append(str(step.get("if", "")))
            env = step.get("env") or {}
            if isinstance(env, dict):
                out.extend(str(v) for v in env.values())
            with_ = step.get("with") or {}
            if isinstance(with_, dict):
                out.extend(str(v) for v in with_.values())
    out.append(str(job.get("env") or ""))
    # The job-level if: is a result check too. A job guarded by
    # `if: always() && needs.x.outputs.y` IS reading its upstream, and missing this
    # was a LEAKY bug found by running against real workflows, not by the corpus.
    out.append(str(job.get("if") or ""))
    return "\n".join(out)


# GitHub's documented fan-in idiom is the WILDCARD form, `needs.*.result`, which is how a
# gate asks about every upstream at once. The detectors below originally matched only
# `needs.<name>.result` via [A-Za-z0-9_-]+, which cannot match "*", so a correctly written
# gate was reported as a gate that cannot fail. The message even said "never reads
# needs.*.result" while failing to match that exact string. Measured on Arize-ai/openinference,
# whose three `ci-required` jobs all read it twice: seven HIGH findings, every one false.
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
_NEEDS_WILDCARD_RESULT = re.compile(r"needs\.\*\.result")


def _reads_all_upstream_state(text: str) -> bool:
    """True when the text consults EVERY upstream at once, by wildcard or by whole context."""
    return bool(_NEEDS_WILDCARD_RESULT.search(text) or _NEEDS_WHOLE_CONTEXT.search(text))


def _job_blob(job) -> str:
    """The WHOLE job serialised, for the question "does this job consult its upstreams?".

    Placement, not spelling, was the fourth false-positive class. A job that calls a reusable
    workflow has `uses:` and no `steps:`, and passes state through JOB-level `with:`:

        update-tracker:
          uses: ./.github/workflows/update_tracking_issue.yml
          if: ${{ always() }}
          needs: [check-sdist]
          with: { job_status: "${{ needs.check-sdist.result }}" }   # <- read, and missed

    `_steps_text` covers step-level `with` and env but not the job-level `with` that only
    exists for reusable calls, so scikit-learn's gate read as checking nothing. Serialising the
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


def d2_fanin_without_result_check(jobs: dict) -> list[Finding]:
    """A fan-in job that runs on always() and never reads needs.*.result.

    It will be GREEN when the jobs it gates FAILED. If it is a required check, the
    branch protection it provides is decorative.
    """
    found = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        if not needs or not _truthy_always(job.get("if")):
            continue
        text = _job_blob(job)
        if _reads_any_upstream_state(text):
            continue
        upstream = ", ".join(needs) if isinstance(needs, list) else str(needs)
        found.append(Finding(
            detector="D2",
            job=name,
            title="fan-in gate cannot fail",
            detail=(f"job '{name}' needs [{upstream}] and runs on always(), but no step reads "
                    f"needs.*.result. It reports success even when [{upstream}] fail."),
            repro=(f"Make any of [{upstream}] exit 1 and re-run. '{name}' still succeeds, "
                   f"and any branch protection requiring it still passes."),
        ))
    return found


def d1_skippable_upstream(jobs: dict, doc: dict | None = None) -> list[Finding]:
    """A conditional job that something depends on, where the dependent never checks the result.

    GitHub documents that a skipped job reports Success and does not prevent a merge even
    as a required check, and that a skipped conclusion is treated as success for dependent
    checks. So skipping the real work silently satisfies the gate.
    """
    found = []
    conditional = {n for n, j in jobs.items()
                   if isinstance(j, dict) and _skip_prone(j.get("if"))}
    # A dependent whose own if: reads the upstream's outputs is deliberately gated on it:
    # the change-detection pattern, which is intentional, and whose real failure mode is
    # D4's. That exclusion happens in _steps_text, which reads the job-level if:. An
    # explicit second check here was DEAD CODE and is removed; the A/B that was supposed
    # to prove it worked reported a difference of exactly zero and found it instead.
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        if not needs:
            continue
        needs_list = needs if isinstance(needs, list) else [needs]
        skippable = [n for n in needs_list if n in conditional]
        if not skippable:
            continue
        text = _job_blob(job)
        if _reads_any_upstream_state(text):
            continue
        sev = _gate_severity(name, job, doc or {})
        for up in skippable:
            found.append(Finding(
                detector="D1",
                severity=sev,
                job=name,
                title="gate satisfied by a skipped job",
                detail=(f"job '{name}' depends on '{up}', which is conditional. A skipped job "
                        f"reports Success, and '{name}' never reads needs.{up}.result."),
                repro=(f"Open a PR where '{up}'s if: condition is false. '{up}' skips, "
                       f"'{name}' succeeds, and nothing ran."),
            ))
    return found


_CANNOT_FAIL = re.compile(r"^\s*(echo|printf)\b")


def _substitution_bodies(line: str) -> list[str]:
    """Bodies of $( ... ), innermost first, so a pipe inside one is analysed on its own."""
    out, stack = [], []
    i = 0
    while i < len(line) - 1:
        if line[i] == "$" and line[i + 1] == "(":
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
    if decides or exported or consumed:
        return "HIGH"
    return "MEDIUM"


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
            for line in body.splitlines():
                line = line.strip()
                if "|" not in line or line.startswith("#") or "||" in line:
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


_ALWAYS_CONJUNCT = re.compile(r"\balways\s*\(\s*\)")


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


def _jobs_covered_by_a_wildcard_gate(jobs: dict) -> set[str]:
    """Jobs whose FAILURE is already caught by a fan-in gate in the same workflow.

    D4's premise is that a failed gating job skips its dependents and the skip reports
    Success, so the merge is green with nothing tested. That premise needs the failure to
    reach nobody. A job running on always() that reads `needs.*.result` sees the failure of
    every job it needs, directly or transitively, so for those jobs the premise is false and
    the finding is noise.

    Measured on Arize-ai/openinference: five D4 findings, three of them HIGH, in four
    workflows that each carry exactly such a gate. Reported in isolation they read as "the
    merge is green with nothing tested", and the merge is not green.
    """
    covered: set[str] = set()
    for name, job in jobs.items():
        if not isinstance(job, dict) or not _runs_like_a_gate(job.get("if")):
            continue
        if not _reads_all_upstream_state(_job_blob(job)):
            continue
        covered |= set(_needs_of(job))
    return covered


def d4_outputs_gate_without_result_check(jobs: dict, doc: dict | None = None) -> list[Finding]:
    """A job gated on an upstream's OUTPUTS, with nothing checking that upstream SUCCEEDED.

    The common change-detection shape:  if: needs.detect.outputs.rust == 'true'

    That is a deliberate optimisation and is NOT a defect by itself. The defect is what
    happens when `detect` FAILS rather than decides: a failed job sets no outputs, the
    comparison is false, the dependent job SKIPS, and a skipped job reports Success. One
    broken detector silently disables the tests it gates, and the merge is green.

    A job that also reads needs.<up>.result is doing it correctly and is not reported.
    """
    found = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        cond = str(job.get("if") or "")
        if not cond:
            continue
        ups = set(re.findall(r"needs\.([A-Za-z0-9_\-]+)\.outputs\.", cond))
        if not ups:
            continue
        blob = cond + _job_blob(job)
        checked = set(re.findall(r"needs\.([A-Za-z0-9_\-]+)\.result", blob))
        if _reads_all_upstream_state(blob):
            # `needs.*.result` asks about EVERY upstream, so it checks all of them at once.
            # Expanding it is the difference between a correct gate and a reported one.
            checked |= ups
        sev = _gate_severity(name, job, doc or {})
        for up in sorted(ups - checked - _jobs_covered_by_a_wildcard_gate(jobs)):
            found.append(Finding(
                detector="D4",
                severity=sev,
                job=name,
                title="tests silently disabled if the gating job fails",
                detail=(f"job '{name}' runs only when '{up}' outputs say so, and nothing checks "
                        f"needs.{up}.result. If '{up}' FAILS, its outputs are unset, the "
                        f"condition is false, '{name}' skips, and a skipped job reports Success."),
                repro=(f"Make '{up}' exit 1. '{name}' does not run, reports Success, and any "
                       f"branch protection requiring it passes with nothing tested."),
            ))
    return found


DETECTORS = (d1_skippable_upstream, d2_fanin_without_result_check,
              d3_pipe_masked_exit, d4_outputs_gate_without_result_check)


def active_detectors():
    return DETECTORS


def scan_workflow(doc: dict) -> list[Finding]:
    jobs = (doc or {}).get("jobs") or {}
    if not isinstance(jobs, dict):
        return []
    out = []
    for fn in active_detectors():
        out.extend(fn(jobs, doc) if fn is not d2_fanin_without_result_check else fn(jobs))
    return out
