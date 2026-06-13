"""Prompts and schema for the Phase 4 ReAct tool-use loop."""
from __future__ import annotations

# Flat schema compatible with Gemini's constrained JSON decoding.
# Gemini does not support oneOf/anyOf discriminated unions — it deadlocks on them.
# All fields are optional except "type" and "thought"; the system prompt instructs
# the model which fields to populate based on the chosen type.
AGENT_STEP_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["tool_call", "emit_patch", "verify_done", "revision_needed"],
            "description": (
                "Action type: tool_call to gather context, emit_patch to write code,"
                " verify_done when checks pass, revision_needed if plan is wrong"
            ),
        },
        "thought": {
            "type": "string",
            "description": "Reasoning before this action (1-3 sentences)",
        },
        # tool_call fields
        "tool": {"type": "string", "description": "Tool name (required for tool_call)"},
        "args": {
            "type": "object",
            "additionalProperties": True,
            "description": "Tool arguments (required for tool_call)",
        },
        # emit_patch fields
        "patch_ops": {
            "type": "array",
            # op/file/reason are required on EVERY patch op (all PatchDocumentV2 op types
            # share them). Constraining items here lets a strict json_schema grammar
            # enforce them at the token level — otherwise the model can omit `reason` and
            # the op only fails later at PatchDocumentV2 validation. Kept flat (no oneOf)
            # so Gemini doesn't deadlock; op-specific fields stay optional via
            # additionalProperties.
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "description": "search_replace | replace_range | apply_diff | create_file | delete_file | replace_node | insert_after_node",  # noqa: E501
                    },
                    "file": {"type": "string", "description": "Target file path"},
                    "reason": {"type": "string", "description": "Why this edit is needed"},
                },
                "required": ["op", "file", "reason"],
                "additionalProperties": True,
            },
            "description": (
                "Patch operations to apply (required for emit_patch):"
                " search_replace, replace_range, apply_diff, create_file, delete_file."
                " Every op MUST include op, file, and reason."
                " MUST cover every file in the step's targets list — no partial patches."
            ),
        },
        # verify_done fields
        "verified": {
            "type": "boolean",
            "description": "True when all linters and tests passed (required for verify_done)",
        },
        "test_output": {
            "type": "string",
            "description": "Full output from the last test/lint run (required for verify_done)",
        },
        "step_summary": {
            "type": "string",
            "description": (
                "One concise sentence summarizing what THIS step changed, for the task "
                "narrative (optional, set it on verify_done)."
            ),
        },
        # revision_needed fields
        "reason": {
            "type": "string",
            "description": "Why the step cannot be completed as planned (required for revision_needed)",  # noqa: E501
        },
        "evidence": {
            "type": "string",
            "description": (
                "Specific evidence from tool calls justifying the revision"
                " (required for revision_needed)"
            ),
        },
        "affected_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Step IDs likely also affected (required for revision_needed)",
        },
    },
    "required": ["type", "thought"],
}

TOOL_LOOP_SYSTEM_PROMPT = """\
You are an expert code editor executing ONE specific step of a multi-step coding plan.

STEP FOCUS:
- step_goal is the only thing to implement — nothing from other steps.
- targets is your patch scope. Reads are never scope-restricted.
- Prior steps are already promoted to the real workspace — to see what they changed, READ the
  file (its current content already includes their edits); do not re-implement their work.
- overall_goal is the full task objective; step_progress lists every step with its status
  (completed/current/pending) so you can see where THIS step fits in the larger plan.
- If the plan looks fundamentally wrong, read to confirm, then emit revision_needed with evidence.

PATCH OPERATION FORMATS (for emit_patch) — pick the op best for the situation; none is preferred:

  {{"op": "search_replace", "file": "path/to/file.py", "search": "exact unique text", "replace": "new text", "reason": "why"}}
    Best for: small, localized edits where you can reproduce the exact, unique surrounding text.

  {{"op": "replace_range", "file": "path/to/file.py", "anchor": {{"start_line": 10, "end_line": 14}}, "content": "new block", "reason": "why"}}
    Best for: replacing a contiguous block by LINE NUMBERS (from read_file's line-numbered output).
    Use it when the text is hard to reproduce exactly (whitespace/quotes) or an anchor keeps not matching.

  {{"op": "apply_diff",     "file": "path/to/file.ext", "diff": "@@ -1,3 +1,4 @@\\n context\\n+added\\n context", "reason": "why"}}
    Best for: multi-line hunk edits that carry surrounding context.

  {{"op": "create_file",    "file": "path/to/new.ext",  "content": "full content", "reason": "why"}}   # new files
  {{"op": "delete_file",    "file": "path/to/file.ext", "reason": "why"}}                               # removed files

EMIT ALL TARGETS: emit_patch must include at least one patch_op for every file in targets.

READ/SEARCH BEHAVIOR:
- Before first patch: reads return real workspace content.
- After first patch: reads automatically switch to shadow workspace (your patched files).
- TARGETED READS/SEARCHES: read_file is capped at 500 lines. DO NOT read files without
  start_line/end_line on large files. Instead, search the code around error symbols
  or lines using search_code first, and then call read_file with start_line and
  end_line parameters to read around those lines. Keep reading and searching
  recursively until you have complete and correct context of the file.
- GRAPH NAVIGATION (query_graph): when changing a symbol, use query_graph to find who depends
  on it before/after patching — cheaper and more precise than grep.
    • query_graph(node="<file>:<Symbol>") → outbound (what it calls, `->`) and inbound
      (who calls it, `<-`) edges with line numbers. Use "Who calls X?" before changing a
      signature; "Where is X defined?" to jump to a callee.
    • query_graph(node="<file>") → distinct files this file connects to, grouped into
      "depends on / connects out" and "used by / connected in". Use to scope a change's blast
      radius across files.
    • edge_kinds filters to any of Calls, Imports, References, Inherits, Implements. "Who
      subclasses/implements X?" → query the base with edge_kinds=["Inherits"] and read inbound
      edges (nominal subclassing only — structural Protocol conformers aren't tracked).
    query_graph tells you WHERE symbols connect, not what they do — pair it with read_file.

PRIOR STEP FILES:
The prior_step_files field lists paths already modified by accepted earlier steps.
Those files are promoted — read_file returns current content. Never create_file over them.

SCOPE VIOLATIONS:
Emit the patch first — the system auto-approves conventional boundary files (__init__.py,
index.ts, mod.rs, conftest.py). If scope is explicitly denied and you cannot proceed,
emit revision_needed citing the missing file and why it is required.

ENV PROFILE (consult this BEFORE guessing interpreter/test/install commands):
  1. Call read_env_profile.
  2. If entries[i].interpreter_or_runner is set, use it directly as the
     command path (e.g. "services/agentd-py/.venv/bin/python"). Do NOT try
     to source activate — tool calls do not persist shell state.
  3. If entries[i].interpreter_or_runner is null, scan diagnostics:
     - *_ABSENT codes (VENV_ABSENT, NODE_MODULES_ABSENT, ...) mean the
       runtime needs bootstrapping. Call setup_env with the entry's
       install_command verbatim and subdir as cwd. The tool output ends
       with "AGENT INFO: interpreter now ready at <path>" on success —
       use that path for run_command next.
     - *_RISK codes (e.g. about manifest layout or build-system config)
       mean the manifest itself needs a quality fix before install can
       succeed. emit_patch the manifest to address the cited issue
       FIRST; the manifest write triggers auto-sync of the install in
       a single round.
  4. Use entries[i].test_command verbatim with entries[i].subdir as cwd.
  5. If the profile is bootstrap_needed=true or returns "not yet built", fall
     back to find_binary / setup_env / init_workspace as below.

BINARY DISCOVERY (when run_command fails with "not found", OR when a binary
runs but its results suggest the workspace env is missing/wrong — e.g. an
import / missing-module / dependency error):
  1. find_binary <name>  — probes workspace bins then PATH; follow any AGENT SHOULD hint.
  2. If found inside the workspace (.venv/bin/, node_modules/.bin/, …): run_command using the resolved path.
  3. If only a system-PATH hit is found AND the workspace has a project manifest, prefer setup_env over the system binary — the system binary will likely lack the project's dependencies.
  4. If a subsequent run_command fails with a missing-module / import / dep error, escalate to setup_env (the workspace env needs bootstrapping or syncing) BEFORE assuming the code is broken.
  5. If not found at all with existing manifest: setup_env "<pm sync command>"
  6. If bare workspace: init_workspace ecosystem=<lang> dev_deps=[...] then setup_env.

init_workspace ecosystems: python / node / rust / go — emits minimal manifest, refuses to
overwrite existing ones. setup_env reads your patched shadow files — deps added via
emit_patch are visible to the very next setup_env call.

If setup_env returns "AGENT SHOULD: emit revision_needed" — do it; toolchain is missing.

OUTPUT — exactly one variant per turn:

Variant 1 — tool call:
  {{"type": "tool_call", "thought": "<reasoning>", "tool": "<name>", "args": {{...}}}}

Variant 2 — patch:
  {{"type": "emit_patch", "thought": "<reasoning — confirm all targets covered>", "patch_ops": [...]}}

Variant 3 — plan error:
  {{"type": "revision_needed", "thought": "...", "reason": "...", "evidence": "...", "affected_steps": [...]}}

Variant 4 — verify complete:
  {{"type": "verify_done", "thought": "...", "verified": true, "test_output": "..."}}
"""


def build_tool_step_payload(
    step_context: dict[str, object],
    history: list[dict[str, object]],
    *,
    state_description: str = "",
) -> dict[str, object]:
    """Build the user_payload dict for a single ReAct loop turn.

    When state_description is provided (verify-phase state machine context),
    it becomes the primary instruction. Explore-phase budget hints kick in
    only when no state description is supplied (back-compat for callers that
    haven't been migrated to the SM-driven path).
    """
    payload: dict[str, object] = {
        "step_goal": step_context.get("goal", ""),
        "targets": step_context.get("targets", []),
        "allowed_files": step_context.get("allowed_files", []),
        "last_failure": step_context.get("last_failure"),
    }

    for field in ("implementation_details", "edge_cases", "design_rationale", "testing_strategy"):
        value = step_context.get(field)
        if value:
            payload[field] = value

    risk = step_context.get("risk")
    if risk and risk != "low":
        payload["risk"] = risk

    file_contents = step_context.get("file_contents")
    if file_contents:
        payload["file_contents"] = file_contents

    prior_step_files = step_context.get("prior_step_files")
    if prior_step_files:
        payload["prior_step_files"] = prior_step_files

    overall_goal = step_context.get("overall_goal")
    if overall_goal:
        payload["overall_goal"] = overall_goal

    step_progress = step_context.get("step_progress")
    if step_progress:
        payload["step_progress"] = step_progress

    diagnostics = step_context.get("diagnostics")
    if diagnostics:
        payload["diagnostics"] = diagnostics

    plan_markdown = step_context.get("plan_markdown")
    if plan_markdown:
        payload["plan_markdown"] = plan_markdown

    if history:
        payload["conversation_history"] = history
        if state_description:
            # SM-driven path: the state description IS the instruction. It tells the
            # model which state it's in, what's available, and what to do next.
            payload["instruction"] = state_description
        else:
            # Back-compat path (no verify-phase SM context wired). Failure-driven
            # hints are kept; iteration-count pressure is NOT — explore until the
            # code is understood, the loop enforces the real ceiling separately.
            recent = [str(m.get("content", "")) for m in history[-6:]]
            patch_fail_count = sum(1 for m in recent if "patch failed" in m.lower() or "not found in" in m)

            if patch_fail_count >= 2:
                payload["instruction"] = (
                    f"⚠ Patch has failed {patch_fail_count} times recently. "
                    "Reading the file before retrying often helps — the content may differ "
                    "from what you expected. Consider a different op type if the current one keeps failing."
                )
            elif patch_fail_count >= 1:
                payload["instruction"] = (
                    "⚠ Last patch failed. The file content may not match your expectations — "
                    "reading it first can help you get the right content before retrying."
                )
            else:
                payload["instruction"] = "Continue exploring until you understand the code, then act."
    else:
        payload["instruction"] = "Start exploring — search or read to understand the code before making changes."

    return payload


def format_tool_system_prompt() -> str:
    return TOOL_LOOP_SYSTEM_PROMPT


def inject_tools_into_payload(
    payload: dict[str, object],
    tool_definitions: list[dict[str, object]],
) -> None:
    """Inject available tool definitions into the per-turn payload.

    Called each turn with only the tools allowed in the current state,
    so the model sees exactly what it can call right now.
    """
    payload["available_tools"] = tool_definitions
