"""Derive the check-run names a workflow job produces.

A required status check is identified by its CHECK-RUN NAME, which is not the job key in
the YAML. Getting this wrong is the way this tier lies: if derivation silently fails, no
required context matches any job, every finding resolves to "not required", and the tool
quietly downgrades real defects. So derivation reports its own confidence, and anything it
cannot work out is AMBIGUOUS rather than "no match".

The naming rules GitHub actually applies:

  no `name:`                     the job key
  no `name:` + a matrix          `<job key> (<values, in matrix key order>)`
  `name:` + a matrix             that string, ALSO suffixed `(<values>)`
  `name:` already naming matrix  the string per leg, with expressions substituted, no suffix
  `uses:` (reusable workflow)    `<caller name> / <callee job name>`

The rule for a static `name:` with a matrix is append-the-leg-values, not use-it-once. Verified
against the authoritative surface: promptfoo's `main` branch requires the context
`Check Python (3.9)` while its workflow declares a static `name: Check Python` over a
`python-version` matrix, and its `Build on Node ${{ matrix.node }}` job, whose name already
references the matrix, is required as the unsuffixed `Build on Node 24.x`. Encoding the
opposite made every leg of a statically named matrix job match nothing.

Matrix legs and reusable callees are matched by PREFIX rather than enumerated, because the
prefix is decidable from the caller alone while the values often are not. That keeps a
`matrix: ${{ fromJson(needs.x.outputs.y) }}` job decidable instead of ambiguous.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

EXACT = "EXACT"
AMBIGUOUS = "AMBIGUOUS"

_EXPR = re.compile(r"\$\{\{(.+?)\}\}", re.S)
_MATRIX_REF = re.compile(r"^\s*matrix\.([A-Za-z_][\w-]*)\s*$")
MAX_COMBINATIONS = 256


@dataclass(frozen=True)
class Derived:
    """Check-run names a job produces, plus how sure we are."""
    names: frozenset[str]
    prefixes: frozenset[str]
    confidence: str
    reason: str = ""

    def matches(self, context: str) -> bool:
        if self.confidence != EXACT:
            raise ValueError("refusing to match on an AMBIGUOUS derivation")
        return context in self.names or any(context.startswith(p) for p in self.prefixes)


def _render(value) -> str:
    """A matrix value as GitHub interpolates it, which is not Python's str().

    A YAML boolean renders `true`, not `True`, and null renders as the empty string. Getting
    this wrong produces a name that matches nothing, and a name that matches nothing clears
    the finding.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _literal_matrix(strategy) -> dict | None:
    """The matrix as literal lists, or None when any part of it is computed.

    `include` and `exclude` add and remove legs by rules not worth guessing at: an
    under-enumerated name set silently fails to match a context that IS required, which clears
    the finding. Both are lists of mappings, so they make the matrix non-literal here.
    """
    if not isinstance(strategy, dict):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return None
    axes = {}
    for key, val in matrix.items():
        # `include` and `exclude` land here too, and that is deliberate: both are lists of
        # mappings, so the check below already makes the matrix non-literal and the caller
        # falls through to AMBIGUOUS. An explicit guard for them was written first and was
        # unreachable, which is the defect this tool is named after.
        if not isinstance(val, list) or any(isinstance(v, (dict, list)) for v in val):
            return None
        if any("${{" in str(v) for v in val):
            return None
        axes[key] = [_render(v) for v in val]
    return axes or None


def _substitute(template: str, axes: dict) -> frozenset[str] | None:
    """Expand a `name:` template over a literal matrix, or None if it cannot be expanded."""
    refs = []
    for raw in _EXPR.findall(template):
        m = _MATRIX_REF.match(raw)
        if not m or m.group(1) not in axes:
            return None          # an expression we cannot evaluate: not our business to guess
        refs.append(m.group(1))
    if not refs:
        return frozenset({template})
    used = {r: axes[r] for r in dict.fromkeys(refs)}
    combos = list(itertools.product(*used.values()))
    if len(combos) > MAX_COMBINATIONS:
        return None
    out = set()
    for combo in combos:
        values = dict(zip(used.keys(), combo))
        # GitHub trims the rendered name, which matters when a leg's value is empty.
        out.add(_EXPR.sub(lambda m: values[_MATRIX_REF.match(m.group(1)).group(1)],
                          template).strip())
    # A job SKIPPED by its job-level if: emits one check run carrying the RAW template, with
    # the expressions left literal and no matrix expansion. Skipped jobs are precisely this
    # tool's target population, so the unexpanded form is a legitimate candidate name.
    out.add(template.strip())
    return frozenset(out)


def derive(job_key: str, job: dict, workflow_callable: bool = False) -> Derived:
    """The check-run names job `job_key` produces.

    `workflow_callable` says this job's own workflow declares `on: workflow_call`. When another
    workflow in the repository `uses:` it, GitHub names the check `<caller job> / <this job>`,
    and which caller, if any, is not decidable from this file. Guessing the unprefixed name
    clears a finding on a job that IS a required gate, so this is AMBIGUOUS instead.
    """
    job = job if isinstance(job, dict) else {}
    if workflow_callable:
        return Derived(frozenset(), frozenset(), AMBIGUOUS,
                       "workflow is `uses:`-callable, so its checks may be named "
                       "`<caller> / <job>` and the caller is not knowable from this file")
    template = job.get("name")
    axes = _literal_matrix(job.get("strategy"))
    has_matrix = isinstance(job.get("strategy"), dict) and job["strategy"].get("matrix") is not None

    if template is None:
        base = job_key
        if "${{" in base:
            return Derived(frozenset(), frozenset(), AMBIGUOUS, "job key contains an expression")
        if job.get("uses"):
            # A reusable call produces `caller / callee`, and a MATRIXED one produces
            # `caller (values) / callee`. Checking uses: before the matrix emitted only the
            # first prefix, so every matrixed reusable call matched nothing.
            prefixes = {f"{base} / "}
            if has_matrix:
                prefixes.add(f"{base} (")
            # A skipped reusable call emits the bare caller name with no callee suffix.
            return Derived(frozenset({base}), frozenset(prefixes), EXACT,
                           "reusable workflow" + (" with matrix legs" if has_matrix else ""))
        prefixes = frozenset({f"{base} ("}) if has_matrix else frozenset()
        return Derived(frozenset({base}), prefixes, EXACT,
                       "job key" + (" with matrix legs" if has_matrix else ""))

    template = str(template)
    if "${{" not in template:
        # A static name is SUFFIXED with the leg values when the job has a matrix. Encoding
        # the opposite, that it is used once verbatim, made every leg match nothing.
        prefixes = {f"{template} ("} if has_matrix else set()
        if job.get("uses"):
            prefixes.add(f"{template} / ")
            return Derived(frozenset({template}), frozenset(prefixes), EXACT,
                           "reusable workflow" + (" with matrix legs" if has_matrix else ""))
        return Derived(frozenset({template}), frozenset(prefixes), EXACT,
                       "explicit name" + (" with matrix legs" if has_matrix else ""))

    if axes is None:
        return Derived(frozenset(), frozenset(), AMBIGUOUS,
                       "name: has an expression and the matrix is not literal")
    names = _substitute(template, axes)
    if names is None:
        return Derived(frozenset(), frozenset(), AMBIGUOUS,
                       "name: has an expression that does not resolve from the matrix")
    if job.get("uses"):
        return Derived(names, frozenset(f"{n} / " for n in names), EXACT,
                       "reusable workflow, name expanded over the matrix")
    return Derived(names, frozenset(), EXACT, "name expanded over the matrix")
