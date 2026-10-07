"""deadgate: find CI checks that cannot fail."""
from __future__ import annotations

import argparse
import pathlib
from dataclasses import replace
import sys

import yaml

from .detectors import scan_workflow
from .protection import fetch, gh_api, repo_meta, selftest
from .resolve import attribute, resolve, workflow_is_callable


def workflows(root: pathlib.Path):
    for pat in ("*.yml", "*.yaml"):
        yield from sorted(root.rglob(f".github/workflows/{pat}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deadgate", description=__doc__)
    ap.add_argument("path", nargs="?", default=".", help="repository root")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--repo", metavar="OWNER/NAME",
                    help="resolve MEDIUM findings against what the branch actually requires. "
                         "Read-only GitHub API calls via `gh`. On a repository you do not "
                         "administer the required set is incomplete, so findings can only be "
                         "escalated, never cleared.")
    ap.add_argument("--branch", help="branch to read protection from (default: the repo's default)")
    ap.add_argument("--all", action="store_true",
                    help="include LOW findings (release and deploy pipelines, where a skip "
                         "is usually the intent). Hidden by default so the output stays actionable.")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.path)
    files = list(workflows(root))
    if not files:
        print(f"no workflow files under {root}/.github/workflows", file=sys.stderr)
        return 0

    prot = None
    jobs_by_file: dict[str, dict] = {}
    callable_files: set[str] = set()
    if args.repo:
        api = gh_api()
        ok, why = selftest(api)
        if not ok:
            # Refuse rather than report every finding UNREADABLE, which would look like a
            # result about the repository instead of a broken transport.
            print(f"GitHub API transport self-test failed: {why}", file=sys.stderr)
            return 2
        default_branch, admin = repo_meta(args.repo, api)
        branch = args.branch or default_branch
        if not branch:
            print(f"could not read {args.repo} from the GitHub API; is `gh` authenticated?",
                  file=sys.stderr)
            return 2
        prot = fetch(args.repo, branch, api, admin=admin)
        print(f"branch {branch}: {prot.state}"
              f"{f', {len(prot.required)} required check(s)' if prot.required else ''}"
              f"{'' if prot.complete else ', required set INCOMPLETE (no admin)'}"
              f"{f' [{prot.detail}]' if prot.detail else ''}\n")

    findings = []
    suppressed = 0
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text())
        except yaml.YAMLError as exc:
            # Refuse to report a clean result for a file we could not read. A parse
            # failure counted as "no findings" is the exact defect this tool exists to find.
            print(f"UNREADABLE {f}: {exc.__class__.__name__}", file=sys.stderr)
            return 2
        jobs_by_file[str(f)] = (doc or {}).get("jobs") or {}
        if workflow_is_callable(doc):
            callable_files.add(str(f))
        for x in scan_workflow(doc):
            why = ""
            if prot is not None:
                job = ((doc or {}).get("jobs") or {}).get(x.job)
                r = resolve(x.severity, x.job, job if isinstance(job, dict) else {}, prot,
                            str(f) in callable_files)
                why = f"{r.verdict}: {r.why}"
                if r.moved:
                    why = f"{r.structural} -> {r.severity}  {why}"
                x = replace(x, severity=r.severity)
            if x.severity == "LOW" and not args.all:
                suppressed += 1
                continue
            findings.append((f, x, why))

    if not args.quiet:
        for f, x, why in findings:
            rel = f.relative_to(root) if f.is_relative_to(root) else f
            print(f"[{x.detector}/{x.severity}] {rel}::{x.job}  {x.title}")
            print(f"        {x.detail}")
            if why:
                print(f"        branch:  {why}")
            print(f"        repro: {x.repro}\n")

    tail = f", {suppressed} LOW hidden (use --all)" if suppressed else ""
    print(f"{len(files)} workflow file(s), {len(findings)} finding(s){tail}")

    if prot is not None and prot.required:
        att = attribute(prot, jobs_by_file, frozenset(callable_files))
        print(f"required checks: {len(att.attributed)}/{len(att.required)} attributed to a job "
              f"in this repository")
        if att.unattributed:
            # Printed because a broken name derivation shows up here as a number instead of
            # as silently cleared findings. Third-party checks land here legitimately.
            print(f"  unattributed: {', '.join(att.unattributed[:8])}"
                  f"{' ...' if len(att.unattributed) > 8 else ''}")
        if att.undecidable_jobs:
            print(f"  jobs whose check name could not be derived: {len(att.undecidable_jobs)}")
        if att.suspicious:
            print("  WARNING: no required check matched any job here. Either every gate is "
                  "external, or the name derivation is broken. Do not read the severities "
                  "above as resolved.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
