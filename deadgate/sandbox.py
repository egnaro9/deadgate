"""Execute a RECONSTRUCTED predicate against synthesized inputs.

SECURITY BOUNDARY, stated first because it is the whole design:

    Shell text taken from a stranger's repository is NEVER executed. It is parsed, and
    the parse yields a small set of validated parameters (a variable name, an integer, a
    file path, a literal). This module then builds its OWN shell from those parameters and
    runs that. Repository content reaches the shell only as DATA: an environment value or
    a file body, never as a command.

    The cost of that choice is stated in the verdict: a reconstruction is a MODEL of the
    gate, not the gate. If the real step had extra conditions around the predicate, the
    model is narrower than the original. That is why `reconstructed` is carried on every
    result rather than hidden.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from .predicates import Predicate

_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INT = re.compile(r"^-?\d+$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")

TIMEOUT_S = 5

# Verdicts, taken from gate mutation sweeping. Both directions are required.
FIRED = "FIRED"              # rejected the plant AND accepted the near miss
DEAD = "DEAD"                # accepted the plant. The defect this tool exists to find.
LEAKY = "LEAKY"              # rejected the near miss. A false alarm, which gets a gate muted.
INCONCLUSIVE = "INCONCLUSIVE"  # could not be exercised. Never folded into success.


@dataclass(frozen=True)
class Result:
    predicate: Predicate
    verdict: str
    reconstructed: str
    plant_exit: int | None
    near_exit: int | None
    detail: str


def _safe_var(v: str) -> str | None:
    v = v.lstrip("$")
    return v if _VAR.match(v) else None


def _run(script: str, env: dict[str, str], cwd: str) -> int | None:
    """Run OUR script with a minimal environment, no network inheritance, and a timeout."""
    base = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "HOME": cwd, "TMPDIR": cwd}
    base.update(env)
    try:
        p = subprocess.run(["/bin/sh", "-c", script], cwd=cwd, env=base,
                           capture_output=True, timeout=TIMEOUT_S, text=True)
        return p.returncode
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def reconstruct(p: Predicate) -> tuple[str, str] | None:
    """Return (script, input_mode) built from validated parameters, or None if not modellable.

    input_mode is 'env' (the plant is a variable value) or 'file' (the plant is file content).
    """
    if p.kind == "NUM_THRESHOLD":
        v = _safe_var(p.reads)
        m = re.search(r"(-ge|-gt|-le|-lt|-eq|-ne)\s+(\d+)", p.note) or \
            re.search(r"(-ge|-gt|-le|-lt|-eq|-ne)\s+\"?(\d+)", p.source)
        if not (v and m):
            return None
        return (f'[ "${v}" {m.group(1)} {int(m.group(2))} ]', "env")
    if p.kind == "STR_EQUAL":
        v = _safe_var(p.reads)
        m = re.search(r"\$\w+\s+(==?|!=)\s+(\S+)", p.note)
        if not (v and m):
            return None
        op = "=" if m.group(1) in ("=", "==") else "!="
        return (f'[ "${v}" {op} "{m.group(2)}" ]', "env")
    if p.kind in ("STR_EMPTY", "STR_NONEMPTY"):
        v = _safe_var(p.reads)
        if not v:
            return None
        flag = "-z" if p.kind == "STR_EMPTY" else "-n"
        return (f'[ {flag} "${v}" ]', "env")
    if p.kind in ("FILE_NONEMPTY", "FILE_EXISTS"):
        if not _SAFE_NAME.match(p.reads):
            return None
        flag = "-s" if p.kind == "FILE_NONEMPTY" else "-f"
        return (f'[ {flag} "./target" ]', "file")
    if p.kind == "GREP_PRESENT":
        m = re.search(r"occurrence of '(.+)'>$", p.plant)
        if not m:
            return None
        pat = m.group(1).replace("'", "")
        return (f"grep -q -- '{pat}' ./target", "file")
    return None


def _materialise(mode: str, value: str, cwd: str) -> dict[str, str]:
    """Turn a plant or near-miss description into a real input. Returns the env to use."""
    if mode == "env":
        return {"GATEVAR": value}
    body = "" if "empty" in value or "no occurrence" in value else "x"
    m = re.search(r"containing '(.+)'>$", value)
    if m:
        body = f"prefix {m.group(1)} suffix\n"
    elif "absent" in value:
        p = os.path.join(cwd, "target")
        if os.path.exists(p):
            os.remove(p)
        return {}
    with open(os.path.join(cwd, "target"), "w") as fh:
        fh.write(body)
    return {}


def exercise(p: Predicate) -> Result:
    """Plant the condition the gate claims to catch, then the near miss. Classify."""
    recon = reconstruct(p)
    if recon is None:
        return Result(p, INCONCLUSIVE, "", None, None,
                      "no safe reconstruction for this predicate shape")
    script, mode = recon
    script = script.replace(f'"${_safe_var(p.reads) or "X"}"', '"$GATEVAR"') if mode == "env" else script

    cwd = tempfile.mkdtemp(prefix="deadgate-")
    try:
        env = _materialise(mode, p.plant, cwd)
        plant_exit = _run(script, env, cwd)
        env = _materialise(mode, p.near_miss, cwd)
        near_exit = _run(script, env, cwd)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)

    if plant_exit is None or near_exit is None:
        return Result(p, INCONCLUSIVE, script, plant_exit, near_exit, "predicate timed out")
    if plant_exit == 0:
        return Result(p, DEAD, script, plant_exit, near_exit,
                      f"the gate ACCEPTED {p.plant!r}, the condition it claims to catch")
    if near_exit != 0:
        return Result(p, LEAKY, script, plant_exit, near_exit,
                      f"the gate REJECTED the near miss {p.near_miss!r}, so it over-fires")
    return Result(p, FIRED, script, plant_exit, near_exit,
                  f"rejected {p.plant!r}, accepted {p.near_miss!r}")
