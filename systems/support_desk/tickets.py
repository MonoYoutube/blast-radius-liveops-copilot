"""Read endpoints of the Support Desk."""
from __future__ import annotations

from typing import List, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel

from core import DB, STATUSES, app, summary, ticket_or_404, ts


class TicketSearch(BaseModel):
    requester_emails: Union[List[str], str, None] = None
    created_from: Optional[str] = None
    status: Optional[str] = None


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    """Get one support ticket by id, e.g. T-1042, with the full conversation, requester e-mail, status and UTC timestamps."""
    ticket = ticket_or_404(ticket_id)
    return {**summary(ticket), "messages": ticket["messages"]}


@app.post("/tickets/search")
def search_tickets(body: TicketSearch):
    """Search tickets for many requester e-mails at once. Body: requester_emails (list or comma-separated string; null or empty = any), created_from (UTC ISO time), status (new, open, pending, solved). Also returns emails_without_tickets."""
    # null (or an empty string) means "any requester"; an explicit empty list means "nobody" and matches nothing.
    # A string is accepted because hosted Synapse tools type every body field as a string.
    emails = body.requester_emails
    if isinstance(emails, str):
        emails = emails.split(",") if emails.strip() else None
    filter_by_email = emails is not None
    wanted = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    try:
        created_from = ts(body.created_from) if body.created_from else None
    except ValueError:
        raise HTTPException(status_code=422, detail="created_from must be an ISO-8601 timestamp, e.g. 2026-11-18T18:10:00Z")
    status = body.status.strip().lower() if body.status else None
    if status and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")

    items = []
    for ticket in DB["tickets"]:
        if filter_by_email and ticket["requester_email"].lower() not in wanted:
            continue
        if created_from and ts(ticket["created_at"]) < created_from:
            continue
        if status and ticket["status"] != status:
            continue
        items.append(summary(ticket))
    items.sort(key=lambda t: t["created_at"])
    matched = {t["requester_email"].lower() for t in items}
    return {
        "count": len(items),
        "items": items,
        "emails_without_tickets": [e for e in wanted if e not in matched],
    }


@app.get("/requesters/{email}/tickets")
def list_requester_tickets(email: str):
    """List all tickets ever written from one e-mail address (case-insensitive), oldest first, without the conversation."""
    wanted = email.strip().lower()
    items = [summary(t) for t in DB["tickets"] if t["requester_email"].lower() == wanted]
    items.sort(key=lambda t: t["created_at"])
    return {"requester_email": wanted, "count": len(items), "items": items}


@app.get("/queues/awaiting-first-response")
def list_tickets_awaiting_first_response():
    """List active tickets that no support agent has answered yet, oldest first. This is the queue of people still waiting."""
    items = [summary(t) for t in DB["tickets"]]
    items = [t for t in items if t["awaiting_first_response"]]
    items.sort(key=lambda t: t["created_at"])
    return {"count": len(items), "items": items}
