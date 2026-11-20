"""Incident endpoints of the Kestrel Arena LiveOps backend."""
from __future__ import annotations

from core import DB, INFRA_END_REASONS, app, incident_or_404, player_or_404, ts


@app.get("/incidents")
def list_incidents():
    """List operational incidents of the game (id, title, server, UTC start/end, status). Start here to find an incident_id."""
    return {"count": len(DB["incidents"]), "items": DB["incidents"]}


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Get one incident by id, e.g. INC-2026-11-18-EU2: affected server, UTC time window, status and summary."""
    return incident_or_404(incident_id)


@app.get("/incidents/{incident_id}/affected-players")
def list_incident_affected_players(incident_id: str):
    """List every player affected by an incident: player_id, nickname, reasons (match_crash, login_timeout, purchase_charged_not_delivered) and emails. Use every address in emails to find the player's tickets in the support desk."""
    incident = incident_or_404(incident_id)
    start, end = ts(incident["started_at"]), ts(incident["ended_at"])
    affected: dict[str, dict] = {}

    def entry(player_id: str) -> dict:
        if player_id not in affected:
            player = player_or_404(player_id)
            affected[player_id] = {
                "player_id": player["player_id"],
                "nickname": player["nickname"],
                "emails": [player["email"], *player["linked_emails"]],
                "reasons": [],
                "interrupted_session_ids": [],
                "undelivered_purchases": [],
            }
        return affected[player_id]

    for session in DB["sessions"]:
        if (
            session["server"] == incident["server"]
            and session["end_reason"] in INFRA_END_REASONS
            and start <= ts(session["ended_at"]) <= end
        ):
            row = entry(session["player_id"])
            reason = INFRA_END_REASONS[session["end_reason"]]
            if reason not in row["reasons"]:
                row["reasons"].append(reason)
            row["interrupted_session_ids"].append(session["session_id"])

    for purchase in DB["purchases"]:
        if (
            purchase["server"] == incident["server"]
            and purchase["status"] == "charged_not_delivered"
            and start <= ts(purchase["created_at"]) <= end
        ):
            row = entry(purchase["player_id"])
            if "purchase_charged_not_delivered" not in row["reasons"]:
                row["reasons"].append("purchase_charged_not_delivered")
            row["undelivered_purchases"].append(
                {
                    "purchase_id": purchase["purchase_id"],
                    "order_id": purchase["order_id"],
                    "item": purchase["item"],
                    "amount_usd": purchase["amount_usd"],
                    "created_at": purchase["created_at"],
                }
            )

    items = sorted(affected.values(), key=lambda row: row["player_id"])
    return {
        "incident_id": incident["incident_id"],
        "server": incident["server"],
        "started_at": incident["started_at"],
        "ended_at": incident["ended_at"],
        "count": len(items),
        "items": items,
    }
