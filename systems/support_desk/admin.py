"""Operations that exist in the help desk but are deliberately NOT exposed to the agent.

See mcp/exposure.json for the reasons.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from core import DB, app, reload_data, summary, ticket_or_404


class ReplyIn(BaseModel):
    text: str


@app.post("/tickets/{ticket_id}/replies")
def reply_to_ticket(ticket_id: str, body: ReplyIn):
    """Send a reply to the customer on a ticket. Customer-facing and cannot be unsent."""
    ticket = ticket_or_404(ticket_id)
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ticket["messages"].append({"author": "agent", "at": at, "text": body.text})
    if ticket["status"] == "new":
        ticket["status"] = "open"
    return {**summary(ticket), "messages": ticket["messages"]}


@app.post("/admin/reseed")
def admin_reseed():
    """Reload the data set from disk. Maintenance only."""
    reload_data()
    return {"status": "reseeded", "tickets": len(DB["tickets"])}
