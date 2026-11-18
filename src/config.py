"""Loads config/icp.yaml and .env, and checks the rubric is complete before anything runs."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ICP_PATH = ROOT / "config" / "icp.yaml"

REQUIRED_GATE_KEYS = ("name", "facts_that_settle_it", "fact_keys", "sources",
                      "sufficient_answer", "max_passes", "if_unanswered")
REQUIRED_LOOP_CAPS = ("max_passes", "max_pages_per_company", "max_llm_calls_per_company",
                      "max_free_api_calls_per_company")


class ConfigError(ValueError):
    pass


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def load_icp(path: Path | str = ICP_PATH) -> dict:
    with open(path) as f:
        icp = yaml.safe_load(f)
    validate_icp(icp)
    return icp


def validate_icp(icp: dict) -> None:
    gates = icp.get("gates") or {}
    if set(icp.get("gate_order", [])) != set(gates):
        raise ConfigError("gate_order must list every gate exactly once")
    for gid, gate in gates.items():
        missing = [k for k in REQUIRED_GATE_KEYS if k not in gate]
        if missing:
            raise ConfigError(f"{gid} is missing {missing}")
        if gate["if_unanswered"] != "needs_review":
            raise ConfigError(f"{gid}: if_unanswered must be needs_review (never silently qualify)")
    for wid, w in (icp.get("weighted") or {}).items():
        if wid in gates:
            raise ConfigError(f"{wid} is both a gate and a weighted factor")
    loop = icp.get("agent_loop") or {}
    for cap in REQUIRED_LOOP_CAPS:
        if not isinstance(loop.get(cap), int) or loop[cap] <= 0:
            raise ConfigError(f"agent_loop.{cap} must be a positive integer")


def news_queries(icp: dict) -> dict[str, tuple[str, str]]:
    """query_id -> (gate_id, template)."""
    out = {}
    for gid, gate in icp["gates"].items():
        for qid, tpl in (gate.get("news_queries") or {}).items():
            out[qid] = (gid, tpl)
    return out


def all_actions(icp: dict) -> dict[str, dict]:
    """Every whitelisted action id -> {tier, serves}. news:<qid> actions are derived from gates."""
    actions = {aid: {"tier": a["tier"], "serves": list(a["serves"])} for aid, a in icp["actions"].items()}
    for qid, (gid, _) in news_queries(icp).items():
        actions[f"news:{qid}"] = {"tier": "free", "serves": [gid]}
    return actions


def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val
