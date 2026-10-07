"""The corpus is the specification.

A detector must fire on the planted defect AND stay quiet on its near miss.
Both directions are required before a detector is called live.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from deadgate.detectors import scan_workflow

CORPUS = pathlib.Path(__file__).parent.parent / "corpus"
BROKEN = sorted((CORPUS / "broken").glob("*.yml"))
GOOD = sorted((CORPUS / "good").glob("*.yml"))

assert BROKEN, "no broken fixtures found"
assert GOOD, "no good fixtures found"


def _expected(path: pathlib.Path) -> str:
    return path.read_text().split("EXPECT:")[1].split()[0].strip()


@pytest.mark.parametrize("path", BROKEN, ids=lambda p: p.stem)
def test_known_broken_fires(path: pathlib.Path) -> None:
    want = _expected(path)
    got = {f.detector for f in scan_workflow(yaml.safe_load(path.read_text()))}
    assert want in got, f"{path.name}: expected {want}, got {sorted(got) or 'nothing'}"


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.stem)
def test_known_good_stays_silent(path: pathlib.Path) -> None:
    """A good fixture may be scoped to ONE detector with `# SILENT: D1`.

    Some shapes are correct for one detector's question and genuinely suspect for
    another's. Asserting that nothing at all fires forces a choice between a false
    negative and a false positive, so the fixture says which question it answers.
    """
    text = path.read_text()
    got = scan_workflow(yaml.safe_load(text))
    if "# SILENT:" in text:
        scoped = text.split("# SILENT:")[1].split()[0].strip()
        offending = [f for f in got if f.detector == scoped]
        assert not offending, (
            f"{path.name} must not fire {scoped}, but got "
            f"{[f'{f.detector}:{f.job}' for f in offending]}"
        )
        return
    assert not got, (
        f"{path.name} is a near miss and must not fire, but got "
        f"{[f'{f.detector}:{f.job}' for f in got]}"
    )
