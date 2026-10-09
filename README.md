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
| D3 | a `run:` step ends a pipeline in a filter with no `pipefail` | the step's status is the filter's, so an upstream failure passes |

**Two detectors have been removed rather than tuned**, each on hand-labelled evidence
that it was never right: D4 on 0 true of 18 sampled (0.1.5), and D2 on 0 true of 27
censused (0.1.6). The shapes they fired on are described in those sections, including
the one real hazard that is now undetected by design.

Measured precision, so the table above is not the only claim: D1 is **65% false per
finding** on a complete 54-finding census and therefore never reaches HIGH on its own;
D3's HIGH is **30.9% false** on a complete hand-labelled census of that stratum (94
findings), down from 51.2% before the 0.1.7 narrowing. It is the only detector that produces HIGH.

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

## 0.1.9 fixes a substitution scanner that was blind to shell quoting

Found by reading the segments 0.1.8 began reporting, not by a test. Several were mangled,
and a mangled segment means the reported filter and the head-can-fail judgement were both
computed on corrupted text. Three defects in one scanner, all from counting parens without
parsing:

| | shape | what it produced |
|---|---|---|
| quotes | `$(grep -o 'X\(A\|B\)' f \| cut ...)` | filter reported as `PATCH\` |
| fresh context | `"$(sed 's#^(a)#\1#p' f \| sort)"` | span closed at `+)` |
| process substitution | `$(comm <(a) <(b \| jq))` | everything after the first `<(...)` lost |

Inside `$( ... )` the body is parsed as new shell code, so a single quote quotes again even
when the substitution sits inside double quotes. The scanner now tracks single quotes,
double quotes, backslash escapes and `<( )`, and saves and restores the quote state across
a substitution boundary.

### D3's HIGH stratum is fully hand-labelled again

**94 findings, 65 true, 29 false, 30.9% false**, with no unlabelled residue. Across the
sequence: 51.2% before any narrowing, 35.1% after 0.1.7 against partial labels, 30.9% now
against complete ones.

Of the 17 that needed new labels because the reported segment moved, 11 are true. They are
the same defects named more precisely: `sha256sum f | cut -d' ' -f1` rather than the
`echo ... >> "$GITHUB_OUTPUT"` wrapped around it, and four `for x in $(find ... | sort)`
loops whose empty list means a check runs over nothing and passes.

### One thing deliberately left alone

Severity is judged on the **line** while the finding names the **segment**. That looks like
an inconsistency and is not: the segment is where the status is masked, the enclosing line
is how the bad value escapes. Judging severity on the segment alone was tried, and HIGH
fell from 94 to 30 with six tests red, because `sha256sum f | cut -d' ' -f1` on its own
shows no assignment and no export.

## 0.1.8 fixes two bugs the census found in D3 itself

Both were found by hand-labelling D3's HIGH findings, not by a test.

### The wrong pipeline was being reported

A line can hold more than one pipeline at different nesting depths, and they are not
equally guilty:

```bash
echo "batch=$(grep -Po '...' list.txt | python3 -c '...')" | tee "$GITHUB_OUTPUT"
```

The outer pipeline is `echo ... | tee`, whose head cannot fail. The inner one is
`grep | python3`, whose head can. D3 split the whole line on its **last** pipe, so it only
ever saw the outer one, and `_upstream_can_fail` then waved it through on the grounds that
"the outer head holds a substitution we already judged benign" -- which it had not, because
that loop returns False only for *benign* substitutions and falls through for every other
kind. Any head containing any substitution was assumed able to fail.

Each pipeline is now judged against its own head, innermost first. That removes the four
head-cannot-fail false positives in the census and, where a finding survives, names the
pipeline that is actually masking something: `sha256sum f | cut -d' ' -f1` rather than the
`echo ... >> "$GITHUB_OUTPUT"` wrapped around it.

### The `break` was not the second bug

One finding per step was blamed for hiding a real inner pipeline behind a benign outer
one. That was the first bug choosing the outer. With candidates ordered innermost-first,
the first qualifying pipeline is the guilty one and stopping there loses nothing.

Removing it anyway was tried and measured: **D3 HIGH went 94 to 125**, about 31 additional
pipelines inside steps that already reported one, none of them in the hand-labelled census.
Shipping a 33% increase in the loudest tier at unknown precision is the move this whole
series exists to avoid, so the break stays.

| | before | after |
|---|---|---|
| D3 HIGH | 94 | **95** |
| head-cannot-fail false positives | 4 | **0** |
| false among findings matchable to a census label | 35.1% | **30.1%** |

22 of the 95 no longer match a census label, because the finding now points at a different
line in the same job: the inner pipeline instead of its wrapper. Those are the same defects
reported more precisely, not new ones, but they are **not** independently labelled and the
overall rate is not re-established by this release.

## 0.1.7 narrows D3, on a complete census of its HIGH findings

**All 125 D3 HIGH findings were hand-labelled. 51.2% were false.** The pre-registered rule
said 50-80% means narrow, so D3 is narrowed rather than removed, and HIGH was not
defensible until it was.

The narrowing asks one question the old severity logic never asked: **can the masked exit
status reach anything at all?**

`_d3_severity` decided HIGH from `exit 1` or `GITHUB_OUTPUT` appearing *anywhere in the
step*, and from the captured variable being dereferenced *anywhere in it*. Both are too
coarse. One `exit 1` at the bottom of a step promoted every log-extraction pipeline above
it, and "dereferenced" counted uses that fail loudly on an empty value.

Two predicates now gate HIGH:

- **The flagged line must consume its own output**: a redirect, `tee`, `$GITHUB_OUTPUT`,
  a command substitution, or an assignment. `ls -la "$DIR" | head -10` reaches nothing,
  whatever else the step does.
- **If the line captures into a variable, some use of that variable must be able to
  silently accept an empty value.** Never dereferenced, only printed, `${var:-default}`,
  and a bare `test x = y` statement are all safe.

| | before | after |
|---|---|---|
| D3 HIGH on the corpus | 125 | **94** |
| false among them | 51.2% | **35.1%** |
| true positives lost | | **0** |

### Bash semantics, established by running bash

A bare `test "$x" = "$y"` that fails exits the step under `set -e`, so an empty captured
value surfaces. **`if [ "$x" -gt 100 ]` does not**: `set -e` is suspended inside an
if-condition, so an empty value makes `[` print "integer expression expected", the else
branch is taken, and the check passes silently with exit 0. The first census pass labelled
that shape false by reasoning that `-e` would catch it, and it cost two labels.

### Three versions of this rule were discarded before one shipped

Each was evaluated against the 125 labels before being written into the detector, and each
of the first three cost true positives:

1. "Only printed" matched `echo "digest=${d}" >> "$GITHUB_OUTPUT"`, the single most
   important true shape in the corpus. It would have suppressed 11 true findings.
2. "Output goes nowhere" missed `export V=$(...)` and `for f in $(...)` as consumption,
   losing 2.
3. The assignment pattern matched an inline **environment prefix**: in
   `WINEDEBUG=-all timeout 300 make check | tee x.log` it read `WINEDEBUG` as the captured
   variable, found it unused, and suppressed a masked test failure. This one survived the
   simulation and was caught only by running the real detector against the labels, because
   the simulation applied the assignment check to fewer lines than the detector does.

All three now have regression tests and are killed by mutation.

### What is still not measured

D3's MEDIUM (152) and LOW (408) are unmeasured. They are 560 of its 654 findings.

## 0.1.6 removes D2, on a census

**D2 is gone. Every one of its 27 findings was hand-labelled: 0 true, 27 false, 0
arguable.**

Not a sample this time. 27 findings over 27 jobs and 14 repositories is the entire
population on the 275-repo corpus, so there is no interval and no sampling design to argue
about. That matters, because the sampling design is what corrupted the earlier numbers in
this project (see 0.1.4).

D2 fired on `always()` + `needs` + no result read. That is the signature of a correctly
written reporting job, and the census says so without exception: report x3,
summary/summarize x5, cost x7, merge-reports x2, aggregate_reports, e2e-log-summary,
accessibility-report, audit-high-report, ci-timings, accept, publish, release_lease x2, a
cleanup job restoring an environment policy, and one change-detection job.

Three were read in full rather than judged by their names, because their names suggested a
gate. All three handle upstream failure deliberately:

| job | what it actually does |
|---|---|
| `KiroCrew accept` | proposes a baseline by PR; exits 0 with "No reports to accept." |
| `trycua publish` | regenerates a support matrix and opens a ledger PR |
| `hermes-agent ci-timings` | "Degraded runs produce no ci-timings.json, skip rather than fail" |

### The structural argument, which is stronger than the rate

D2 reached MEDIUM only when `_GATE_NAME` matched the job. **All 27 findings were LOW, so
zero matched.** By its own severity logic, D2 never once found a job it believed was a
gate, across 275 repositories and 4543 files, while the text of every finding it emitted
said the branch protection that job provides is decorative.

That is not a precision problem a narrowing fixes. The detector's own gate test disagreed
with its own finding text, every single time.

D4 was removed on 0 true of 18 **sampled**. D2 goes on 0 true of 27 **censused**.

Total output across the corpus: **1054 to 1027.** HIGH is unchanged at 125, all D3.

### Also removed: dead code that predates this release

`_steps_text` was defined and never called, and had been dead since `_job_blob` superseded
it. The zero-reference scan used when D4 was removed missed it, because that scan counted
textual mentions and this function is named in two comments. A call-graph check finds it.
Both comments now name the function that actually does the work.

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
There are 218 tests now, and `scripts/check_readme_test_count.py` fails if that number and the
suite ever drift apart again.
