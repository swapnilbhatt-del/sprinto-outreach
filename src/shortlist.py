"""Step 2: normalise input domains, drop CRM customers and competitor customers, cap at N."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .config import ROOT


def normalise(raw: str) -> str | None:
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].split(":")[0]
    d = re.sub(r"^www\.", "", d)
    return d if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d) else None


def load_exclusions(specs: list[dict], root: Path = ROOT) -> dict[str, str]:
    """domain -> which file excluded it."""
    out = {}
    for spec in specs:
        path = root / spec["path"]
        if not path.exists():
            if spec.get("required"):
                raise FileNotFoundError(f"required exclusion list missing: {path}")
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            stages = {x.lower() for x in spec.get("exclude_stages", [])}
            for row in csv.DictReader(f):
                d = normalise(row.get(spec["domain_column"], ""))
                stage = (row.get(spec.get("stage_column") or "") or "").strip().lower()
                # With a stage column, only listed stages (e.g. customer) are excluded, so prospects
                # logged in the CRM as leads stay researchable. A row with no stage is excluded (safe default).
                if d and (not stages or not stage or stage in stages):
                    out.setdefault(d, spec["path"] + (f" ({stage})" if stage else ""))
    return out


def shortlist(domains: list[str], icp: dict, root: Path = ROOT) -> tuple[list[str], list[dict]]:
    """(kept, removed). removed rows: {domain, reason}."""
    run = icp["run"]
    excl = load_exclusions(run["shortlist_exclusions"], root)
    kept, removed, seen = [], [], set()
    for raw in domains:
        d = normalise(raw)
        if not d:
            if raw.strip() and not raw.strip().startswith("#"):
                removed.append({"domain": raw.strip(), "reason": "not a valid domain"})
            continue
        if d in seen:
            continue
        seen.add(d)
        if d in excl:
            removed.append({"domain": d, "reason": f"in {excl[d]}"})
        elif len(kept) >= run["max_companies_per_run"]:
            removed.append({"domain": d, "reason": f"over run cap of {run['max_companies_per_run']}"})
        else:
            kept.append(d)
    return kept, removed
