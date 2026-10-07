"""deadgate: find CI checks that cannot fail."""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

from .detectors import scan_workflow


def workflows(root: pathlib.Path):
    for pat in ("*.yml", "*.yaml"):
        yield from sorted(root.rglob(f".github/workflows/{pat}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deadgate", description=__doc__)
    ap.add_argument("path", nargs="?", default=".", help="repository root")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--all", action="store_true",
                    help="include LOW findings (release and deploy pipelines, where a skip "
                         "is usually the intent). Hidden by default so the output stays actionable.")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.path)
    files = list(workflows(root))
    if not files:
        print(f"no workflow files under {root}/.github/workflows", file=sys.stderr)
        return 0

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
        for x in scan_workflow(doc):
            if x.severity == "LOW" and not args.all:
                suppressed += 1
                continue
            findings.append((f, x))

    if not args.quiet:
        for f, x in findings:
            rel = f.relative_to(root) if f.is_relative_to(root) else f
            print(f"[{x.detector}/{x.severity}] {rel}::{x.job}  {x.title}")
            print(f"        {x.detail}")
            print(f"        repro: {x.repro}\n")

    tail = f", {suppressed} LOW hidden (use --all)" if suppressed else ""
    print(f"{len(files)} workflow file(s), {len(findings)} finding(s){tail}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
