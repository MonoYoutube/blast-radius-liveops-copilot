"""The evaluator must not pass wrong answers or fail right ones."""
from eval.agent_eval import _step_matches, cases, check_trace, compare, evaluate, extract_facts
from eval.scripted_check import is_player_not_found

LOCAL_404 = "Error executing tool find_player_by_email: 404 Client Error: Not Found for url: http://127.0.0.1:8101/players/by-email/ghost.player@example.net"
HOSTED_404 = '{"detail":"no player uses the e-mail \'ghost.player@example.net\'"}'


def case(case_id: str, live: bool = False) -> dict:
    return next(c for c in cases(live) if c["id"] == case_id)


def test_fact_extraction_and_exact_match():
    text = 'Answer.\n```json\n{"affected": ["p-1002", "P-1001"], "claim_confirmed": true}\n```'
    facts = extract_facts(text)
    assert compare({"affected": ["P-1001", "P-1002"], "claim_confirmed": True}, facts) == []
    assert compare({"affected": ["P-1001"]}, facts) != []
    assert compare({"can_answer": False}, None) == ["no JSON facts block found in the answer"]
    assert extract_facts("") is None and extract_facts(None) is None


def test_extra_evidence_is_rejected():
    expected = case("q2_true")["expected"]
    padded = dict(expected, evidence_purchase_ids=["PU-5001", "PU-FICTIONAL"])
    assert compare(expected, expected) == []
    assert any("evidence_purchase_ids" in problem for problem in compare(expected, padded))


def test_types_are_compared_strictly():
    assert compare({"can_answer": True}, {"can_answer": 1}) != []                 # 1 is not true
    assert compare({"can_answer": False}, {"can_answer": 0}) != []
    assert compare({"can_answer": False}, {"can_answer": "false"}) != []
    assert compare({"still_owed": None}, {"still_owed": False}) != []             # "unknown" is not "no"
    assert compare({"still_owed": None}, {}) == []
    assert compare({"player_id": "P-1002"}, {"player_id": ["P-1002"]}) != []


def test_malformed_lists_fail_the_fact_without_crashing():
    assert compare({"affected": ["P-1001"]}, {"affected": ["P-1001", 7, None]}) != []
    assert compare({"affected": ["P-1001"]}, {"affected": "P-1001"}) != []
    assert compare({"with_tickets": {"P-1001": ["T-1041"]}}, {"with_tickets": {"P-1001": [1, "T-1041"]}}) != []
    assert compare({"with_tickets": {"P-1001": ["T-1041"]}}, {"with_tickets": ["T-1041"]}) != []
    assert compare({"with_tickets": {"P-1001": ["T-1041"]}}, {"with_tickets": {"p-1001": ["t-1041"]}}) == []


def test_cannot_answer_must_come_with_empty_lists():
    expected = case("q3")["expected"]
    assert expected["can_answer"] is False
    assert compare(expected, {"can_answer": False}) == []                          # lists may be left out
    assert compare(expected, {"can_answer": False, "compensated": [], "not_compensated": None}) == []
    assert compare(expected, {"can_answer": False, "compensated": ["P-1014"]}) != []   # an invented name does not pass


def test_ticket_1042_expects_different_facts_before_and_after_the_live_tool():
    before, after = case("q2_true", live=False), case("q2_true", live=True)
    assert before["expected"]["compensation_checked"] is False and before["expected"]["still_owed"] is None
    assert before["expected"]["related_compensation_ids"] == []
    assert after["expected"]["compensation_checked"] is True and after["expected"]["still_owed"] is False
    assert after["expected"]["related_compensation_ids"] == ["C-9003"]
    assert before["expected"]["evidence_purchase_ids"] == after["expected"]["evidence_purchase_ids"] == ["PU-5001"]
    assert compare(after["expected"], before["expected"]) != []
    tools_after = [wanted["tool"] for wanted in after["trace"]]
    assert "game__list_incident_compensations" in tools_after and "game__list_incident_compensations" not in [w["tool"] for w in before["trace"]]


def test_player_not_found_has_exactly_two_accepted_forms():
    assert is_player_not_found(LOCAL_404)
    assert is_player_not_found(HOSTED_404)
    assert is_player_not_found("Error: " + HOSTED_404)
    for other in (
        "Error executing tool find_player_by_email: 500 Server Error: Internal Server Error for url: http://127.0.0.1:8101/players/by-email/x@y.z",
        "Error executing tool get_incident: 404 Client Error: Not Found for url: http://127.0.0.1:8101/incidents/INC-NOPE",
        "ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=8101): Max retries exceeded",
        "McpError: Connection closed",
        '{"detail":"Internal Server Error"}',
        '{"detail":[{"msg":"Input should be a valid string"}]}',
        "Unknown tool 'game__find_player_by_email'",
        "",
    ):
        assert not is_player_not_found(other), other


def step(tool, ok, arguments, preview=""):
    return {"tool": tool, "ok": ok, "arguments": arguments, "result_preview": preview, "system": tool.split("__")[0]}


def test_unknown_requester_case_needs_the_ticket_and_a_real_not_found():
    unknown = case("q2_unknown")
    ticket = step("desk__get_ticket", True, {"ticket_id": "T-1045"})
    lookup = lambda preview, email="ghost.player@example.net": step("game__find_player_by_email", False, {"email": email}, preview)
    answer = 'No link.\n```json\n{"player_found": false}\n```'
    good = {"completed": True, "stop_reason": "answered", "answer": answer, "systems_used": ["desk"]}

    assert evaluate(unknown, {**good, "steps": [ticket, lookup(LOCAL_404)]}) == []
    assert evaluate(unknown, {**good, "steps": [ticket, lookup(HOSTED_404)]}) == []
    # a transport failure, a different address, or skipping the ticket do not prove the player is unknown
    assert evaluate(unknown, {**good, "steps": [ticket, lookup("ConnectionError: refused")]}) != []
    assert evaluate(unknown, {**good, "steps": [ticket, lookup(LOCAL_404, email="someone.else@example.com")]}) != []
    assert evaluate(unknown, {**good, "steps": [lookup(LOCAL_404)], "systems_used": []}) != []
    assert evaluate(unknown, {**good, "steps": [ticket, lookup(LOCAL_404)], "answer": 'x\n```json\n{"player_found": true}\n```'}) != []


def test_trace_requirements_and_run_state():
    q1 = case("q1")
    wanted = q1["trace"][0]
    assert _step_matches(step(wanted["tool"], True, {"incident_id": "inc-2026-11-18-eu2"}), wanted)      # ids compare case-insensitively
    assert not _step_matches(step(wanted["tool"], False, {"incident_id": "INC-2026-11-18-EU2"}), wanted)
    assert not _step_matches(step(wanted["tool"], True, {"incident_id": "INC-2026-11-05-ASIA1"}), wanted)
    assert check_trace(q1, {"steps": [], "systems_used": []})
    assert evaluate(q1, {"completed": False, "stop_reason": "model_error", "error": "HTTP 401", "steps": []}) == ["model call failed: HTTP 401"]
    unfinished = evaluate(q1, {"completed": False, "stop_reason": "max_rounds", "answer": "", "steps": [], "systems_used": []})
    assert any("did not finish: max_rounds" in problem for problem in unfinished)
