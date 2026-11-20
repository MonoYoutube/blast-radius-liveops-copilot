"""Operations that exist in the backend but are deliberately NOT exposed to the agent.

See mcp/exposure.json for the reasons.
"""
from __future__ import annotations

from core import DB, app, player_or_404, reload_data


@app.post("/admin/reseed")
def admin_reseed():
    """Reload the data set from disk. Maintenance only."""
    reload_data()
    return {"status": "reseeded", "players": len(DB["players"])}


@app.delete("/players/{player_id}")
def erase_player(player_id: str):
    """Erase a player and all of their records (data-protection request). Irreversible."""
    pid = player_or_404(player_id)["player_id"]
    DB["players"] = [p for p in DB["players"] if p["player_id"] != pid]
    for table in ("sessions", "purchases", "compensations"):
        DB[table] = [row for row in DB[table] if row["player_id"] != pid]
    return {"status": "erased", "player_id": pid}
