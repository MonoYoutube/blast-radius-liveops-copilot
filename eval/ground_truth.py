"""Expected answers, computed straight from the two seed files.

This is deliberately a second, independent implementation: it does not import
the services and does not go through MCP. The scripted check and the tests
compare what the agent's tools return against these values.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "systems" / "game_backend" / "data" / "seed.json"
DESK = ROOT / "systems" / "support_desk" / "data" / "seed.json"

SESSION_REASONS = {"server_crash": "match_crash", "login_timeout": "login_timeout"}
NOT_CHARGED = "payment_failed_not_charged"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load() -> tuple[dict, dict]:
    return json.loads(GAME.read_text(encoding="utf-8")), json.loads(DESK.read_text(encoding="utf-8"))


def _emails(player: dict) -> set[str]:
    return {e.lower() for e in [player["email"], *player["linked_emails"]]}


def affected_players(incident_id: str) -> dict[str, set[str]]:
    """player_id -> set of reasons."""
    game, _ = _load()
    inc = next(i for i in game["incidents"] if i["incident_id"] == incident_id)
    start, end = _ts(inc["started_at"]), _ts(inc["ended_at"])
    out: dict[str, set[str]] = {}
    for s in game["sessions"]:
        if s["server"] == inc["server"] and s["end_reason"] in SESSION_REASONS and start <= _ts(s["ended_at"]) <= end:
            out.setdefault(s["player_id"], set()).add(SESSION_REASONS[s["end_reason"]])
    for p in game["purchases"]:
        if p["server"] == inc["server"] and p["status"] == "charged_not_delivered" and start <= _ts(p["created_at"]) <= end:
            out.setdefault(p["player_id"], set()).add("purchase_charged_not_delivered")
    return out


def q1(incident_id: str) -> dict:
    """Who was affected by the incident, and who of them wrote to support since it started?"""
    game, desk = _load()
    inc = next(i for i in game["incidents"] if i["incident_id"] == incident_id)
    start = _ts(inc["started_at"])
    affected = affected_players(incident_id)
    players = {p["player_id"]: p for p in game["players"]}
    with_tickets: dict[str, list[str]] = {}
    awaiting: list[str] = []
    for t in desk["tickets"]:
        if _ts(t["created_at"]) < start:
            continue
        for pid in affected:
            if t["requester_email"].lower() in _emails(players[pid]):
                with_tickets.setdefault(pid, []).append(t["ticket_id"])
                answered = any(m["author"] == "agent" for m in t["messages"])
                if not answered and t["status"] in ("new", "open", "pending"):
                    awaiting.append(t["ticket_id"])
    return {
        "affected": sorted(affected),
        "with_tickets": {pid: sorted(ids) for pid, ids in sorted(with_tickets.items())},
        "silent": sorted(set(affected) - set(with_tickets)),
        "awaiting_first_response": sorted(awaiting),
    }


def double_charged_orders(purchases: list[dict]) -> list[str]:
    """Orders that were charged more than once. Buying the same item under two orders is not a double charge."""
    charges: dict[str, int] = {}
    for p in purchases:
        if p["status"] != NOT_CHARGED:
            charges[p["order_id"]] = charges.get(p["order_id"], 0) + 1
    return sorted(order for order, count in charges.items() if count > 1)


def q2(ticket_id: str) -> dict:
    """Which player wrote this ticket and what does the purchase log say?"""
    game, desk = _load()
    ticket = next(t for t in desk["tickets"] if t["ticket_id"] == ticket_id)
    player = next((p for p in game["players"] if ticket["requester_email"].lower() in _emails(p)), None)
    if player is None:
        return {"player_id": None}
    purchases = [p for p in game["purchases"] if p["player_id"] == player["player_id"]]
    return {
        "player_id": player["player_id"],
        "undelivered_purchase_ids": sorted(p["purchase_id"] for p in purchases if p["status"] == "charged_not_delivered"),
        "affected_by_incidents": sorted(
            inc["incident_id"] for inc in game["incidents"] if player["player_id"] in affected_players(inc["incident_id"])
        ),
        "double_charged_order_ids": double_charged_orders(purchases),
    }


def q2_resolution(ticket_id: str) -> dict:
    """For a 'paid, not delivered' ticket: the original charge and any compensation granted for it later.

    The purchase row keeps its original status; the compensation ledger is a separate record.
    """
    game, _ = _load()
    base = q2(ticket_id)
    undelivered = base.get("undelivered_purchase_ids", [])
    related = sorted(c["compensation_id"] for c in game["compensations"] if c["related_purchase_id"] in undelivered)
    settled = {c["related_purchase_id"] for c in game["compensations"] if c["related_purchase_id"] in undelivered}
    return {
        "player_id": base["player_id"],
        "undelivered_purchase_ids": undelivered,
        "related_compensation_ids": related,
        "still_owed_purchase_ids": sorted(set(undelivered) - settled),
    }


def q3(incident_id: str) -> dict:
    """Who of the affected players has already been compensated? (needs the endpoint added live)"""
    game, _ = _load()
    affected = set(affected_players(incident_id))
    compensated = {c["player_id"] for c in game["compensations"] if c["incident_id"] == incident_id} & affected
    first = q1(incident_id)
    waiting_players = sorted(
        pid for pid, ids in first["with_tickets"].items()
        if pid not in compensated and any(t in first["awaiting_first_response"] for t in ids)
    )
    return {
        "compensated": sorted(compensated),
        "not_compensated": sorted(affected - compensated),
        "not_compensated_and_awaiting_reply": waiting_players,
        "not_compensated_and_silent": sorted(set(first["silent"]) - compensated),
    }


if __name__ == "__main__":
    print(json.dumps({
        "Q1 INC-2026-11-18-EU2": q1("INC-2026-11-18-EU2"),
        "Q1 INC-2026-11-12-NA1": q1("INC-2026-11-12-NA1"),
        "Q2 T-1042": q2("T-1042"),
        "Q2 T-1047": q2("T-1047"),
        "Q2 T-1051": q2("T-1051"),
        "Q2 T-1045": q2("T-1045"),
        "Q2 T-1042 resolution": q2_resolution("T-1042"),
        "Q3 INC-2026-11-18-EU2": q3("INC-2026-11-18-EU2"),
    }, indent=2))
