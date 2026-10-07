# deadgate

**Find CI checks that cannot fail.**

Flaky-test tools find checks that fail randomly. This finds the opposite: checks that are
structurally incapable of going red, so your pipeline is green for reasons unrelated to your code.

GitHub documents the sharpest case itself:

> A job that is skipped will report its status as `Success`. It will not prevent a pull
> request from merging, even if it is a required check.

```bash
pip install deadgate
deadgate .
```

Exit code is 1 when there are findings, 0 when clean, and **2 when a workflow file could not
be parsed**. A file it could not read is never counted as a file with no problems.

## What it detects

| id | defect | why it matters |
|----|--------|----------------|
| D1 | a job depends on a skip-prone job and never reads `needs.*.result` | the dependency skips, reports Success, and the gate passes with nothing run |
| D2 | a fan-in job runs on `always()` and never reads `needs.*.result` | it is green when the jobs it gates failed |
| D3 | a `run:` step ends a pipeline in a filter with no `pipefail` | the step's status is the filter's, so an upstream failure passes |

Every finding carries a reproduction. A finding without one is an opinion, and this tool
does not emit opinions.

## The calibration corpus is the specification

`corpus/` holds workflows that are **known broken** and workflows that are **known good**, and
the good ones are deliberate near misses of the broken ones. A detector has to fire on the
defect *and* stay quiet on its near miss. A detector that fires on everything is as useless as
one that fires on nothing, and it gets the whole tool switched off.

```bash
python -m pytest tests/
```

Fixtures `g6` and `g7` exist because the detectors were **wrong against real repositories while
the corpus was green**:

- `g6` a job guarding itself with a job-level `if:` that reads `needs.*.outputs` IS checking its
  upstream. The first version only read step-level conditions and flagged correct jobs.
- `g7` `if: ${{ !cancelled() }}` is not skip-prone. It runs in normal operation and on failure.
  Treating every `if:` as a possible skip produced false alarms.

Both were found by running against a real 25k-star repository, not by the suite. That is the
argument this tool makes about everyone else's checks, so it is held to it too.

## Scope, stated plainly

This reads workflow files. It does **not** read branch-protection settings, so it reports the
*shape* of a defect, not whether a given check is actually required on your default branch.
Semantic gate testing, planting the condition a gate claims to catch and asserting it reacts,
is a separate and harder problem and is not in this release.

## Licence
MIT
