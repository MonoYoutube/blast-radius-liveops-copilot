"""Player endpoints of the Kestrel Arena LiveOps backend."""
from __future__ import annotations

from fastapi import HTTPException

from core import DB, app, player_or_404, player_view


@app.get("/players/by-email/{email}")
def find_player_by_email(email: str):
    """Find the player that owns an e-mail address (primary or linked, case-insensitive). Returns 404 if no player uses this address."""
    wanted = email.strip().lower()
    for player in DB["players"]:
        known = [player["email"], *player["linked_emails"]]
        if wanted in (address.lower() for address in known):
            return player_view(player)
    raise HTTPException(status_code=404, detail=f"no player uses the e-mail {email!r}")


@app.get("/players/{player_id}")
def get_player(player_id: str):
    """Get a player profile by player_id, e.g. P-1002: nickname, region and all e-mail addresses."""
    return player_view(player_or_404(player_id))


@app.get("/players/{player_id}/purchases")
def list_player_purchases(player_id: str):
    """List a player's store purchases, newest first. status: delivered, charged_not_delivered (paid, item missing) or payment_failed_not_charged. One row per charge; a double charge is two rows with the same order_id."""
    player = player_or_404(player_id)
    items = [p for p in DB["purchases"] if p["player_id"] == player["player_id"]]
    items.sort(key=lambda p: p["created_at"], reverse=True)
    return {"player_id": player["player_id"], "nickname": player["nickname"], "count": len(items), "items": items}
