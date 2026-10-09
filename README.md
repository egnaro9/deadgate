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

The short version: a corpus total is weighted by workflow size, so one monorepo's release
pipeline can carry most of it while the repository you actually run this on sees a handful.
`bench/ab.py` prints the per-repo median, p90 and max alongside every total, so the unit cannot
be dropped by accident, and the median is the number a user feels.

The `DEADGATE_NAIVE` arm was removed in 0.1.2. It had stopped isolating what it named: the new
suppressions live in the shared path, so both arms reported identical figures and the
pre-narrowing column had quietly become a copy of the current one.

## Scope, stated plainly

This reads workflow files. It does **not** read branch-protection settings, so it reports the
*shape* of a defect, not whether a given check is actually required on your default branch.
Semantic gate testing, planting the condition a gate claims to catch and asserting it reacts,
is a separate and harder problem and is not in this release.

## Licence
MIT

## 0.1.5 removes D4

**D4 is gone, not demoted.** It scored **0 defensible findings out of 18** in a
pre-registered hand-labelled sample, and it was the largest detector by volume: **932
findings over 93 repositories, 47% of everything this tool emitted.**

0.1.3 capped it below HIGH and left it reporting. That was the wrong fix. A detector that
has never once been right is not improved by saying it quietly, and 724 LOW findings still
cost a reader attention. Removal is the honest form of a zero.

Total output across 275 repositories and 4543 files, every severity: **1986 to 1054.**
HIGH is unchanged at 125, because D4 had already stopped producing HIGH.

### A real hazard that is now undetected by design

D4 fired on a genuine failure mode: if a change-detection job FAILS rather than decides,
its outputs are unset, the condition is false, the dependent job skips, and a skipped job
reports Success. One broken path filter silently disables the tests it gates.

That shape is no longer reported, and a test asserts the silence so it stays deliberate:

```yaml
detect:                                   # if this FAILS rather than decides...
  outputs: { rust: "${{ steps.filter.outputs.rust }}" }
test:
  needs: [detect]
  if: ${{ needs.detect.outputs.rust == 'true' }}   # ...this is false, test skips, green
```

The reason it is not reported is that a workflow file cannot distinguish it from the
intended optimisation, which is the same shape, and whether the skip matters depends on
which checks are required, which no workflow states. Eighteen hand-labelled attempts at
that distinction produced nothing defensible. If a future version can read branch
protection for the gating job specifically, the question becomes answerable and the
detector can come back on evidence rather than on intuition.

Removing it also surfaced three symbols that only D4 used, including two compiled patterns
and a whole-context predicate, all now deleted.

## 0.1.4 closes two more classes, and D1 stops claiming HIGH

**Upgrade from anything earlier.** Seven times now this tool has reported correct CI as
broken. The first six were all the same mistake, asking "does this job consult its
upstreams?" and looking in too few places, each fixed by widening a pattern. The seventh
is not that, and could not be fixed that way.

| # | what was missed | found on |
|---|---|---|
| 6 | D2 fired on `always() + needs + no result read`, the signature of every reporting job | pre-registered sample |
| 7 | upstream status read through the **GitHub Actions API**, not `needs` | WordPress/gutenberg |

### Class 7, and why a wider regex cannot fix it

gutenberg's `*-status-check` jobs run a script that pages
`GET /repos/{repo}/actions/runs/{run_id}/jobs` and fails if any job concluded as anything
other than `success` or `skipped`. The word `result` never appears and `needs` is never
consulted, so **no pattern over the workflow file can find it**: the evidence lives in a
file this tool does not parse. That gate is in fact *more* robust than the idiom deadgate
hunts for, because it also fails closed when a job was never evaluated, which
`needs.*.result` cannot detect. Ten of the 54 D1 findings were this shape, all false, all
one repository.

So the claim is narrowed instead of the pattern widened. When a named gate shells out with
a token in scope, deadgate reads the script: it **suppresses** if the script consults run
status, keeps full severity if it demonstrably does not, and otherwise **demotes and says
the claim is unverified** rather than asserting something it cannot check. The token
requirement is what keeps this narrow, since reading another job's conclusion needs
credentials; without it every job running `./build.sh` would be demoted.

### Two smaller classes

- **A job that can never run.** Four findings were jobs whose own `if:` is a literal
  false. A job that never executes cannot report success on anything.
- **A gate that watches the whole chain.** A gate needing a job *and every job upstream of
  it* sees any real failure in that chain directly, so the only skip still getting through
  is a condition the author deliberately evaluated false. Note the direction: suppressing a
  job because its *parent* is covered is unsound, because `needs.*.result` reports only
  direct needs and a failed grandparent makes the parent skip rather than fail.

### D1 no longer produces a HIGH finding

D1's population was small enough to **census rather than sample**, so it was: every one of
the 54 findings hand-labelled, 29 distinct jobs. **76% false per finding, 59% per job**,
exact, no interval. Even after all three fixes above it is 65% false, with eleven of the
survivors a single reporting job.

"A required check is satisfied by a skipped job" needs a protection configuration no
workflow file carries. So D1 follows D2: MEDIUM when nothing in the workflow
consults upstream results at all, LOW when something does, and HIGH only when
`protection.py` confirms the job is a required check.

Corpus HIGH across 275 repositories and 4543 files: **354 to 125**, now entirely D3,
measured at 21-33% false per finding.

### The measurement defect, which is the part worth reading

Every precision figure published for this tool before 0.1.4, including the 40% in the
write-up, was **answering a different question than the one it was reported as answering.**

The samples were stratified with a cap of two findings per repository, to stop one
repository dominating. But deadgate emits one finding per (job, upstream) pair, and the
false positives concentrate in jobs with many dependencies: one reporting job in the
corpus emits eleven findings by itself. The cap deletes exactly that clustering.

Simulated against the censused truth, 20k draws at n=15:

| sampler | converges to | bias vs per-finding | bias vs per-job |
|---|---|---|---|
| cap 1/repo | 58.3% | -17.6 pts | -0.3 pts |
| cap 2/repo | 60.1% | -15.8 pts | +1.5 pts |
| cap 3/repo | 63.6% | -12.3 pts | +5.0 pts |
| uncapped | 74.7% | -1.2 pts | +16.1 pts |

The capped sampler is an unbiased estimator of the **per-job** rate and runs about 16
points low for the **per-finding** rate. Every published number was weighted back to
per-finding populations, so every one understated in the same direction. Two n=15 samples
of D1 estimated 60% and 33%; the truth is 76%, and the second sample's 95% interval did
not contain it.

Related, and also worth stating plainly: **179 findings were 136 distinct problems**, a
1.32x inflation. A finding count is not a problem count.

## 0.1.3 closes a fifth class, and D4 stops claiming HIGH

**Upgrade from anything earlier.** Five times now this tool has reported correct CI as
broken, each time because it asked "does this job consult its upstreams?" and looked in too
few places.

| # | what was missed | found on |
|---|---|---|
| 1 | `needs.*.result`, the WILDCARD form, matched with `[A-Za-z0-9_-]+` which cannot match `*` | Arize-ai/openinference |
| 2 | D4 judged a job alone, ignoring the workflow's own gate | Arize-ai/openinference |
| 3 | `toJSON(needs)`, every upstream read with no `result` token anywhere | astral-sh/ruff |
| 4 | PLACEMENT: a reusable-workflow call has `uses:` and no `steps:`, passing the result through JOB-level `with:` | scikit-learn |
| 5 | a gate that NAMES each upstream, `needs.a.result`, `needs.b.result`, one per job | open-gsd, omi, inbox-zero |

1 and 2 shipped in 0.1.0. 3 and 4 were found by pointing the fixed version at two more
repositories. 5 was found by hand-labelling 40 of its own surviving findings, which is the
only method here that found anything the previous method could not.

**D4 no longer produces a HIGH finding.** That sample scored D4 at **0 defensible out of
18**, and the overall false-positive rate among survivors was 40% (Wilson 95% [26%, 55%]),
statistically indistinguishable from the 40% removed in 0.1.2. D4's HIGH came from whether
the job's NAME matched test/lint/check, while the finding asserted something about branch
protection that a workflow file cannot know. It is now MEDIUM when nothing in the workflow
gates at all, LOW when something does, and HIGH only when protection data confirms the job
is a required check.

Corpus HIGH across 275 repositories and 4543 files: **658 to 354**.

## 0.1.2 closes four false-positive classes, two of them shipped

**Upgrade from 0.1.0 or 0.1.1.** Four times this tool reported correct CI as broken, each time
because it asked "does this job consult its upstreams?" and looked in too few places.

| # | what was missed | found on |
|---|---|---|
| 1 | `needs.*.result` — the WILDCARD form, matched with `[A-Za-z0-9_-]+`, which cannot match `*` | Arize-ai/openinference |
| 2 | D4 judged a job alone, ignoring the workflow's own gate | Arize-ai/openinference |
| 3 | `toJSON(needs)` — every upstream read with no `result` token anywhere; and gates written `always() && <cond>` rather than bare `always()` | astral-sh/ruff |
| 4 | PLACEMENT, not spelling: a reusable-workflow call has `uses:` and no `steps:`, passing `needs.X.result` through JOB-level `with:` | scikit-learn |

1 and 2 shipped in 0.1.0 and were fixed in 0.1.1; 3 and 4 were found afterwards by pointing the
fixed version at two more repositories. The whole job is now searched, so placement stops
mattering.

```yaml
# all four of these are a gate doing its job, and all four were reported as one that cannot
if: always()
run: if [[ "${{ contains(needs.*.result, 'failure') }}" == "true" ]]; then exit 1; fi
---
if: ${{ always() && github.ref != 'refs/heads/main' }}
env: { NEEDS_JSON: "${{ toJSON(needs) }}" }
---
uses: ./.github/workflows/report.yml
with: { job_status: "${{ needs.check-sdist.result }}" }
```

Measured on one 275-repository, 4543-file corpus, both arms reading identical bytes:
**4562 findings and 1104 HIGH before, 2917 and 658 after** — 40% of HIGH removed. On the
repositories that write their gates carefully the share is far higher: openinference went from
13 HIGH to 1, ruff from 21 to 3, and in both cases the survivors are unrelated D3 findings.

That gap is the lesson worth keeping. The tool was least accurate on the repositories with the
BEST CI, which is the worst place for a linter to cry wolf, and no corpus average would have
surfaced it. The suite passed unchanged through every one of the four, 149 of it through the
first two fixes and 161 through the second two, so not one of them was covered by anything.
There are 198 tests now, and `scripts/check_readme_test_count.py` fails if that number and the
suite ever drift apart again.
