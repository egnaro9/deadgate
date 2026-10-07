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
from dataclasses import dataclass

FILTERS = {"grep", "jq", "head", "tail", "tee", "awk", "sed", "cut", "sort", "uniq", "wc", "tr"}


@dataclass(frozen=True)
class Finding:
    detector: str
    job: str
    title: str
    detail: str
    repro: str


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
        text = _steps_text(job)
        if re.search(r"needs\.[A-Za-z0-9_\-]+\.(result|outputs)", text):
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


def d1_skippable_upstream(jobs: dict) -> list[Finding]:
    """A conditional job that something depends on, where the dependent never checks the result.

    GitHub documents that a skipped job reports Success and does not prevent a merge even
    as a required check, and that a skipped conclusion is treated as success for dependent
    checks. So skipping the real work silently satisfies the gate.
    """
    found = []
    conditional = {n for n, j in jobs.items()
                   if isinstance(j, dict) and _skip_prone(j.get("if"))}
    # A dependent whose own if: reads the upstream's outputs is deliberately gated on it.
    # That is the change-detection pattern, it is intentional, and D4 covers its real
    # failure mode. Measured: it was 17 of 20 sampled D1 findings, i.e. most of the noise.
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
        text = _steps_text(job)
        if re.search(r"needs\.[A-Za-z0-9_\-]+\.(result|outputs)", text):
            continue
        if re.search(r"needs\.[A-Za-z0-9_\-]+\.outputs\.", str(job.get("if") or "")):
            continue
        for up in skippable:
            found.append(Finding(
                detector="D1",
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


def d3_pipe_masked_exit(jobs: dict) -> list[Finding]:
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


def d4_outputs_gate_without_result_check(jobs: dict) -> list[Finding]:
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
        checked = set(re.findall(r"needs\.([A-Za-z0-9_\-]+)\.result", cond + _steps_text(job)))
        for up in sorted(ups - checked):
            found.append(Finding(
                detector="D4",
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


def scan_workflow(doc: dict) -> list[Finding]:
    jobs = (doc or {}).get("jobs") or {}
    if not isinstance(jobs, dict):
        return []
    out = []
    for fn in DETECTORS:
        out.extend(fn(jobs))
    return out
