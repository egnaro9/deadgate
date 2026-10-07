"""Join a structural finding to what the branch actually requires.

The structural tier reports the SHAPE of a dead gate. This tier answers the question the
workflow file cannot: was anything relying on that job. Only MEDIUM moves, because MEDIUM is
precisely the tier that means "the file does not say". A structural HIGH keeps its severity
even when a check is not required, since protection can be added later and may be configured
where this API does not reach; a structural LOW keeps its severity because a release pipeline
that skips on purpose does not become a merge gate by being listed.

Resolution never downgrades on an absence it cannot vouch for. Absence of a match means
"not required" only when the required set is COMPLETE; otherwise it is AMBIGUOUS.
"""
from __future__ import annotations

from dataclasses import dataclass

from .checknames import AMBIGUOUS as DERIVE_AMBIGUOUS, derive
from .protection import PROTECTED, Protection, UNPROTECTED, UNREADABLE

REQUIRED = "REQUIRED"
NOT_REQUIRED = "NOT_REQUIRED"
AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Resolution:
    verdict: str
    severity: str
    structural: str
    why: str

    @property
    def moved(self) -> bool:
        return self.severity != self.structural


def resolve(severity: str, job_key: str, job: dict, prot: Protection,
            workflow_callable: bool = False) -> Resolution:
    keep = lambda verdict, why: Resolution(verdict, severity, severity, why)

    if prot.state == UNREADABLE:
        return keep(UNREADABLE, f"could not read what this branch requires ({prot.detail})")

    d = derive(job_key, job, workflow_callable)
    if d.confidence == DERIVE_AMBIGUOUS:
        return keep(AMBIGUOUS, f"cannot derive this job's check name: {d.reason}")

    if any(d.matches(ctx) for ctx in prot.required):
        hit = sorted(ctx for ctx in prot.required if d.matches(ctx))
        sev = "HIGH" if severity == "MEDIUM" else severity
        return Resolution(REQUIRED, sev, severity,
                          f"required on the protected branch as {hit[0]!r}"
                          + (f" (+{len(hit) - 1} more)" if len(hit) > 1 else ""))

    if not prot.complete:
        return keep(AMBIGUOUS,
                    "no match, but the required set is incomplete without admin on this "
                    f"repository, so absence proves nothing ({prot.detail})")

    verdict = UNPROTECTED if prot.state == UNPROTECTED else NOT_REQUIRED
    sev = "LOW" if severity == "MEDIUM" else severity
    why = ("this branch requires no status checks at all" if verdict == UNPROTECTED
           else "not in the branch's required checks, and that set is complete")
    return Resolution(verdict, sev, severity, why)


@dataclass(frozen=True)
class Attribution:
    """Which required contexts this repository's jobs account for.

    `unattributed` is the instrument check. If the name derivation breaks, every context lands
    here and every finding resolves away quietly; as a printed number it is visible instead.
    Some unattributed contexts are legitimate, from third-party apps or from workflows that
    do not live in this repository, so this is a signal to read and not an assertion.
    """
    required: tuple[str, ...]
    attributed: dict[str, tuple[str, ...]]
    unattributed: tuple[str, ...]
    undecidable_jobs: tuple[str, ...]

    @property
    def suspicious(self) -> bool:
        return bool(self.required) and not self.attributed


def workflow_is_callable(doc: dict | None) -> bool:
    """Does this workflow declare `on: workflow_call`?"""
    on = (doc or {}).get("on", (doc or {}).get(True))
    if isinstance(on, str):
        return on == "workflow_call"
    if isinstance(on, list):
        return "workflow_call" in on
    if isinstance(on, dict):
        return "workflow_call" in on
    return False


def attribute(prot: Protection, jobs_by_file: dict[str, dict],
              callable_files: frozenset[str] = frozenset()) -> Attribution:
    """`jobs_by_file` maps a workflow path to that file's `jobs` mapping."""
    hits: dict[str, list[str]] = {}
    undecidable: list[str] = []
    for path, jobs in jobs_by_file.items():
        for key, job in (jobs or {}).items():
            d = derive(key, job if isinstance(job, dict) else {}, path in callable_files)
            if d.confidence == DERIVE_AMBIGUOUS:
                undecidable.append(f"{path}::{key}")
                continue
            for ctx in prot.required:
                if d.matches(ctx):
                    hits.setdefault(ctx, []).append(f"{path}::{key}")
    return Attribution(
        tuple(sorted(prot.required)),
        {k: tuple(v) for k, v in sorted(hits.items())},
        tuple(sorted(set(prot.required) - set(hits))),
        tuple(sorted(undecidable)),
    )
