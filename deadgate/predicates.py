"""Extract gate PREDICATES from CI shell, and derive how to falsify each one.

The method is taken from mutation testing of gates: do not run the gate whole, extract
the predicate and exercise it directly. For each predicate we derive two inputs:

    plant      the condition the gate CLAIMS to catch. The gate must reject it.
    near_miss  the closest input it must still ACCEPT.

Both directions are required. A gate that rejects everything is as useless as one that
rejects nothing, and only the near miss separates the two.

NOTHING HERE EXECUTES ANYTHING. This module decides WHAT to run; running it is a separate
concern with a separate security boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Predicate:
    kind: str
    source: str            # the shell fragment this came from
    reads: str             # the file, variable or command output it depends on
    plant: str             # input that MUST be rejected
    near_miss: str         # closest input that MUST be accepted
    note: str = ""
    boundary: bool = False  # True when plant/near_miss differ by the smallest possible step


_RULES: list[tuple[str, re.Pattern]] = [
    ("FILE_NONEMPTY", re.compile(r"(?:test|\[\[?)\s+-s\s+([^\s\]]+)")),
    ("FILE_EXISTS",   re.compile(r"(?:test|\[\[?)\s+-[fe]\s+([^\s\]]+)")),
    ("STR_EMPTY",     re.compile(r"(?:test|\[\[?)\s+-z\s+\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)")),
    ("STR_NONEMPTY",  re.compile(r"(?:test|\[\[?)\s+-n\s+\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)")),
    # A quoted pattern may contain spaces; an unquoted one may not. Two alternatives,
    # because a single character class cannot express both and silently dropped
    # `grep -q "BUILD OK" build.log`.
    ("GREP_PRESENT",  re.compile(
        r"grep\s+(?:-[a-zA-Z]+\s+)*-q[a-zA-Z]*\s+"
        r"(?:'([^']+)'|\"([^\"]+)\"|([^\s|;&]+))"
        r"\s+([^\s|;&]+)")),
    ("JQ_PREDICATE",  re.compile(r"jq\s+(?:-[a-zA-Z]+\s+)*-e[a-zA-Z]*\s+'([^']+)'")),
]

_NUM = re.compile(r"\[\[?\s+\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\"?\s+(-ge|-gt|-le|-lt|-eq|-ne)\s+\"?(\d+)\"?")
_STREQ = re.compile(r"\[\[?\s+\"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\"?\s+(==?|!=)\s+\"?([^\"\]\s]+)\"?")


def _numeric(var: str, op: str, n: int, frag: str) -> Predicate:
    """Threshold gates are where the real defects hide, so mutate at the BOUNDARY.

    A gate written -ge that should be -gt passes at exactly n, and no amount of reading
    finds that. One value either side does.
    """
    table = {
        "-ge": (str(n - 1), str(n)),
        "-gt": (str(n), str(n + 1)),
        "-le": (str(n + 1), str(n)),
        "-lt": (str(n), str(n - 1)),
        "-eq": (str(n + 1), str(n)),
        "-ne": (str(n), str(n + 1)),
    }
    plant, near = table[op]
    return Predicate(
        kind="NUM_THRESHOLD", source=frag.strip(), reads=f"${var}",
        plant=plant, near_miss=near, boundary=True,
        note=f"boundary of '{op} {n}'. If the gate accepts {plant} the comparison is wrong by one.",
    )


def extract(shell: str) -> list[Predicate]:
    """Find gate predicates in a run: block. Returns [] when nothing is decidable."""
    out: list[Predicate] = []
    seen: set[tuple[str, str]] = set()

    for line in shell.splitlines():
        frag = line.strip()
        if not frag or frag.startswith("#"):
            continue

        m = _NUM.search(frag)
        if m:
            p = _numeric(m.group(1), m.group(2), int(m.group(3)), frag)
            if (p.kind, p.reads) not in seen:
                seen.add((p.kind, p.reads)); out.append(p)
            continue

        m = _STREQ.search(frag)
        if m:
            var, op, val = m.group(1), m.group(2), m.group(3)
            plant, near = (f"{val}_X", val) if op in ("=", "==") else (val, f"{val}_X")
            p = Predicate("STR_EQUAL", frag, f"${var}", plant, near,
                          note=f"gate compares ${var} {op} {val}")
            if (p.kind, p.reads) not in seen:
                seen.add((p.kind, p.reads)); out.append(p)
            continue

        for kind, rx in _RULES:
            m = rx.search(frag)
            if not m:
                continue
            if kind == "GREP_PRESENT":
                pat = m.group(1) or m.group(2) or m.group(3)
                target = m.group(4)
                p = Predicate(kind, frag, target,
                              plant=f"<{target} with no occurrence of {pat!r}>",
                              near_miss=f"<{target} containing {pat!r}>",
                              note="gate requires the pattern to be present")
            elif kind == "JQ_PREDICATE":
                expr = m.group(1)
                p = Predicate(kind, frag, "stdin json",
                              plant=f"<json where `{expr}` is false or null>",
                              near_miss=f"<json where `{expr}` is true>",
                              note="jq -e exits nonzero on false or null")
            elif kind == "FILE_NONEMPTY":
                f_ = m.group(1)
                p = Predicate(kind, frag, f_, plant=f"<{f_} empty>", near_miss=f"<{f_} with one byte>",
                              note="gate requires a non-empty file", boundary=True)
            elif kind == "FILE_EXISTS":
                f_ = m.group(1)
                p = Predicate(kind, frag, f_, plant=f"<{f_} absent>", near_miss=f"<{f_} present>",
                              note="gate requires the file to exist")
            elif kind == "STR_EMPTY":
                v = m.group(1)
                p = Predicate(kind, frag, f"${v}", plant="nonempty", near_miss="",
                              note=f"gate fires when ${v} is EMPTY, so the plant is a value")
            else:  # STR_NONEMPTY
                v = m.group(1)
                p = Predicate(kind, frag, f"${v}", plant="", near_miss="value",
                              note=f"gate requires ${v} to be set")
            if (p.kind, p.reads) not in seen:
                seen.add((p.kind, p.reads)); out.append(p)
            break
    return out


def is_gate_step(shell: str) -> bool:
    """Does this step deliberately fail the build under some condition?"""
    return bool(re.search(r"\bexit\s+[1-9]|::error::|assert|\|\|\s*exit|&&\s*exit", shell))
