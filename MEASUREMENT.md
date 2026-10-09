# What the numbers mean, and which unit they are in

Every figure here comes from one corpus: 207 public repositories, 1668 workflow files,
`sha256 d3c2d8b4fb2c6e44`.

```bash
python bench/build_cache.py            # once; needs `gh` authenticated
python bench/ab.py                     # current detectors
```

Two things about that pin, stated before the numbers that depend on it.

The repo *list* is pinned, in `bench/pinned_repos.json`, because `gh search repos --sort
updated` returns a different set on every call. The first attempt at this measurement compared
two different populations without noticing.

Upstream *commits* are not pinned. A fetch today and a fetch next month give different bytes,
so the sha256 above identifies the cache this run read, not a permanent corpus. Build the cache
once and every detector configuration reads identical bytes; compare two arms only when
`bench/ab.py` prints the same sha for both.

## Headline

Corpus rebuilt 2026-10-07 with NO per-repository cap: **275 repositories, 4543 workflow files,
`sha256 021da3340e12b608`.**
The figures published before that date were from a cache that no longer exists AND could not be
rebuilt, because `bench/build_cache.py` raised `NameError` on three names it used and never
defined. The documented reproduction command had therefore never run in the form this
repository shipped, so those numbers came from a script that is not the one here. They are
withdrawn rather than carried forward; see "The detectors were blind to the documented idiom".

```
CURRENT  2560 findings  172/275 repos (63%)  {D4: 1058, D3: 1061, D1: 414, D2: 27}
HIGH      354 findings   88/275 repos (32%)  {D3: 273, D2: 27, D1: 54}
severity  {LOW: 1686, HIGH: 354, MEDIUM: 520}

The corpus is NOT comparable to the 207-repository one quoted previously: the pinned list holds
278 repositories and the earlier run reached 207 of them, so population and commits both moved.
Only same-cache arms are compared below.

### The cap drops files alphabetically, which is not a random subset

`bench/build_cache.py` caps at 12 files per repository and now says what it left out: **2064
files across 80 repositories were not fetched.** The cap takes the first 12 by name, so it is
biased, not sampled. Measured consequence: of Arize-ai/openinference's four affected CI
workflows, `go-CI.yaml` and `java-CI.yaml` are cached while `python-CI.yaml` and
`typescript-CI.yaml` are dropped, because "p" and "t" sort after "g" and "j". Every finding
count here is a floor for repositories with many workflows, and CI files tend to sort late.

## 0.1.3: a sixth class, and D4 stops claiming HIGH

**The fifth false-positive class.** A gate can consult every upstream by NAMING each one,
`needs.a.result`, `needs.b.result`, one expression per job. That is the wildcard written
longhand and it was invisible, so the jobs those gates cover were still reported. Found by
hand-labelling a sample of this tool's own survivors: 6 of 16 false positives were this,
across scikit-learn, open-gsd, BasedHardware/omi and elie222/inbox-zero. Coverage is now
per-name and NOT all-or-nothing: a gate naming eight of its nine needs covers eight.

**D4 no longer reaches HIGH.** It used to inherit a severity decided by whether the job's
NAME matched test/lint/check/verify/ci. The finding it labelled asserts "any branch
protection requiring it passes with nothing tested", and whether a job is required is the
one thing a workflow file cannot say.

This was measured, not argued. A pre-registered, hand-labelled sample of 40 of 0.1.2's
surviving HIGH findings (n=40, stratified by repo, cap 2 per repo, seed 20261009, labels
fixed before drawing) scored:

| detector | n | defensible | false | arguable |
|---|---|---|---|---|
| D3 | 17 | 7 | 8 | 2 |
| D4 | 18 | **0** | 8 | 10 |
| D2 | 4 | 0 | 0 | 4 |
| D1 | 1 | 1 | 0 | 0 |

Overall false-positive rate 40%, Wilson 95% [26%, 55%], which is indistinguishable from the
40% removed in 0.1.2. Every arguable D4 failed on the same point: a failed gating job is
itself red on the pull request, so the stated impact needs a configuration the detector
cannot see.

D4 severity now: HIGH only via the protection escalation path; MEDIUM when NO job in the
workflow consults upstream results at all, which was 126 of the 196; LOW when the workflow
does gate but not over this upstream, weaker on purpose because 55% of real gates hand the
decision to a script this tool cannot read. The `repro` text no longer asserts the branch
protection claim, it states the condition and says the file cannot settle it.

## The detectors were blind to the documented idiom

GitHub's documented way to ask "did anything upstream fail" is the WILDCARD form,
`needs.*.result`. Three detectors tested for it with `needs\.[A-Za-z0-9_\-]+\.(result|outputs)`,
which cannot match `*`. The finding's own text said "never reads needs.*.result" while failing
to match that exact string, so a correctly written fan-in gate was reported as a gate that
cannot fail. D4 had a second form of the same error: it judged each job alone and ignored
whether the workflow's own gate already caught the failure it described.

Both arms below read the same bytes, `sha256 021da3340e12b608`. Each revert was confirmed by a
BEHAVIOUR PROBE before measuring, because three A/B runs during this work silently no-opped
their own revert and printed two identical arms as if they were a comparison:

| arm | findings | HIGH |
|---|---|---|
| 0.1.1, as published | 4562 | 1104 |
| 0.1.2 | 2917 | 658 |
| removed | 1645 | **446 (40% of HIGH)** |

On the earlier CAPPED corpus the same comparison read 34%. The cap was hiding part of the
defect, which is what a biased subset does: it dropped the late-alphabet CI files, and those
are where gates live.

Per detector: D1 1315 to 414, D2 66 to 27, D4 2120 to 1415. D3 is untouched at 1061 and does
not read upstream state at all.

**The 9.1% average hides where it lands.** Only 10 of 275 repositories use the wildcard idiom
at all, so those 43 false HIGH concentrate on them. Measured on Arize-ai/openinference (1.2k
stars, 22 workflows), whose go/java/python/typescript `ci-required` jobs each read it twice,
counting every severity with `--all`:

| arm | findings | HIGH |
|---|---|---|
| before the fix | 25 | 13  {D1 5, D2 4, D4 3, D3 1} |
| after the fix | 11 | 1  {D3 1} |

**Twelve of the thirteen HIGH were false.** The survivor is the one real finding, a D3: a
`uvx ... \| grep \| awk \| jq` pipeline with no pipefail, so the step's status is jq's. Its
impact is small because a later step fails the job when the resulting list is empty, which the
detector cannot see; D3 does not read upstream state and neither fix touches it.

The tool was most wrong about the repository with the most carefully written gates, which is the
worst place for a linter to cry wolf, and it is why a corpus average was not enough to notice:
9.1% across the corpus, 92% on this one repository.

All 149 tests passed before and after both fixes, so neither defect was covered. The suite is
156 now. One of the fixes was itself wrong and was caught the same way: the gate-coverage check
first walked the needs graph transitively, but `needs.*.result` reports only DIRECT needs, and a
failed grandparent makes the parent SKIP, which is not a failure. Transitive suppression would
have hidden real findings. The mutation that deleted the walk SURVIVED, which is what exposed
it.

## The corpus total is weighted, and the median is what a user feels

**3513 of 4543 files produce no finding at all** (77%). The findings land on the other 1030,
and unevenly: across the 172 affected repositories the median is 6 and the p90 is 43, against
a maximum of 190. Quoting a corpus total alone says more about the largest repository in the
corpus than about the tool, so `bench/ab.py` prints the per-repo median, p90 and max beside
every total and the unit cannot be dropped by accident.

```
  49  LanternOps/breeze/ci.yml
  42  Expensify/App/deploy.yml
  38  openclaw/openclaw/ci.yml
```

An earlier version of this file said three files carried 68% of the total and one carried 55%
alone. That was measured on the withdrawn 1668-file corpus, against the naive D1 arm that no
longer exists, and it is not true here: the largest single file carries 2% and the top three
carry 4%. The heavy tail is real, the single dominating file is not, and the claim is
withdrawn rather than restated with new numbers behind the old sentence.

## What the branch-protection tier actually changed

Run over the same 207 cached repositories, resolving every finding against what each default
branch requires. Read-only, retried past rate limiting, behind the transport self-test:

```
protection states   PROTECTED 56   UNREADABLE 151   complete 0/207
finding verdicts    REQUIRED 63    AMBIGUOUS 844    UNREADABLE 711
severity before     HIGH 417   MEDIUM 331   LOW 870
severity after      HIGH 428   MEDIUM 320   LOW 870
moved               MEDIUM -> HIGH 11, across 7 repositories. Nothing was downgraded.
```

`complete 0/207` is the headline, not a footnote. On repositories nobody here administers the
required set can never be shown complete, so the tier may only escalate. Zero downgrades is the
design holding, not a disappointing result: every one of those 320 remaining MEDIUMs is a case
where the honest answer is "this evidence cannot settle it".

11 of 331 is a 3% move. The tier earns its place by being sound rather than by being loud, and
its real use is the case this corpus cannot represent: a repository you administer, where both
endpoints answer and findings can also be cleared.

### The attribution warning fired, and pointed at the corpus

109 required contexts were attributed to a job; 126 were not, and in 21 repositories nothing
matched at all. That is the instrument check, and reading it found two separate causes:

Most unattributed contexts are not workflow jobs. ClickHouse requires `CH Inc sync` and
`Mergeable Check`; NVIDIA/TensorRT-LLM requires `DCO` and `blossom-ci`. Those are apps and
external systems, and not matching them is correct.

The rest are a defect in this corpus, not in the matcher. `bench/build_cache.py` caps at 12
workflow files per repository, alphabetically, so Comfy-Org/ComfyUI's `ruff.yml` and
`test-*.yml` are simply absent while its required `Run Ruff` and `test` contexts are present.
Homebrew/homebrew-core loses `tap_syntax` the same way. The cap used to print nothing, which
read as full coverage; it now reports how many files it did not fetch.

### Fifteen fail-opens, and what a corpus could not see

An adversarial review of this tier, four independent lenses with every finding verified by
reproduction, claimed 19 defects and confirmed 15. The previous version of this section said
seven. That number was written while the review was still running and is superseded; it was
the count in hand, reported as though it were the count.

All 15 are fixed, each pinned by a test proved to fail by planting the defect back. The ones
that mattered came from live check-run names and required-check sets, not from reading code:

- **A static `name:` with a matrix IS suffixed with the leg values.** The module documented and
  enforced the opposite, that the name is used once verbatim, so every leg of such a job matched
  nothing. promptfoo's `main` branch requires the context `Check Python (3.9)` while its
  workflow declares a static `name: Check Python`. The complementary rule holds and is why this
  survived: `Build on Node ${{ matrix.node }}`, whose name already references the matrix, is
  required as the unsuffixed `Build on Node 24.x`.
- **A workflow that declares `on: workflow_call` cannot name its own checks.** When another
  workflow `uses:` it, GitHub names the check `<caller job> / <this job>`. Deriving the
  unprefixed name cleared findings on jobs that were required merge gates, a two-step error in
  the clearing direction since the truth was REQUIRED. Worse, the attribution check could not
  see it: the caller's own prefix credited those contexts, so the run printed
  `required checks: 2/2 attributed` while the finding was hidden. Such jobs are now AMBIGUOUS.
- a matrixed reusable call is named `caller (values) / callee`, and `uses:` was checked before
  the matrix, so none of them matched
- a job SKIPPED by its job-level `if:` emits one check run carrying the RAW `name:` template,
  expressions left literal. Confirmed live on two repositories
- YAML booleans rendered `True`/`False` where GitHub renders `true`/`false`
- GitHub trims the rendered check name; the expansion did not
- `include`/`exclude` legs were absent while confidence stayed EXACT
- completeness was granted without confirming the branch exists whenever any check was
  required, and rulesets can return pattern-matched organisation rules for a branch that is
  not there, so a typo in `--branch` could still reach NOT_REQUIRED
- a classic 200 whose body is null, a list or a number counted as a successful read
- 429, GitHub's secondary rate limit, was classified as a permission boundary
- four guards had no test at all, so deleting them changed nothing that was measured

### What the fixes did to the numbers

```
                     before fixes      after fixes
REQUIRED verdicts              63               34
AMBIGUOUS                     844              873
contexts attributed           109              106
repos with zero matches        21               24
MEDIUM -> HIGH                 11               11
```

The escalations are unchanged and everything else moved toward caution, which is the shape a
soundness fix should have. Half the REQUIRED verdicts were being claimed on derivations that
could not support them.

The first round of fixes, before the callee defect was known, produced numbers **identical** to
the unfixed run. That is the part worth keeping. A corpus of workflow files cannot see any of
this, because the evidence that exposes it is what GitHub actually named the check runs. A
measurement can be stable, reproducible, hash-pinned, and still be measuring past the bug.

Residual: `gh api` does not paginate, so `per_page=100` covers rulesets but a branch with more
than 100 rules would truncate.

## What a corpus measurement cannot tell you

Hit rate is not precision. These are counts of a *shape* in a workflow file. Whether a given
finding would have let a real regression through depends on branch protection, which this
release does not read. A hand-traced sample of 20 D3 findings is in the commit history; no
population precision is claimed, because none has been measured.
