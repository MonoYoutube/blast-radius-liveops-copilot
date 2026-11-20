"""Shared state of the Kestrel Arena LiveOps backend: the FastAPI app, the data set and lookups."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

DATA_FILE = Path(__file__).parent / "data" / "seed.json"

app = FastAPI(title="Kestrel Arena LiveOps API", version="1.0.0")

# Session end reasons caused by our infrastructure, and the reason an affected player is given for each.
INFRA_END_REASONS = {"server_crash": "match_crash", "login_timeout": "login_timeout"}

DB: dict = {}


def reload_data() -> None:
    DB.clear()
    DB.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))


reload_data()


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def incident_or_404(incident_id: str) -> dict:
    for incident in DB["incidents"]:
        if incident["incident_id"].lower() == incident_id.strip().lower():
            return incident
    raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")


def player_or_404(player_id: str) -> dict:
    for player in DB["players"]:
        if player["player_id"].lower() == player_id.strip().lower():
            return player
    raise HTTPException(status_code=404, detail=f"player {player_id!r} not found")


def player_view(player: dict) -> dict:
    return {**player, "emails": [player["email"], *player["linked_emails"]]}
