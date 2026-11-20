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
