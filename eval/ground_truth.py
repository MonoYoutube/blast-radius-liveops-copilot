"""Expected answers, computed straight from the two seed files.

This is deliberately a second, independent implementation: it does not import
the services and does not go through MCP. The scripted check and the tests
compare what the agent's tools return against these values.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SESSION_REASONS = {"server_crash": "match_crash", "login_timeout": "login_timeout"}
NOT_CHARGED = "payment_failed_not_charged"
# A ticket counts as a report of the incident when its category matches a way the game says its author was affected.
TOPIC_OF_REASON = {"match_crash": "gameplay", "login_timeout": "gameplay", "purchase_charged_not_delivered": "billing"}
ACTIVE = ("new", "open", "pending")


def data_file(key: str) -> Path:
    """The data file of a system: the committed one, or the isolated copy named by <KEY>_DIR (see eval/instance.py)."""
    return Path(os.environ.get(f"{key.upper()}_DIR") or ROOT / "systems" / key) / "data" / "seed.json"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load() -> tuple[dict, dict]:
    return (json.loads(data_file("game_backend").read_text(encoding="utf-8")),
            json.loads(data_file("support_desk").read_text(encoding="utf-8")))


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


def _answered_at(ticket: dict) -> str | None:
    return next((m["at"] for m in ticket["messages"] if m["author"] == "agent"), None)


def _incident_tickets(incident_id: str) -> tuple[dict, dict[str, set[str]], list[tuple[str, dict]]]:
    """The incident, its affected players and every (player_id, ticket) they wrote since it started."""
    game, desk = _load()
    inc = next(i for i in game["incidents"] if i["incident_id"] == incident_id)
    affected = affected_players(incident_id)
    owner = {email: p["player_id"] for p in game["players"] if p["player_id"] in affected for email in _emails(p)}
    rows = [
        (owner[t["requester_email"].lower()], t) for t in desk["tickets"]
        if t["requester_email"].lower() in owner and _ts(t["created_at"]) >= _ts(inc["started_at"])
    ]
    return inc, affected, rows


def q1_topics(incident_id: str) -> dict:
    """Of the tickets affected players wrote since the incident started: which report it, which are about something else?

    Author and time only show that an affected player wrote. A ticket is a report of the incident when its
    category matches a way the game says that player was affected (see TOPIC_OF_REASON).
    """
    _, affected, rows = _incident_tickets(incident_id)
    about, other = [], []
    for player_id, ticket in rows:
        topics = {TOPIC_OF_REASON[reason] for reason in affected[player_id]}
        (about if ticket["category"] in topics else other).append(ticket)
    waiting = lambda tickets: sorted(t["ticket_id"] for t in tickets if _answered_at(t) is None and t["status"] in ACTIVE)
    return {
        "about_incident": sorted(t["ticket_id"] for t in about),
        "other_topics": sorted(t["ticket_id"] for t in other),
        "awaiting_about_incident": waiting(about),
        "awaiting_other_topics": waiting(other),
    }


def q1_moment(incident_id: str) -> dict:
    """Tickets from affected players without a first reply: when the incident ended, and in the desk as it is now."""
    inc, _, rows = _incident_tickets(incident_id)
    end = _ts(inc["ended_at"])
    return {
        "unanswered_at_incident_end": sorted(
            t["ticket_id"] for _, t in rows
            if _ts(t["created_at"]) <= end and (_answered_at(t) is None or _ts(_answered_at(t)) > end)
        ),
        "awaiting_first_response_now": sorted(
            t["ticket_id"] for _, t in rows if _answered_at(t) is None and t["status"] in ACTIVE
        ),
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

    The purchase row keeps its original status; the compensation ledger is a separate record. Only a
    compensation that names the purchase settles it. Anything else the player was granted is listed apart.
    """
    game, _ = _load()
    base = q2(ticket_id)
    undelivered = base.get("undelivered_purchase_ids", [])
    ledger = [c for c in game["compensations"]
              if c["player_id"] == base["player_id"] and c["incident_id"] in base.get("affected_by_incidents", [])]
    settled = {c["related_purchase_id"] for c in ledger if c["related_purchase_id"] in undelivered}
    return {
        "player_id": base["player_id"],
        "undelivered_purchase_ids": undelivered,
        "related_compensation_ids": sorted(c["compensation_id"] for c in ledger if c["related_purchase_id"] in undelivered),
        "other_grant_ids": sorted(c["compensation_id"] for c in ledger if c["related_purchase_id"] not in undelivered),
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


def q3_owed(incident_id: str, ledger: bool = True) -> dict:
    """What is still owed for the incident, purchase by purchase.

    A purchase that was charged and not delivered is settled only by a compensation whose related_purchase_id
    names it. A grant for something else (a crash grant, for instance) settles no purchase, even for the same
    player. With ledger=False this is what can be said while no tool returns compensations: the undelivered
    purchases are known, whether they were settled is not.
    """
    game, _ = _load()
    inc, affected, rows = _incident_tickets(incident_id)
    start, end = _ts(inc["started_at"]), _ts(inc["ended_at"])
    undelivered = {
        p["purchase_id"]: p["player_id"] for p in game["purchases"]
        if p["server"] == inc["server"] and p["status"] == "charged_not_delivered" and start <= _ts(p["created_at"]) <= end
    }
    if not ledger:
        return {"ledger_checked": False, "undelivered_purchase_ids": sorted(undelivered),
                "settled": None, "owed": None, "owed_players_awaiting_reply": None, "grants_without_purchase": None}
    grants = [c for c in game["compensations"] if c["incident_id"] == incident_id and c["player_id"] in affected]
    settled: dict[str, list[str]] = {}
    loose: dict[str, list[str]] = {}
    for c in sorted(grants, key=lambda c: c["compensation_id"]):
        if c["related_purchase_id"] in undelivered:
            settled.setdefault(c["related_purchase_id"], []).append(c["compensation_id"])
        else:
            loose.setdefault(c["player_id"], []).append(c["compensation_id"])
    owed = {purchase_id: player_id for purchase_id, player_id in sorted(undelivered.items()) if purchase_id not in settled}
    waiting = {player_id for player_id, t in rows if _answered_at(t) is None and t["status"] in ACTIVE}
    return {
        "ledger_checked": True,
        "undelivered_purchase_ids": sorted(undelivered),
        "settled": dict(sorted(settled.items())),
        "owed": owed,
        "owed_players_awaiting_reply": sorted(set(owed.values()) & waiting),
        "grants_without_purchase": dict(sorted(loose.items())),
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
        "Q2 T-1043 resolution": q2_resolution("T-1043"),
        "Q3 INC-2026-11-18-EU2": q3("INC-2026-11-18-EU2"),
        "Q1 topics": q1_topics("INC-2026-11-18-EU2"),
        "Q1 moment": q1_moment("INC-2026-11-18-EU2"),
        "Q3 owed": q3_owed("INC-2026-11-18-EU2"),
        "Q3 owed, no ledger": q3_owed("INC-2026-11-18-EU2", ledger=False),
    }, indent=2))
