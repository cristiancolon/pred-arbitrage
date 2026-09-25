"""The approved-pairs file (pairs.csv): the scanner's source of truth.

Edit it by hand or through ``arbscan review``. Lines starting with '#' are ignored.
The scanner reloads it whenever it changes.
"""

import csv
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .store import pair_id

log = logging.getLogger(__name__)

HEADER = ["kalshi_ticker", "pm_slug", "relation", "added", "note"]
RELATIONS = ("same", "inverse")


@dataclass(frozen=True)
class Pair:
    kalshi: str
    pm: str
    relation: str
    note: str = ""

    @property
    def id(self) -> str:
        return pair_id(self.kalshi, self.pm)


def load_pairs(path: str) -> list[Pair]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    out: dict[str, Pair] = {}
    for n, row in enumerate(csv.DictReader(lines), start=2):
        k = (row.get("kalshi_ticker") or "").strip()
        p = (row.get("pm_slug") or "").strip()
        rel = (row.get("relation") or "same").strip().lower()
        if not k or not p:
            log.warning("%s row %d: missing kalshi_ticker or pm_slug, skipped", path, n)
            continue
        if rel not in RELATIONS:
            log.warning("%s row %d: relation %r must be one of %s, skipped", path, n, rel, RELATIONS)
            continue
        pair = Pair(k, p, rel, (row.get("note") or "").strip())
        out[pair.id] = pair
    return list(out.values())


def append_pair(path: str, kalshi: str, pm: str, relation: str, note: str = "") -> None:
    if relation not in RELATIONS:
        raise ValueError(relation)
    new = not os.path.exists(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADER)
        w.writerow([kalshi, pm, relation, time.strftime("%Y-%m-%d"), note])


def remove_pairs(path: str, ids: set[str]) -> int:
    """Rewrite pairs.csv without the given pair ids, keeping comments and order."""
    if not os.path.exists(path) or not ids:
        return 0
    with open(path, newline="") as f:
        lines = f.readlines()
    keep, removed = [], 0
    for i, line in enumerate(lines):
        if i == 0 or not line.strip() or line.lstrip().startswith("#"):
            keep.append(line)
            continue
        row = next(csv.reader([line]), [])
        if len(row) >= 2 and pair_id(row[0].strip(), row[1].strip()) in ids:
            removed += 1
            continue
        keep.append(line)
    if removed:
        tmp = f"{path}.tmp"
        with open(tmp, "w", newline="") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    return removed


class PairFile:
    """pairs.csv, reloaded when its mtime changes."""

    def __init__(self, path: str):
        self.path = path
        self._mtime: float | None = None
        self.pairs: list[Pair] = []

    def refresh(self) -> bool:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        self.pairs = load_pairs(self.path)
        log.info("loaded %d pairs from %s", len(self.pairs), self.path)
        return True
