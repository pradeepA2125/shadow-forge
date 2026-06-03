"""End-to-end reachability tests against the LIVE workspace snapshot.

Simulates the planner's path: semantic top-K + keyword matching surface a few
"seed" files; the planner then calls `query_graph` to follow Calls / Imports /
References / Implements / Inherits edges from those seeds. These tests answer
the question the planner answers in production: *from a realistic seed, can
the planner reach the files it needs to modify at depth ≤ 2?*

Each scenario takes the form:
    - `goal`: a one-line task description (rough analogue to what the user
      types in chat).
    - `seeds`: a list of `path` or `path:Symbol` arguments — these stand in
      for the semantic top-K + keyword evidence the planner would receive.
    - `expected_reach`: files the planner MUST be able to reach at depth=2
      to produce a correct plan. Missing one means the plan would forget to
      modify that file.

If the live snapshot isn't present (e.g. CI), each test is skipped — the
data they validate is workspace-specific and can't be embedded in fixtures
without freezing it. Re-run after a fresh `ai-editor-indexer index` pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.retrieval.graph_walker import GraphWalker

_LIVE_WORKSPACE = Path(
    "/Users/pradeepkumar/projects/AI editor/workspaces/shadow-forge-stress"
)
_LIVE_SNAPSHOT = _LIVE_WORKSPACE / ".ai-editor" / "index-snapshot.json"

requires_live_snapshot = pytest.mark.skipif(
    not _LIVE_SNAPSHOT.exists(),
    reason=(
        "Live snapshot not present at "
        f"{_LIVE_SNAPSHOT}; run the indexer first."
    ),
)


def _walker() -> GraphWalker:
    return GraphWalker(_LIVE_SNAPSHOT, _LIVE_WORKSPACE)


def _files_reached(
    walker: GraphWalker,
    seeds: list[str],
    depth: int = 2,
    limit: int = 60,
    edge_kinds: list[str] | None = None,
) -> set[str]:
    """BFS from every seed; return the set of workspace-relative file paths
    touched (matched_roots + every neighbour the walker returns)."""
    reached: set[str] = set()
    for seed in seeds:
        result = walker.query(seed, depth=depth, limit=limit, edge_kinds=edge_kinds)
        for root in result.matched_roots:
            reached.add(root.file)
        for n in result.neighbors:
            reached.add(n.node.file)
    return reached


def _assert_reach(
    scenario_name: str,
    seeds: list[str],
    expected: list[str],
    depth: int = 2,
    limit: int = 60,
) -> None:
    walker = _walker()
    reached = _files_reached(walker, seeds, depth=depth, limit=limit)
    missing = [target for target in expected if target not in reached]
    assert not missing, (
        f"\n{scenario_name}\n"
        f"  seeds (depth={depth}, limit={limit}): {seeds}\n"
        f"  expected to reach: {expected}\n"
        f"  ACTUALLY MISSING: {missing}\n"
        f"  reached ({len(reached)} files): {sorted(reached)[:30]}{'…' if len(reached) > 30 else ''}\n"
    )


# ── Scenarios ────────────────────────────────────────────────────────────────


@requires_live_snapshot
def test_scenario_add_clarification_flow_to_planner() -> None:
    """Goal: "make the planner ask a clarifying question when the goal is vague".

    Semantic seeds: the planning module + the prompts file (these are the
    obvious vector-match results for a query about "planner clarification").
    The planner MUST be able to reach engine.py (where the task lifecycle
    lives) and from there state_machine.py (TaskStatus transitions) and
    routes.py (the HTTP feedback endpoint) — without those it would write a
    plan that forgets to add the new state + route.
    """
    _assert_reach(
        scenario_name="Add clarification flow to planner",
        seeds=[
            "services/agentd-py/agentd/planning/loop.py",
            "services/agentd-py/agentd/planning/agent.py",
            "services/agentd-py/agentd/planning/prompts.py",
        ],
        expected=[
            # Reached via Imports from planning/* (the planner imports from
            # the orchestrator's engine to coordinate the task lifecycle).
            "services/agentd-py/agentd/orchestrator/engine.py",
            # Reached via Calls from the orchestrator's transition() use.
            "services/agentd-py/agentd/domain/state_machine.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_add_a_new_task_status() -> None:
    """Goal: "add AWAITING_FOO to the task status enum and wire its transitions".

    Semantic seed: domain/models.py (where TaskStatus lives — keyword "status"
    + "TaskStatus" land here). The planner needs to find state_machine.py
    (transitions live there) and every call site that calls transition() to
    pause for the new status — at minimum engine.py and routes.py.
    """
    _assert_reach(
        scenario_name="Add a new task status",
        seeds=[
            "services/agentd-py/agentd/domain/models.py",
        ],
        expected=[
            "services/agentd-py/agentd/domain/state_machine.py",
            "services/agentd-py/agentd/orchestrator/engine.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_add_new_planning_tool() -> None:
    """Goal: "add a new read-only tool to the planning agent".

    Semantic seed: planning/registry.py (literally named "registry" for tools)
    and the tool_prompts file. The planner must reach the parallel
    ToolRegistry on the execution side so it knows to register the same tool
    there too (otherwise the agent has the tool during plan but not during
    execute) — and the planning loop / agent file (which builds tool
    definitions into the prompt).
    """
    _assert_reach(
        scenario_name="Add new planning tool",
        seeds=[
            "services/agentd-py/agentd/planning/registry.py",
        ],
        expected=[
            # Both registries should be reachable since they reuse types.
            "services/agentd-py/agentd/tools/registry.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_modify_chat_agent_flow() -> None:
    """Goal: "fix chat agent to also resolve clarification responses".

    Semantic seed: chat/agent.py. The planner must reach the orchestrator
    (which the chat agent calls into to create / continue tasks) and the
    planning registry (the chat explore phase shares the same tools).
    """
    _assert_reach(
        scenario_name="Modify chat agent flow",
        seeds=[
            "services/agentd-py/agentd/chat/agent.py",
        ],
        expected=[
            "services/agentd-py/agentd/orchestrator/engine.py",
            "services/agentd-py/agentd/planning/registry.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_add_verify_phase_state() -> None:
    """Goal: "add a new state to the verify-phase state machine".

    Semantic seed: tools/verify_phase_sm.py and tools/loop.py. The planner
    must reach the reasoning contracts/prompts (the SM's allow_lists
    interlock with the action_type schema) — otherwise it would add a state
    without updating the schema the model is asked to respect.
    """
    _assert_reach(
        scenario_name="Add verify-phase state",
        seeds=[
            "services/agentd-py/agentd/tools/verify_phase_sm.py",
            "services/agentd-py/agentd/tools/loop.py",
        ],
        expected=[
            # tools/loop.py imports reasoning contracts to build the agent
            # step schema; the new state must thread through both.
            "services/agentd-py/agentd/reasoning/contracts.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_add_new_http_route() -> None:
    """Goal: "add POST /v1/tasks/{id}/cancel-with-reason endpoint".

    Semantic seed: api/routes.py. The planner must reach the orchestrator
    (where cancellation actually fires) and the models (the response body
    type and TaskRecord/TaskStatus enum it manipulates).
    """
    _assert_reach(
        scenario_name="Add new HTTP route",
        seeds=[
            "services/agentd-py/agentd/api/routes.py",
        ],
        expected=[
            "services/agentd-py/agentd/orchestrator/engine.py",
            "services/agentd-py/agentd/domain/models.py",
        ],
        depth=2,
        limit=60,
    )


@requires_live_snapshot
def test_scenario_modify_retrieval_client() -> None:
    """Goal: "change how retrieval excludes test files from semantic results".

    Semantic seed: retrieval/artifact_client.py. The planner must reach
    chunker / semantic_index (the chunk types and query primitive) and at
    least one caller (orchestrator/engine.py) to understand how the changes
    propagate to the planning pass.
    """
    _assert_reach(
        scenario_name="Modify retrieval client",
        seeds=[
            "services/agentd-py/agentd/retrieval/artifact_client.py",
        ],
        expected=[
            "services/agentd-py/agentd/retrieval/chunker.py",
        ],
        depth=2,
        limit=60,
    )


# ── A diagnostic that PRINTS the reach instead of asserting ─────────────────
# Useful for tuning seed selection + depth; runs only when explicitly invoked.

@requires_live_snapshot
@pytest.mark.skip(reason="diagnostic-only; remove the skip to print reachability")
def test_diagnostic_print_reach_for_planner_clarification() -> None:
    walker = _walker()
    seeds = [
        "services/agentd-py/agentd/planning/loop.py",
        "services/agentd-py/agentd/planning/prompts.py",
    ]
    for depth in (1, 2, 3):
        reach = _files_reached(walker, seeds, depth=depth, limit=80)
        print(f"\ndepth={depth}: {len(reach)} files")
        for path in sorted(reach):
            print(f"  - {path}")
