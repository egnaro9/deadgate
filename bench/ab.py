"""Run the detectors over the cached corpus. Identical bytes for every configuration.

  python bench/build_cache.py          # once; needs `gh` authenticated
  python bench/ab.py                   # current detectors
  DEADGATE_NAIVE=1 python bench/ab.py  # pre-narrowing behaviour

Set DEADGATE_CACHE if the cache is not ./wfcache. The sha256 printed identifies the bytes
the run actually read, so two arms can be compared only when it matches.
"""
import collections, hashlib, json, os, pathlib, statistics, sys

import yaml

from deadgate.detectors import NAIVE, active_detectors, scan_workflow

CACHE = pathlib.Path(os.environ.get("DEADGATE_CACHE", "wfcache"))
if not CACHE.is_dir():
    sys.exit(f"no corpus at {CACHE}. Run bench/build_cache.py first, or set DEADGATE_CACHE.")

digest = hashlib.sha256()
per_repo, totals, severities, high = {}, collections.Counter(), collections.Counter(), collections.Counter()
n_repo = n_file = n_hit = 0
high_repos = set()

for repo_dir in sorted(d for d in CACHE.iterdir() if d.is_dir()):
    files = sorted(repo_dir.glob("*.y*ml"))
    if not files:
        continue
    n_repo += 1
    counts = collections.Counter()
    for path in files:
        raw = path.read_bytes()
        digest.update(raw)
        n_file += 1
        try:
            doc = yaml.safe_load(raw.decode())
        except Exception:
            continue
        for finding in scan_workflow(doc):
            counts[finding.detector] += 1
            severities[finding.severity] += 1
            if finding.severity == "HIGH":
                high[finding.detector] += 1
                high_repos.add(repo_dir.name)
    if sum(counts.values()):
        n_hit += 1
    totals.update(counts)
    per_repo[repo_dir.name] = dict(counts)

mode = "NAIVE" if NAIVE else "CURRENT"
pct = lambda k: 100 * k / max(n_repo, 1)
print(f"{mode}  detectors={[f.__name__ for f in active_detectors()]}")
print(f"  {n_repo} repos, {n_file} files, corpus sha256={digest.hexdigest()[:16]}")
print(f"  {sum(totals.values())} findings, {n_hit} repos with >=1 ({pct(n_hit):.0f}%)  {dict(totals)}")
print(f"  severity={dict(severities)}")
print(f"  HIGH: {sum(high.values())} across {len(high_repos)} repos ({pct(len(high_repos)):.0f}%)  {dict(high)}")

# Per-repo distribution, because a corpus total is weighted by workflow size and one
# monorepo can carry the majority of it. See MEASUREMENT.md.
for det in sorted(totals):
    vals = sorted(n for c in per_repo.values() if (n := c.get(det, 0)))
    if not vals:
        continue
    p90 = vals[min(int(0.9 * len(vals)), len(vals) - 1)]
    print(f"  {det} per affected repo: n={len(vals)} median={statistics.median(vals):.0f} "
          f"p90={p90} max={vals[-1]}")

json.dump(per_repo, open(f"ab_{mode.lower()}.json", "w"), indent=1)
