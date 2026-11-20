"""Shared state of the Support Desk: the FastAPI app, the data set and ticket helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

DATA_FILE = Path(__file__).parent / "data" / "seed.json"

app = FastAPI(title="Support Desk API", version="1.0.0")

ACTIVE_STATUSES = {"new", "open", "pending"}

DB: dict = {}


def reload_data() -> None:
    DB.clear()
    DB.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))


reload_data()


def ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. A value without an offset is read as UTC."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


STATUSES = ("new", "open", "pending", "solved")


def first_response_at(ticket: dict) -> Optional[str]:
    for message in ticket["messages"]:
        if message["author"] == "agent":
            return message["at"]
    return None


def summary(ticket: dict) -> dict:
    """Ticket without the conversation, plus the fields needed to triage it."""
    answered_at = first_response_at(ticket)
    return {
        "ticket_id": ticket["ticket_id"],
        "requester_email": ticket["requester_email"],
        "requester_name": ticket["requester_name"],
        "subject": ticket["subject"],
        "category": ticket["category"],
        "status": ticket["status"],
        "priority": ticket["priority"],
        "created_at": ticket["created_at"],
        "tags": ticket["tags"],
        "first_response_at": answered_at,
        "awaiting_first_response": answered_at is None and ticket["status"] in ACTIVE_STATUSES,
    }


def ticket_or_404(ticket_id: str) -> dict:
    for ticket in DB["tickets"]:
        if ticket["ticket_id"].lower() == ticket_id.strip().lower():
            return ticket
    raise HTTPException(status_code=404, detail=f"ticket {ticket_id!r} not found")
