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

## The branch-protection tier

The structural tier reads workflow files and reports the SHAPE of a dead gate. It cannot tell
you whether anything was relying on the job. That needs the branch's required status checks:

```bash
deadgate . --repo owner/name            # read-only GitHub API calls via `gh`
```

Two endpoints answer, and they do not have the same reach:

| endpoint | access needed | what it covers |
|---|---|---|
| `GET /repos/{o}/{r}/rules/branches/{b}` | read | rulesets only |
| `GET /repos/{o}/{r}/branches/{b}/protection` | **admin** | classic protection |

On a repository you do not administer, only the first answers. It says nothing about classic
protection, so an empty result does not mean the branch is unprotected. That asymmetry decides
what the tier is allowed to claim:

- a **match proves** the check is required, so a MEDIUM finding escalates to HIGH
- **no match proves nothing** unless the required set is complete, which needs admin on both
  endpoints. Without that, the verdict is AMBIGUOUS and the severity does not move

Only MEDIUM moves. MEDIUM is the tier that means "the workflow file does not say", so it is the
only one this evidence can settle. A structural HIGH keeps its severity even when a check is not
required, because protection can be added later and may be configured where this API does not
reach. A structural LOW keeps its severity because a release pipeline that skips on purpose does
not become a merge gate by appearing in a list.

### Verdicts

| verdict | meaning | severity |
|---|---|---|
| `REQUIRED` | a derived check name matches a required context | MEDIUM becomes HIGH |
| `NOT_REQUIRED` | complete required set, no match | MEDIUM becomes LOW |
| `UNPROTECTED` | complete, and the branch requires nothing at all | MEDIUM becomes LOW |
| `AMBIGUOUS` | the name could not be derived, or the set is incomplete | unchanged |
| `UNREADABLE` | the API did not answer | unchanged |

### Why it reports contexts it could not attribute

A required status check is identified by its **check-run name**, which is not the job key in the
YAML. If name derivation breaks, no required context matches any job, every MEDIUM resolves to
"not required", and the tool quietly downgrades real defects. So the run prints how many required
contexts it attributed to a job in the repository, and warns when none of them matched anything.
A broken matcher then appears as a number rather than as silence. Some contexts land there
legitimately, from third-party apps or workflows outside the repository, so it is a signal to
read and not an assertion.

Two transport facts are enforced rather than trusted, because both were observed:

- `403` is returned for rate limiting **and** for insufficient permissions. These are separated,
  because one is retryable and the other means this tier cannot help on that repository.
- `404` from the classic endpoint means "not protected" for a branch you administer and "you
  cannot see this" otherwise, with the same status code and only the prose differing. So it is
  gated on `permissions.admin`, never on the message text.

## Measurements

Figures, and the unit each one is in, are in [MEASUREMENT.md](MEASUREMENT.md). Reproduce them
with `python bench/build_cache.py` then `python bench/ab.py`.

The short version: corpus totals are weighted by workflow size, one monorepo carries 55% of the
naive D1 count, and the median affected repository sees 2 findings where the pre-narrowing
detector gave 3. `bench/ab.py` prints the per-repo median, p90 and max alongside every total,
so the unit cannot be dropped by accident.

## Scope, stated plainly

This reads workflow files. It does **not** read branch-protection settings, so it reports the
*shape* of a defect, not whether a given check is actually required on your default branch.
Semantic gate testing, planting the condition a gate claims to catch and asserting it reacts,
is a separate and harder problem and is not in this release.

## Licence
MIT

## 0.1.1 fixes a false-positive class in 0.1.0

If you installed **0.1.0**, upgrade. Three detectors asked whether a job reads its upstream's
result with a pattern that could not match `*`, so the WILDCARD form GitHub documents,

```yaml
if: always()
run: |
  if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then exit 1; fi
```

was invisible to them and a correctly written fan-in gate was reported as a gate that cannot
fail. The finding's own text said "never reads needs.*.result" while failing to match that
string. D4 had a second form of it: it judged each job alone and ignored whether the
workflow's own gate already caught the failure it described.

Measured on one 1.2k-star repository that writes its gates this way: 13 HIGH findings before,
1 after, and the survivor is an unrelated D3. Across the 275-repository corpus the fix removes
43 of 430 HIGH. The gap between those two numbers is the point, and `MEASUREMENT.md` has it:
the tool was least accurate on the repositories with the most careful CI, so a corpus average
hid it. 149 tests passed before and after the fix, which is why there are 156 now.
