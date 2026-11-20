# Demo script (about 4 minutes)

Record with two terminals side by side. Left: `python scripts/run_apis.py` (leave it running). Right: `python scripts/demo.py`, which pauses before every step and prints each command it runs.

| Time | On screen | Say |
|---|---|---|
| 0:00 | Title card, then the architecture diagram from the README | "An outage hits EU-2. The support lead needs to know who was hit, who already wrote in, and what we owe them. Those answers sit in two systems that share nothing but an e-mail address." |
| 0:25 | `systems/game_backend` and `systems/support_desk` in the file tree; `mcp/exposure.json` | "Two separate codebases. Synapse reads each one and generates an MCP server. We expose ten read-only endpoints and deliberately withhold four: resets, erasing a player, replying to a customer." |
| 0:50 | Step 1: `synapse build` output for both systems, then the tool list (10 tools) | "This is the whole integration: one `synapse build` per codebase. The agent's only access to either system is these tools." |
| 1:15 | Step 2: question 1 and the tool trace with `[game]` and `[desk]` lines | "One question, both systems. Seven players affected, four have tickets, two tickets still have no reply. Frostbyte wrote from a linked address and is still matched to the right player." |
| 1:50 | Step 3: ticket T-1042, then T-1047 | "Now the other direction: a ticket makes a claim, the purchase log decides. The Battle Pass claim is confirmed. The double-charge claim is not: one order, one charge." |
| 2:20 | Step 4: the compensation question, answer says it cannot verify | "Ask about compensations and the agent says what it cannot check. No tool exposes that data yet." |
| 2:40 | Step 5: the snippet is added to `incidents.py`; the API answers 200 | "A developer adds one endpoint to the game backend. The API serves it. The agent still cannot see it." |
| 3:00 | Step 6: plain rebuild fails with "Endpoints Not Found" | "A normal rebuild reuses Synapse's last scan, so the new endpoint does not exist for it." |
| 3:10 | Step 7: `synapse build --discover`, 7 tools; tool list shows 11 | "`synapse build --discover` rescans the code. One new tool, the other six untouched." |
| 3:25 | Step 8: the compensation question again, then T-1042 again | "Same question, no restart, complete answer. And the T-1042 answer changes: the pass was already re-delivered yesterday, so we reply instead of compensating twice." |
| 3:50 | `python -m pytest` and `python -m eval.agent_eval` summaries | "Every answer is checked against ground truth computed from the raw data: a scripted check, a test suite, and repeated agent runs." |

Before recording:

1. `python scripts/demo.py --auto --no-llm` must finish with "Done." (no model needed).
2. `python -m eval.agent_eval --runs 1` must pass every case.
3. Increase the terminal font size and clear the scrollback.
