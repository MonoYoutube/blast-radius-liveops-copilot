# >>> live-demo endpoint (remove with: python scripts/live_update.py revert)
@app.get("/incidents/{incident_id}/compensations")
def list_incident_compensations(incident_id: str):
    """List compensations already granted for an incident: player, grant, reason, related purchase, UTC time and who granted it. Check this before recommending a new compensation, so nobody is compensated twice."""
    incident = incident_or_404(incident_id)
    items = [c for c in DB["compensations"] if c["incident_id"] == incident["incident_id"]]
    items.sort(key=lambda c: (c["granted_at"], c["compensation_id"]))
    return {"incident_id": incident["incident_id"], "count": len(items), "items": items}
# <<< live-demo endpoint
