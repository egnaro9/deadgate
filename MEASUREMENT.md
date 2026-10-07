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

## What a corpus measurement cannot tell you

Hit rate is not precision. These are counts of a *shape* in a workflow file. Whether a given
finding would have let a real regression through depends on branch protection, which this
release does not read. A hand-traced sample of 20 D3 findings is in the commit history; no
population precision is claimed, because none has been measured.
