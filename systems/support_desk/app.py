"""Support Desk (system #2 of 2).

A small, self-contained FastAPI service that plays the role of an existing
help desk: tickets written by players, with the conversation on each ticket.
It knows nothing about the game backend: no player ids, no purchases, no
incidents. A requester is identified only by the e-mail address they wrote from.

Run:  uvicorn app:app --port 8102
"""
from core import app  # noqa: F401

import tickets  # noqa: E402,F401
import admin  # noqa: E402,F401
