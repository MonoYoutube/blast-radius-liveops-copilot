"""Kestrel Arena LiveOps backend (system #1 of 2).

A small, self-contained FastAPI service that plays the role of an existing game
backend: players, match sessions, store purchases and operational incidents.
It knows nothing about the support desk. The only thing the two systems have in
common is a player's e-mail address.

Run:  uvicorn app:app --port 8101
"""
from core import app  # noqa: F401

import incidents  # noqa: E402,F401
import players  # noqa: E402,F401
import admin  # noqa: E402,F401
