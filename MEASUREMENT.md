# What the numbers mean, and which unit they are in

Every figure here comes from one corpus: 207 public repositories, 1668 workflow files,
`sha256 d3c2d8b4fb2c6e44`.

```bash
python bench/build_cache.py            # once; needs `gh` authenticated
python bench/ab.py                     # current detectors
DEADGATE_NAIVE=1 python bench/ab.py    # pre-narrowing behaviour
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

```
CURRENT  1618 findings  139/207 repos (67%)  {D4 751, D1 462, D3 384, D2 21}
NAIVE    2170 findings  143/207 repos (69%)  {D1 1668, D3 481, D2 21}
HIGH      417 findings   79/207 repos (38%)  {D4 188, D3 109, D1 99, D2 21}
```

## The corpus total is weighted by workflow size

A finding count summed across a corpus is weighted by how large each workflow is. Three files
carry 68% of the naive D1 total and one carries 55% of it alone, while 1532 of the 1668 files
produce no D1 finding at all:

```
921  ClickHouse/master.yml                        (163 jobs)
141  ClickHouse/backport_branches.yml             ( 40 jobs)
 80  liferay-portal/ci-publish-cloud-...yaml      ( 63 jobs)
```

The same narrowing, measured four ways:

| unit | naive | current | change |
|---|---|---|---|
| corpus total | 1668 | 462 | -72% |
| corpus total excluding ClickHouse | 587 | 256 | -56% |
| median affected repo | 3 | 2 | -1 finding |
| p90 affected repo | 19 | 14 | -26% |
| repos with >=1 D1 | 75 | 53 | -29% |

All four are arithmetically correct. `-72%` is the one that flatters the fix, and it is a
statement about ClickHouse's release pipeline more than about the detector. The median is the
number a user feels, because they run this on one repository: two findings instead of three.

D4 skews the same way, 751 findings but a median of 5 per affected repo against a max of 206,
so this is a property of the corpus and not of one detector. `bench/ab.py` therefore prints the
per-repo median, p90 and max for every detector on every run. Quoting a corpus total alone is
not available by default.

Quote the unit with the number. A percentage whose denominator was never chosen deliberately is
the same defect this tool exists to find: a field that looks like a judgment and is really a
default.

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
