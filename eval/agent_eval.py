"""Reliability check for the LLM agent.

Runs each question several times and compares the facts the agent reports
with eval/ground_truth.py. Needs both APIs running and a model configured in .env.

    python -m eval.agent_eval --runs 5
    python -m eval.agent_eval --runs 1 --only q1,q2_true
    python -m eval.agent_eval --scenario partial_grant --live --only q2_owed,q3_owed
    python -m eval.agent_eval --scenario unrelated_ticket --only q1_topics,q1_moment

--scenario runs on an isolated copy of both systems with changed data (see
eval/instance.py). The copy starts its own APIs; --live also gives it the live
endpoint through `synapse build --discover`. The expected facts are computed
from the data of the copy, so every case stays valid in every scenario.

The agent answers as usual and is additionally asked to end with a JSON block
of facts, so the comparison does not depend on wording. A case passes only if

* the facts equal the expected values exactly (types included, no extra ids),
* the run finished with an answer,
* the tool trace contains the calls the case requires, and
* where the tools cannot settle the question, the answer says so after `Not verified:`.

A failed model call never loses the runs that already finished: the report is
written in any case, and the series stops after two model errors in a row. The
report records the model, the scenario and a hash of the system prompt, so a
result can always be tied to the prompt it was measured with.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

from agent.copilot import PROMPT_SHA256, answer, find_llm_error
from agent.llm import make_llm
from agent.mcp_hub import McpHub
from agent.servers import ROOT, load_dotenv, load_servers, run_cli, scrub, utf8_stdio
from eval import ground_truth
from eval.scripted_check import COMPENSATIONS_TOOL, INCIDENT, QUIET_INCIDENT, is_player_not_found

FACTS = "After your answer, add one final fenced ```json block with exactly these keys and nothing else: "
MISSING = object()


def cases(has_compensations: bool) -> list[dict]:
    """Demo questions, the facts each answer must contain and the tool calls each run must show.

    needs: systems that must have answered at least one call successfully.
    trace: calls that must appear, each as {"tool", "ok", optional "args", optional "not_found"}.
    """
    q1 = ground_truth.q1(INCIDENT)
    q3 = ground_truth.q3(INCIDENT)
    resolution = ground_truth.q2_resolution("T-1042")
    gems = ground_truth.q2_resolution("T-1043")
    topics = ground_truth.q1_topics(INCIDENT)
    owed = ground_truth.q3_owed(INCIDENT, ledger=has_compensations)
    affected_call = {"tool": "game__list_incident_affected_players", "ok": True, "args": {"incident_id": INCIDENT}}
    purchase_facts = ('{"player_id": "<player id>", "charged_without_delivery": true or false, "evidence_purchase_ids": [purchase ids], '
                      '"compensation_checked": true only if a tool returned compensation records, otherwise false, '
                      '"related_compensation_ids": [compensation ids granted for that purchase], ')
    q1_facts = ('{"affected": [player ids], '
                '"with_tickets": {"<player id>": [ids of every ticket that player wrote since the incident started, whatever it is about]}, '
                '"silent": [player ids of affected players with no such ticket], '
                '"awaiting_first_response": [ids of those tickets that still have no first reply]}')
    both = {"game", "desk"}
    compensations_call = {"tool": COMPENSATIONS_TOOL, "ok": True, "args": {"incident_id": INCIDENT}}
    return [
        {
            "id": "q1",
            "question": f"Incident {INCIDENT}: which players were affected, and which of them have written to support since it started? Who is still waiting for a first reply?",
            "facts": q1_facts, "expected": q1, "needs": both,
            "trace": [affected_call],
        },
        {
            "id": "q1_empty",
            "question": f"Incident {QUIET_INCIDENT}: which players were affected, and which of them have written to support since it started?",
            "facts": q1_facts, "expected": ground_truth.q1(QUIET_INCIDENT), "needs": {"game"},
            "trace": [{"tool": "game__list_incident_affected_players", "ok": True, "args": {"incident_id": QUIET_INCIDENT}}],
        },
        {
            "id": "q1_topics",
            "question": f"Incident {INCIDENT}: which tickets have affected players written since it started? Which of them report the incident itself, and which are about something else?",
            "facts": ('{"about_incident": [ticket ids that report the incident], '
                      '"other_topics": [ids of tickets affected players wrote since the start about something else], '
                      '"awaiting_about_incident": [ticket ids that report the incident and still have no first reply]}'),
            "expected": {key: topics[key] for key in ("about_incident", "other_topics", "awaiting_about_incident")},
            "needs": both, "trace": [affected_call],
        },
        {
            "id": "q1_moment",
            "question": f"Incident {INCIDENT}: which tickets from affected players had no first reply at the moment the incident ended, and which still have none now?",
            "facts": '{"unanswered_at_incident_end": [ticket ids], "awaiting_first_response_now": [ticket ids]}',
            "expected": ground_truth.q1_moment(INCIDENT), "needs": both, "trace": [affected_call],
        },
        {
            "id": "q2_true",
            "question": "Ticket T-1042: the player says they paid for a Battle Pass and never received it. Were they charged without delivery, and is anything still owed to them now?",
            "facts": purchase_facts + '"still_owed": true or false, or null if compensations could not be checked}',
            "expected": {
                "player_id": "P-1002", "charged_without_delivery": True, "evidence_purchase_ids": resolution["undelivered_purchase_ids"],
                "compensation_checked": has_compensations,
                "related_compensation_ids": resolution["related_compensation_ids"] if has_compensations else [],
                "still_owed": bool(resolution["still_owed_purchase_ids"]) if has_compensations else None,
            },
            "needs": both, "unverified": not has_compensations,
            "trace": [{"tool": "desk__get_ticket", "ok": True, "args": {"ticket_id": "T-1042"}}] + ([compensations_call] if has_compensations else []),
        },
        {
            "id": "q2_false",
            "question": "Ticket T-1047: the player says they were charged twice for the Battle Pass. Is the claim true?",
            "facts": '{"player_id": "<player id>", "claim_confirmed": true or false, "double_charged_order_ids": [order ids]}',
            "expected": {"player_id": "P-1008", "claim_confirmed": False, "double_charged_order_ids": []}, "needs": both,
            "trace": [{"tool": "game__list_player_purchases", "ok": True, "args": {"player_id": "P-1008"}}],
        },
        {
            "id": "q2_double",
            "question": "Ticket T-1051: the player says they were charged twice for the Battle Pass. Is the claim true?",
            "facts": '{"player_id": "<player id>", "claim_confirmed": true or false, "double_charged_order_ids": [order ids]}',
            "expected": {"player_id": "P-1011", "claim_confirmed": True, "double_charged_order_ids": ["O-7011"]}, "needs": both,
            "trace": [{"tool": "game__list_player_purchases", "ok": True, "args": {"player_id": "P-1011"}}],
        },
        {
            "id": "q2_unknown",
            "question": f"Ticket T-1045: can we confirm that this person was hit by incident {INCIDENT}?",
            "facts": '{"player_found": true or false (false if the game backend does not know the requester e-mail)}',
            "expected": {"player_found": False},
            # The right chain ends in an error: the ticket is read, then the game backend reports the address as unknown.
            "needs": {"desk"},
            "trace": [
                {"tool": "desk__get_ticket", "ok": True, "args": {"ticket_id": "T-1045"}},
                {"tool": "game__find_player_by_email", "ok": False, "args": {"email": "ghost.player@example.net"}, "not_found": True},
            ],
        },
        {
            # The trap of the partial_grant scenario: a crash grant to the same player does not settle the purchase.
            "id": "q2_owed",
            "question": "Ticket T-1043: the player says they paid for 500 Gems and never received them. Were they charged without delivery, and is anything still owed to them now?",
            "facts": purchase_facts + ('"other_grant_ids": [ids of compensations this player received for anything other than that purchase], '
                                       '"still_owed": true or false, or null if compensations could not be checked}'),
            "expected": {
                "player_id": "P-1004", "charged_without_delivery": True, "evidence_purchase_ids": gems["undelivered_purchase_ids"],
                "compensation_checked": has_compensations,
                "related_compensation_ids": gems["related_compensation_ids"] if has_compensations else [],
                "other_grant_ids": gems["other_grant_ids"] if has_compensations else [],
                "still_owed": bool(gems["still_owed_purchase_ids"]) if has_compensations else None,
            },
            "needs": both, "unverified": not has_compensations,
            "trace": [{"tool": "desk__get_ticket", "ok": True, "args": {"ticket_id": "T-1043"}}] + ([compensations_call] if has_compensations else []),
        },
        {
            "id": "q3",
            "question": f"Incident {INCIDENT}: which affected players have already been compensated and which have not? Among those not compensated, who is still waiting for a first reply from support?",
            "facts": ('{"can_answer": true or false (false if no tool gives compensation data; then leave the three lists empty), '
                      '"compensated": [ids of affected players with at least one compensation record for this incident, whatever it was granted for], '
                      '"not_compensated": [ids of affected players with none], "not_compensated_and_awaiting_reply": [player ids]}'),
            "expected": (
                {"can_answer": True, "compensated": q3["compensated"], "not_compensated": q3["not_compensated"],
                 "not_compensated_and_awaiting_reply": q3["not_compensated_and_awaiting_reply"]}
                if has_compensations else
                {"can_answer": False, "compensated": [], "not_compensated": [], "not_compensated_and_awaiting_reply": []}
            ),
            "needs": both if has_compensations else {"game"}, "unverified": not has_compensations,
            "trace": [compensations_call] if has_compensations else [],
        },
        {
            "id": "q3_owed",
            "question": (f"Incident {INCIDENT}: which purchases were charged but not delivered because of it? For each one, has it been settled "
                         "or do we still owe it? Which of the players we still owe are waiting for a first reply?"),
            "facts": ('{"ledger_checked": true only if a tool returned compensation records, otherwise false, '
                      '"undelivered_purchase_ids": [purchase ids], '
                      '"settled_purchase_ids": [ids of purchases settled by a compensation granted for that purchase], or null if compensations could not be checked, '
                      '"owed_purchase_ids": [ids of purchases still owed], or null if compensations could not be checked, '
                      '"owed_players_awaiting_reply": [player ids], or null if compensations could not be checked}'),
            "expected": {
                "ledger_checked": has_compensations, "undelivered_purchase_ids": owed["undelivered_purchase_ids"],
                "settled_purchase_ids": sorted(owed["settled"]) if has_compensations else None,
                "owed_purchase_ids": sorted(owed["owed"]) if has_compensations else None,
                "owed_players_awaiting_reply": owed["owed_players_awaiting_reply"],
            },
            "needs": both if has_compensations else {"game"}, "unverified": not has_compensations,
            "trace": [affected_call] + ([compensations_call] if has_compensations else []),
        },
    ]


def extract_facts(text: str) -> dict | None:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", flags=re.S)
    candidates = blocks[::-1] or re.findall(r"(\{.*\})", text or "", flags=re.S)
    for raw in candidates:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _ids(value) -> list[str] | None:
    """A list of id strings, normalised. None if the value is not a list of strings."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return sorted({item.strip().upper() for item in value})


def _diff(key: str, want, have) -> list[str]:
    """Strict, type-aware comparison of one fact. Never raises."""
    absent = have is MISSING or have is None
    shown = "nothing" if have is MISSING else json.dumps(have, ensure_ascii=False, default=str)
    if isinstance(want, bool):
        return [] if isinstance(have, bool) and have == want else [f"{key}: expected {str(want).lower()}, got {shown}"]
    if want is None:
        return [] if absent else [f"{key}: expected null, got {shown}"]
    if isinstance(want, str):
        ok = isinstance(have, str) and have.strip().upper() == want.strip().upper()
        return [] if ok else [f"{key}: expected {want}, got {shown}"]
    if isinstance(want, list):
        got = [] if (absent and not want) else _ids(have)
        if got is None:
            return [f"{key}: expected a list of id strings, got {shown}"]
        return [] if got == _ids(want) else [f"{key}: expected {sorted(want)}, got {shown}"]
    if isinstance(want, dict):
        if absent and not want:
            return []
        if not isinstance(have, dict):
            return [f"{key}: expected an object, got {shown}"]
        got = {str(k).strip().upper(): _ids(v) for k, v in have.items()}
        expected = {k.upper(): _ids(v) for k, v in want.items()}
        return [] if got == expected else [f"{key}: expected {want}, got {shown}"]
    return [f"{key}: unsupported expected value {want!r}"]


def compare(expected: dict, got: dict | None) -> list[str]:
    """Differences between expected and reported facts; an empty list means an exact match."""
    if not isinstance(got, dict):
        return ["no JSON facts block found in the answer"]
    problems: list[str] = []
    for key, want in expected.items():
        try:
            problems += _diff(key, want, got.get(key, MISSING))
        except Exception as exc:                       # a malformed value fails this fact, not the whole series
            problems.append(f"{key}: could not be compared ({type(exc).__name__}: {exc})")
    return problems


def _step_matches(step: dict, wanted: dict) -> bool:
    if step.get("tool") != wanted["tool"] or bool(step.get("ok")) != wanted["ok"]:
        return False
    for name, value in (wanted.get("args") or {}).items():
        if str((step.get("arguments") or {}).get(name, "")).strip().lower() != str(value).lower():
            return False
    if wanted.get("not_found") and not is_player_not_found(step.get("result_preview", "")):
        return False
    return True


def check_trace(case: dict, run: dict) -> list[str]:
    """Calls the case requires but the run does not show."""
    problems = []
    used = set(run.get("systems_used") or [])
    if not case["needs"] <= used:
        problems.append(f"expected successful calls to {sorted(case['needs'])}, got {sorted(used)}")
    for wanted in case.get("trace", []):
        if not any(_step_matches(step, wanted) for step in run.get("steps") or []):
            what = f"{wanted['tool']}({wanted.get('args') or {}})"
            outcome = "a 'player not found' answer" if wanted.get("not_found") else ("success" if wanted["ok"] else "an error")
            problems.append(f"trace: no call {what} ending in {outcome}")
    return problems


def unverified_note(text: str) -> str | None:
    """What the answer lists after its last `Not verified:` label. None if there is no such label."""
    prose = re.sub(r"```.*?```", "", text or "", flags=re.S)
    found = list(re.finditer(r"not[ \t]+verified[^\w\n]*", prose, flags=re.I))
    return prose[found[-1].end():].strip(" \t\n*_`.:-") if found else None


def check_unverified(case: dict, run: dict) -> list[str]:
    """Where the tools cannot settle the question, an answer must not end with 'Not verified: nothing'."""
    if not case.get("unverified"):
        return []
    note = unverified_note(run.get("answer", ""))
    if note is None:
        return ["the answer has no 'Not verified:' line although the tools cannot settle this question"]
    if not note or re.match(r"(nothing|none|n/a)\b", note, flags=re.I):
        return ["the answer says nothing is unverified although no tool returned compensation records"]
    return []


def evaluate(case: dict, run: dict) -> list[str]:
    problems = []
    if run.get("stop_reason") == "model_error":
        return [f"model call failed: {run.get('error', '')}"]
    if not run.get("completed"):
        problems.append(f"run did not finish: {run.get('stop_reason') or 'unknown'}")
    problems += compare(case["expected"], extract_facts(run.get("answer", "")))
    problems += check_trace(case, run)
    problems += check_unverified(case, run)
    return problems


def run_case(case: dict, mode: str) -> dict:
    """One agent run. Never raises: transport or model failures come back as a run with an error."""
    try:
        return asyncio.run(answer(case["question"], make_llm(), mode=mode, extra_instruction=FACTS + case["facts"]))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        wrapped = find_llm_error(exc)
        reason = "model_error" if wrapped else "crashed"
        return {"question": case["question"], "steps": [], "answer": "", "completed": False, "stop_reason": reason,
                "error": scrub(str(wrapped or f"{type(exc).__name__}: {exc}")), "systems_used": [], "seconds": 0.0, "usage_total": {}}


async def has_tool(mode: str, name: str) -> bool:
    async with McpHub(load_servers(mode)) as hub:
        return name in hub.names()


def series(args) -> int:
    """Run the cases and write the report. Returns the exit code."""
    live = asyncio.run(has_tool(args.mode, COMPENSATIONS_TOOL))
    print(f"stage: {'live' if live else 'baseline'} (compensations tool {'present' if live else 'absent'})\n")
    wanted = {c.strip() for c in args.only.split(",") if c.strip()}
    known = {case["id"] for case in cases(live)}
    if wanted - known:
        raise SystemExit(f"unknown case id(s): {', '.join(sorted(wanted - known))}. Known: {', '.join(sorted(known))}")
    report, failed, aborted, model_errors, usage, model = [], False, False, 0, {}, ""
    try:
        for case in cases(live):
            if (wanted and case["id"] not in wanted) or aborted:
                continue
            passed = 0
            for attempt in range(1, args.runs + 1):
                run = run_case(case, args.mode)
                model = model or run.get("model", "")
                problems = evaluate(case, run)
                for key, value in (run.get("usage_total") or {}).items():
                    usage[key] = usage.get(key, 0) + value
                ok = not problems
                passed += ok
                report.append({"case": case["id"], "attempt": attempt, "passed": ok, "problems": problems, "run": run})
                print(scrub(f"{case['id']:10s} run {attempt}: {'PASS' if ok else 'FAIL'}  {len(run.get('steps') or [])} tool calls, {run.get('seconds', 0)} s"
                            + ("" if ok else "  " + "; ".join(problems))))
                model_errors = model_errors + 1 if run.get("stop_reason") == "model_error" else 0
                if model_errors >= 2:
                    aborted = True
                    print("\nTwo model errors in a row: stopping the series. Finished runs are kept in the report.")
                    break
            failed |= passed != args.runs
            print(f"{case['id']:10s} {passed}/{args.runs}\n")
    finally:
        folder = ROOT / "runs"
        folder.mkdir(exist_ok=True)
        target = folder / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
        header = {"stage": "live" if live else "baseline", "scenario": args.scenario or "committed data", "mode": args.mode,
                  "model": model, "system_prompt_sha256": PROMPT_SHA256, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        target.write_text(scrub(json.dumps({**header, "usage_total": usage, "runs": report},
                                           indent=2, ensure_ascii=False, default=str)), encoding="utf-8")
        if usage:
            print("tokens: " + ", ".join(f"{key}={value}" for key, value in sorted(usage.items())))
        print(f"system prompt: sha256 {PROMPT_SHA256[:12]}")
        print(f"details: {target.relative_to(ROOT)}")
    return 3 if aborted else (1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--mode", choices=["local", "hosted"], default="local")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--scenario", default="", help="run on an isolated copy with changed data (python -m eval.instance lists them)")
    parser.add_argument("--live", action="store_true", help="with --scenario: give the copy the live endpoint (synapse build --discover)")
    args = parser.parse_args()
    utf8_stdio()
    load_dotenv()
    if not args.scenario:
        if args.live:
            raise SystemExit("--live belongs to --scenario. On the committed systems, run scripts/live_update.py apply and build_mcp.py --discover --include-live.")
        sys.exit(series(args))
    if args.mode != "local":
        raise SystemExit("--scenario builds and runs local MCP servers in an isolated copy; use it with --mode local")
    from eval import instance                      # imported here: a plain series needs no copy machinery

    instance.check_name(args.scenario)
    with instance.running(args.scenario, live=args.live) as copy:
        print(f"scenario {copy.scenario}: {instance.SCENARIOS[copy.scenario][0]}")
        code = series(args)
    sys.exit(code)


if __name__ == "__main__":
    run_cli(main)
