"""Black-box checks of the two source systems over HTTP."""
import requests

from eval import ground_truth

INCIDENT = "INC-2026-11-18-EU2"


def get(base, path):
    return requests.get(base + path, timeout=10)


def search(base, **body):
    payload = {"requester_emails": None, "created_from": None, "status": None, **body}
    return requests.post(base + "/tickets/search", json=payload, timeout=10)


def test_incidents_and_affected_players(apis):
    game = apis["game_backend"]
    assert get(game, "/incidents").json()["count"] == 3
    rows = get(game, f"/incidents/{INCIDENT}/affected-players").json()["items"]
    assert sorted(r["player_id"] for r in rows) == ground_truth.q1(INCIDENT)["affected"]
    by_id = {r["player_id"]: r for r in rows}
    assert set(by_id["P-1002"]["reasons"]) == {"match_crash", "purchase_charged_not_delivered"}
    assert by_id["P-1007"]["reasons"] == ["purchase_charged_not_delivered"]      # bought, never crashed
    assert by_id["P-1007"]["interrupted_session_ids"] == []
    assert "t.sadykov@example.org" in by_id["P-1002"]["emails"]                  # linked address travels with the player
    assert by_id["P-1002"]["undelivered_purchases"][0]["order_id"] == "O-7001"


def test_players_not_affected_stay_out(apis):
    rows = get(apis["game_backend"], f"/incidents/{INCIDENT}/affected-players").json()["items"]
    ids = {r["player_id"] for r in rows}
    assert "P-1008" not in ids      # played and bought after the incident ended
    assert "P-1013" not in ids      # was on another server
    assert "P-1014" not in ids      # left before the incident started


def test_reason_matches_what_happened(apis):
    game = apis["game_backend"]
    asia = get(game, "/incidents/INC-2026-11-05-ASIA1/affected-players").json()["items"]
    assert [(r["player_id"], r["reasons"]) for r in asia] == [("P-1009", ["login_timeout"])]   # not a match crash
    quiet = get(game, "/incidents/INC-2026-11-12-NA1/affected-players").json()
    assert quiet["count"] == 0 and quiet["items"] == []


def test_email_bridge(apis):
    game = apis["game_backend"]
    assert get(game, "/players/by-email/t.sadykov@example.org").json()["player_id"] == "P-1002"
    assert get(game, "/players/by-email/TIMUR.S@EXAMPLE.COM").json()["player_id"] == "P-1002"
    assert get(game, "/players/by-email/ghost.player@example.net").status_code == 404
    assert get(game, "/incidents/INC-NOPE").status_code == 404


def test_purchase_log(apis):
    game = apis["game_backend"]
    kraken = get(game, "/players/P-1008/purchases").json()["items"]
    assert len({p["order_id"] for p in kraken}) == len(kraken) == 1               # one order, one charge: claim is false
    echo = get(game, "/players/P-1011/purchases").json()["items"]
    assert [p["order_id"] for p in echo] == ["O-7011", "O-7011"]                  # one order charged twice: claim is true
    frost = get(game, "/players/P-1002/purchases").json()["items"]
    assert [p["purchase_id"] for p in frost if p["status"] == "charged_not_delivered"] == ["PU-5001"]
    assert len({p["order_id"] for p in frost}) == len(frost) == 2                 # two different orders, no double charge


def test_ticket_search_filters(apis):
    desk = apis["support_desk"]
    found = search(desk, requester_emails=["Aruzhan.K@example.com", "nobody@example.com"], created_from="2026-11-18T18:10:00Z").json()
    assert [t["ticket_id"] for t in found["items"]] == ["T-1041"]                 # T-1030 is older than the incident
    assert found["emails_without_tickets"] == ["nobody@example.com"]
    assert search(desk, status="solved").json()["count"] == 3


def test_ticket_search_edge_semantics(apis):
    desk = apis["support_desk"]
    assert search(desk).json()["count"] == 12                                     # null = any requester
    assert search(desk, requester_emails=[]).json()["count"] == 0                 # empty list = nobody
    assert search(desk, requester_emails=["  "]).json()["count"] == 0
    with_zone = search(desk, created_from="2026-11-18T18:10:00Z").json()
    without_zone = search(desk, created_from="2026-11-18T18:10:00")               # no offset is read as UTC
    assert without_zone.status_code == 200 and without_zone.json() == with_zone
    assert search(desk, created_from="2026-11-18T23:10:00+05:00").json() == with_zone
    assert search(desk, created_from="yesterday").status_code == 422
    assert search(desk, status="closed").status_code == 422


def test_ticket_search_accepts_strings_for_hosted_tools(apis):
    """Hosted Synapse tools send every body field as a string; the endpoint has to cope with that."""
    desk = apis["support_desk"]
    as_list = search(desk, requester_emails=["aruzhan.k@example.com", "marco.r@example.com"], created_from="2026-11-18T18:10:00Z").json()
    as_text = search(desk, requester_emails="aruzhan.k@example.com, Marco.R@example.com", created_from="2026-11-18T18:10:00Z", status="").json()
    assert as_text == as_list and [t["ticket_id"] for t in as_text["items"]] == ["T-1041", "T-1043"]
    assert search(desk, requester_emails="", created_from="", status="").json()["count"] == 12    # empty strings = no filter


def test_ticket_and_queue(apis):
    desk = apis["support_desk"]
    ticket = get(desk, "/tickets/T-1042").json()
    assert ticket["awaiting_first_response"] is True and ticket["requester_email"] == "t.sadykov@example.org"
    queue = get(desk, "/queues/awaiting-first-response").json()["items"]
    assert [t["ticket_id"] for t in queue] == ["T-1051", "T-1045", "T-1042", "T-1043", "T-1047"]   # oldest first
    assert get(desk, "/requesters/ARUZHAN.K@example.com/tickets").json()["count"] == 2
    assert get(desk, "/tickets/T-0000").status_code == 404
