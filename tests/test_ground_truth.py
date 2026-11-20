"""The data set is part of the demo. These literals fail loudly if someone changes it by accident."""
from eval import ground_truth

INCIDENT = "INC-2026-11-18-EU2"


def test_q1_literal():
    assert ground_truth.q1(INCIDENT) == {
        "affected": ["P-1001", "P-1002", "P-1003", "P-1004", "P-1005", "P-1006", "P-1007"],
        "with_tickets": {"P-1001": ["T-1041"], "P-1002": ["T-1042"], "P-1004": ["T-1043"], "P-1005": ["T-1044"]},
        "silent": ["P-1003", "P-1006", "P-1007"],
        "awaiting_first_response": ["T-1042", "T-1043"],
    }


def test_q1_incident_without_affected_players():
    assert ground_truth.q1("INC-2026-11-12-NA1") == {"affected": [], "with_tickets": {}, "silent": [], "awaiting_first_response": []}
    assert ground_truth.affected_players("INC-2026-11-05-ASIA1") == {"P-1009": {"login_timeout"}}


def test_q2_literals():
    assert ground_truth.q2("T-1042") == {
        "player_id": "P-1002", "undelivered_purchase_ids": ["PU-5001"],
        "affected_by_incidents": [INCIDENT], "double_charged_order_ids": [],
    }
    assert ground_truth.q2("T-1047") == {                                        # "charged twice": false
        "player_id": "P-1008", "undelivered_purchase_ids": [],
        "affected_by_incidents": [], "double_charged_order_ids": [],
    }
    assert ground_truth.q2("T-1051") == {                                        # "charged twice": true
        "player_id": "P-1011", "undelivered_purchase_ids": [],
        "affected_by_incidents": [], "double_charged_order_ids": ["O-7011"],
    }
    assert ground_truth.q2("T-1045") == {"player_id": None}                      # unknown requester


def test_same_item_under_two_orders_is_not_a_double_charge():
    rows = [
        {"order_id": "O-1", "status": "delivered"}, {"order_id": "O-2", "status": "delivered"},
        {"order_id": "O-3", "status": "payment_failed_not_charged"}, {"order_id": "O-3", "status": "delivered"},
    ]
    assert ground_truth.double_charged_orders(rows) == []
    assert ground_truth.double_charged_orders(rows + [{"order_id": "O-2", "status": "charged_not_delivered"}]) == ["O-2"]


def test_q3_literal():
    assert ground_truth.q3(INCIDENT) == {
        "compensated": ["P-1001", "P-1002", "P-1003"],
        "not_compensated": ["P-1004", "P-1005", "P-1006", "P-1007"],
        "not_compensated_and_awaiting_reply": ["P-1004"],
        "not_compensated_and_silent": ["P-1006", "P-1007"],
    }


def test_topics_and_moments_literal():
    """Every ticket affected players wrote since the start reports the incident; one was answered only after it ended."""
    assert ground_truth.q1_topics(INCIDENT) == {
        "about_incident": ["T-1041", "T-1042", "T-1043", "T-1044"], "other_topics": [],
        "awaiting_about_incident": ["T-1042", "T-1043"], "awaiting_other_topics": [],
    }
    assert ground_truth.q1_moment(INCIDENT) == {
        "unanswered_at_incident_end": ["T-1042", "T-1043", "T-1044"],          # T-1044: created 19:22, answered 19:50, incident ended 19:25
        "awaiting_first_response_now": ["T-1042", "T-1043"],
    }
    quiet = "INC-2026-11-12-NA1"
    assert ground_truth.q1_topics(quiet) == {"about_incident": [], "other_topics": [], "awaiting_about_incident": [], "awaiting_other_topics": []}
    assert ground_truth.q1_moment(quiet) == {"unanswered_at_incident_end": [], "awaiting_first_response_now": []}


def test_owed_purchase_by_purchase_literal():
    assert ground_truth.q3_owed(INCIDENT) == {
        "ledger_checked": True,
        "undelivered_purchase_ids": ["PU-5001", "PU-5002", "PU-5003"],
        "settled": {"PU-5001": ["C-9003"]},                                    # Battle Pass S7, re-delivery recorded on 19 November
        "owed": {"PU-5002": "P-1004", "PU-5003": "P-1007"},                    # 500 Gems; Legendary Skin: Ember
        "owed_players_awaiting_reply": ["P-1004"],                             # P-1007 has not written at all
        "grants_without_purchase": {"P-1001": ["C-9001"], "P-1003": ["C-9002"]},
    }


def test_without_a_ledger_nothing_is_called_owed_or_settled():
    assert ground_truth.q3_owed(INCIDENT, ledger=False) == {
        "ledger_checked": False, "undelivered_purchase_ids": ["PU-5001", "PU-5002", "PU-5003"],
        "settled": None, "owed": None, "owed_players_awaiting_reply": None, "grants_without_purchase": None,
    }


def test_ticket_resolution_literals():
    assert ground_truth.q2_resolution("T-1042") == {
        "player_id": "P-1002", "undelivered_purchase_ids": ["PU-5001"],
        "related_compensation_ids": ["C-9003"], "other_grant_ids": [], "still_owed_purchase_ids": [],
    }
    assert ground_truth.q2_resolution("T-1043") == {
        "player_id": "P-1004", "undelivered_purchase_ids": ["PU-5002"],
        "related_compensation_ids": [], "other_grant_ids": [], "still_owed_purchase_ids": ["PU-5002"],
    }
    assert ground_truth.q2_resolution("T-1045")["player_id"] is None            # unknown requester: nothing to resolve
