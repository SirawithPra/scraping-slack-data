"""The clustering resolution is one number, defined once, chosen by measurement.

It lived as the literal `1.0` in five places — two function signatures and three CLI
defaults — which is how a repeated constant drifts apart: raising it in `build_digest`
while `tam.analysis.graph --resolution` still defaulted to the old value would make the
report and the tool that explains the report disagree, with nothing failing.

The value itself is argued in `graph.DEFAULT_RESOLUTION`'s comment, including what it
costs and what it does not fix.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from tam.analysis.digest import build_digest
from tam.analysis.graph import DEFAULT_RESOLUTION, detect_communities
from tam.analysis.linker import cluster_labels

MODULES = ("analysis/graph.py", "analysis/digest.py", "analysis/linker.py")


def test_every_signature_takes_the_shared_default() -> None:
    for function in (detect_communities, build_digest, cluster_labels):
        default = inspect.signature(function).parameters["resolution"].default
        assert default == DEFAULT_RESOLUTION, f"{function.__name__} has its own resolution"


def test_no_module_hardcodes_a_resolution_default() -> None:
    # Catches both `resolution: float = 1.0` and `add_argument(..., default=1.0)`.
    literal = re.compile(r"resolution[^\n=]*=\s*(\d+\.?\d*)|default=(\d+\.?\d*)[^\n]*resolution")
    root = Path(__file__).resolve().parents[1] / "tam"
    for name in MODULES:
        for line_number, line in enumerate(((root / name).read_text(encoding="utf-8")).splitlines(), 1):
            if "resolution" not in line or "DEFAULT_RESOLUTION" in line or line.lstrip().startswith("#"):
                continue
            match = literal.search(line)
            assert not match, f"{name}:{line_number} hardcodes a resolution default: {line.strip()}"


def test_the_chosen_value_splits_rather_than_merges() -> None:
    # Pins the direction, not the digit: the measurement that set this found 1.0 merged a
    # whole channel into one work item, so a future edit that lands back at or below the
    # old default should have to say so here.
    assert DEFAULT_RESOLUTION > 1.0
