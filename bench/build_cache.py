"""Fetch each pinned repo's workflows ONCE. Every later run reads identical bytes.

The pin is the repo LIST (pinned_repos.json), not upstream commits. A fetch today and a
fetch next month give different bytes, so the sha256 printed at the end identifies YOUR
cache. Build it once, then every detector configuration reads the same bytes.
Set DEADGATE_CACHE to put the cache somewhere other than ./wfcache.
"""
import os
import json, subprocess, pathlib, hashlib
HERE = pathlib.Path(__file__).resolve().parent
CACHE = pathlib.Path(os.environ.get("DEADGATE_CACHE", "wfcache"))
def gh(a):
    p=subprocess.run(["gh"]+a,capture_output=True,text=True)
    return p.stdout if p.returncode==0 else None
# Files per repo. The cap takes the first N BY NAME, so it is a biased subset and not a
# sample: CI and gate workflows tend to sort late, and at 12 it dropped openinference's
# python-CI.yaml and typescript-CI.yaml while keeping go- and java-. Override with
# DEADGATE_PER_REPO_CAP to measure without the bias. These three names were USED below and
# never defined, so the script this repository ships raised NameError on its first uncached
# repo and the documented reproduction command could not run at all. The published figures
# therefore came from a version that is not the one here. Found by running it.
PER_REPO_CAP = int(os.environ.get("DEADGATE_PER_REPO_CAP", "12"))

pinned=json.load(open(HERE/"pinned_repos.json"))
cache=CACHE; n_repo=n_file=0
n_dropped=0; capped=[]
manifest=[]
for r in pinned:
    full=r['repo']; slug=full.replace('/','__')
    d=cache/slug
    if d.exists(): 
        manifest.append({**r,"files":[p.name for p in d.glob('*.y*ml')]}); n_repo+=1; continue
    listing=gh(["api",f"repos/{full}/contents/.github/workflows","--jq",".[].name"])
    if not listing: continue
    found=[x for x in listing.split() if x.endswith(('.yml','.yaml'))]
    names=found[:PER_REPO_CAP]
    dropped=len(found)-len(names)
    if dropped: n_dropped+=dropped; capped.append(f"{full} (+{dropped})")
    if not names: continue
    d.mkdir(parents=True, exist_ok=True)
    got=[]
    for nm in names:
        raw=gh(["api",f"repos/{full}/contents/.github/workflows/{nm}","-H","Accept: application/vnd.github.raw"])
        if raw is None: continue
        (d/nm).write_text(raw); got.append(nm); n_file+=1
    if got: manifest.append({**r,"files":got}); n_repo+=1
    if n_repo % 25 == 0: print(f"  cached {n_repo} repos, {n_file} files", flush=True)
json.dump(manifest, open(CACHE.parent/"cache_manifest.json","w"), indent=1)
h=hashlib.sha256()
for p in sorted(cache.rglob('*.y*ml')): h.update(p.read_bytes())
print(f"DONE {n_repo} repos, {n_file} files. corpus sha256={h.hexdigest()[:16]}")
# Say what was left out. A cap that prints nothing reads as "everything was covered", and
# this one measurably inflated the count of required checks that matched no job.
if n_dropped:
    print(f"CAPPED at {PER_REPO_CAP} files/repo: {n_dropped} workflow file(s) NOT fetched "
          f"across {len(capped)} repo(s). Attribution is measured against a subset.")
    print("  " + ", ".join(capped[:6]) + (" ..." if len(capped) > 6 else ""))
