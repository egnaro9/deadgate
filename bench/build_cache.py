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
pinned=json.load(open(HERE/"pinned_repos.json"))
cache=CACHE; n_repo=n_file=0
manifest=[]
for r in pinned:
    full=r['repo']; slug=full.replace('/','__')
    d=cache/slug
    if d.exists(): 
        manifest.append({**r,"files":[p.name for p in d.glob('*.y*ml')]}); n_repo+=1; continue
    listing=gh(["api",f"repos/{full}/contents/.github/workflows","--jq",".[].name"])
    if not listing: continue
    names=[x for x in listing.split() if x.endswith(('.yml','.yaml'))][:12]
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
