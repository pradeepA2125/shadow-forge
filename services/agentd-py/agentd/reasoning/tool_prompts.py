"""Prompts and schema for the Phase 4 ReAct tool-use loop."""
from __future__ import annotations

import json

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
            "items": {"type": "object", "additionalProperties": True},
            "description": (
                "Patch operations to apply (required for emit_patch):"
                " search_replace, create_file, apply_diff, delete_file."
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

════════════════════════════════════════════════════════════
  THIS STEP'S GOAL IS YOUR ENTIRE UNIVERSE.
  NOTHING ELSE EXISTS. NOTHING ELSE MATTERS.
════════════════════════════════════════════════════════════

STEP FOCUS RULES — absolute, non-negotiable:

1. YOUR GOAL: The "step_goal" field in your request is the ONLY thing you must implement.
   Do not implement anything from other steps. Do not fix things not broken by this step.

2. YOUR FILES: The "targets" list is your primary scope. Patching files not in targets
   triggers a scope-extension prompt — only do this if your step genuinely requires it.

3. PRIOR-STEP FILES — see PRIOR STEP FILES section below for full details.
   Short version: accepted prior steps are promoted to real workspace, so read_file
   returns their current state. Do not re-implement what they already did.

4. READS ARE NEVER SCOPE-RESTRICTED. You may call read_file, search_code, or
   list_directory on ANY file in the workspace at any time — scope restrictions
   apply only to patch operations (emit_patch). Read freely to understand context.

5. If the plan looks fundamentally wrong (missing dependency, wrong API, impossible
   target), DO NOT guess — read the relevant files to confirm, then emit
   revision_needed with specific evidence from those reads.
   Do not silently re-implement or work around a wrong plan.

AVAILABLE TOOLS:
{tools_json}

PATCH OPERATION FORMATS (for emit_patch):
Each element of patch_ops must be one of these objects:

search_replace — find and replace text in a file (most reliable):
  {{"op": "search_replace", "file": "path/to/file.rs", "search": "exact text to find", "replace": "new text", "reason": "why"}}

create_file — create a new file:
  {{"op": "create_file", "file": "path/to/new_file.ext", "content": "full file content", "reason": "why"}}

apply_diff — apply a unified diff (for multi-section edits):
  {{"op": "apply_diff", "file": "path/to/file.ext", "diff": "@@ -1,3 +1,4 @@\\n context\\n+added line\\n context", "reason": "why"}}

delete_file — delete a file:
  {{"op": "delete_file", "file": "path/to/file.ext", "reason": "why"}}

WHEN search_replace FAILS ("search text not found"):
  NEVER re-submit the same search string — it will fail again for the same reason.
  Escalation order:

  1. read_file the exact line range you want to change → re-emit search_replace using ONLY
     the text returned by that read. One re-read is permitted.

  2. apply_diff — switch to this when search_replace has failed twice on the same location.
     Only ±3 context lines required around each hunk:
       {{"op": "apply_diff", "file": "path/to/file.py",
         "diff": "@@ -45,6 +45,8 @@\n     existing_line\n+    new_line\n     existing_line",
         "reason": "search_replace failed; using diff"}}

  3. create_file — use when apply_diff also fails, or you need to change >30% of the file.
     Read the full file first (read_file start_line=1 to end), then rewrite entirely:
       {{"op": "create_file", "file": "path/to/file.py", "content": "<full updated content>"}}

  One failure → re-read and retry search_replace.
  Two failures → apply_diff.
  Three failures → create_file.
  No exceptions. Never repeat a failing search string.

RULES:
1. FIND THE RIGHT SECTION — target files are always listed in "targets". Your job is to find
   the exact lines you need to change, not the file itself. Follow this decision tree:

   A) You know which symbol (function, class, variable) to modify
      → search_code for the symbol name scoped to the target file → get the line number
      → read_file with start_line/end_line around that line.
        If you know the section is small (a single function), use a tight range (±60 lines).
        If you are uncertain how long the section is, read a wider block (200–300 lines).

   B) search_code returns no results
      → try search_semantic with a natural-language description of the concept.

   C) search_semantic also returns nothing, OR the file is short / new
      → read_file from top (start_line=1, end_line=300).

   WRONG — searching for the file path itself (finds nothing useful):
     {{"type": "tool_call", "tool": "search_code",
       "args": {{"pattern": "services/agentd-py/agentd/__init__.py", "fixed_strings": true}}}}

   WRONG — read_file with no line range on a large file (wastes the 150-line cap):
     {{"type": "tool_call", "tool": "read_file",
       "args": {{"path": "services/agentd-py/agentd/orchestrator/engine.py"}}}}

   RIGHT — search for the symbol to get a line number, then read around it:
     {{"type": "tool_call", "tool": "search_code",
       "args": {{"pattern": "_execute_plan", "path_filter": "**/engine.py", "context_lines": 3}}}}
     → result shows "engine.py:247: async def _execute_plan"
     {{"type": "tool_call", "tool": "read_file",
       "args": {{"path": "services/agentd-py/agentd/orchestrator/engine.py",
                 "start_line": 240, "end_line": 320}}}}

   RIGHT — file is short or new, read from top:
     {{"type": "tool_call", "tool": "read_file",
       "args": {{"path": "services/agentd-py/agentd/__init__.py",
                 "start_line": 1, "end_line": 300}}}}

2. read_file is capped at 500 lines. Provide start_line and end_line.
   Use a tight range when you know the exact location from search_code.
   Use a wider range (200–300 lines) when unsure of a section's length.
   To read a different section: call read_file again with the new range.

3. When you have enough context for every target file, emit_patch. Do not over-search.

4. The search field in search_replace must be an EXACT substring of what read_file
   returns in your current phase (real workspace in explore, shadow in verify).

5. Output exactly one JSON object per turn. The "type" field selects the variant; all fields
   listed for that variant are REQUIRED.

6. EMIT ALL TARGETS — HARD RULE: Your emit_patch MUST include at least one patch_op for
   EVERY file listed in "targets". Before you emit, mentally check the list:
     - For each file in targets: is there a patch_op whose "file" field matches it?
     - If any target is missing: add the ops for it NOW, in the same emit_patch.
   A step that only patches some of its targets is INCOMPLETE and will fail in verify.
   Do NOT emit and expect a follow-up turn to cover the rest — emit everything at once.

EXECUTION PHASES:

Phase 1 — EXPLORE & PATCH
  Gather context with tools, emit_patch when confident.
  After your patch is applied you will automatically enter Phase 2.

Phase 2 — VERIFY
  Phase 2 begins when you see "Patch applied successfully" in the conversation.
  Your next turn's instruction field will also say "You are in VERIFY phase."

  STATIC CHECKS (automatic — no action needed):
    py_compile, ruff, and mypy run automatically after every patch and their results
    appear in the patch-applied message. Fix any failures they report before proceeding.
    Do NOT call run_command for ruff, mypy, or py_compile — they already ran.

  Required sequence:
    1. Check the AUTO-CHECKS block in the patch-applied message. Fix any failures before
       continuing. Use read_file to get current shadow content, then emit_patch to fix.
    2. Run a SCOPED test — NOT the full suite. Derive from touched files + testing_strategy:
         THIS IS A MUST DO STEP ONLY IF TEST FILE WAS CREATED OR MODIFIED BY THIS STEP.
         If your step does not touch any test files, you may skip this step but must explain
         why in the test_output of your final verify_done.
         Python:     pytest tests/test_<module>.py -x -q
         TypeScript: npm exec -- vitest run <matching_spec>
         Rust:       cargo test <module_path>
       If unsure which test file covers your changes: search_code for the function name
       you modified to find the test file, then run that file only.
    3. If any check fails — DIAGNOSE BEFORE PATCHING:
         a. Parse the error output for file path and line number.
         b. Before patching ANY file you created or modified in this step, you MUST
            read its exact current shadow content first.
            Your memory of what you wrote is NOT reliable — the file may have changed
            across multiple patches. Always read before patching. Use read_file or
            search_code — tool definitions:

{read_file_schema_json}

{search_code_schema_json}

         c. Call read_file on the error's reported file at the reported line (±20 lines)
            using start_line and end_line args.
         d. If the error references an undefined name or missing import, also call
            search_code to confirm where the definition lives.
         e. Only then emit_patch with a fix derived from what you just read.
         f. Re-run the same scoped check to confirm the fix.
    4. When all pass: emit verify_done with verified=true and full test_output.

  HARD RULES — verify_done(verified=true) requires ALL of:
    - AUTO-CHECKS (py_compile, ruff, mypy) in the patch-applied message all passed.
    - At least one test command ran and exited 0.
    - Every run_command that returned non-zero or timed out was subsequently re-run
      with a fix applied, and that re-run exited 0.
    - Tool calls that do not execute code (find_binary, list_directory, read_file,
      search_code) do NOT count as verification — they are diagnostic only.

  NEVER run the full test suite (pytest tests/, cargo test, npm test with no path):
    - Full suites time out and contain pre-existing failures unrelated to your change.
    - Always scope: pytest tests/test_foo.py -x -q, cargo test mymod::, vitest run path/
    - If you don't know the test file: use search_code to find it. Never guess a filename.

  TIMEOUTS count as failure:
    - If a run_command returns "timed out", you have NOT verified — scope down and retry.
    - Do NOT call verify_done after a timeout without a subsequent successful run.


BINARY DISCOVERY (verify phase only):

run_command auto-resolves naked binary names against the real workspace's
.venv/bin and node_modules/.bin (where setup_env installs them) — try the
direct command first. CWD is the shadow so your patched files are tested.

When run_command fails with "not found":
  1. find_binary <name>      — probes workspace bins, then PATH; on miss it appends
                                an "AGENT SHOULD: setup_env <cmd>" hint — follow it.
  2. If found:  run_command <name> ...   (or use the full path returned)
  3. If not found and you have an existing manifest: setup_env "<pm sync command>"
  4. If not found and the workspace is bare:  init_workspace + setup_env

WORKSPACE BOOTSTRAPPING:

Bare workspace (no manifest files) — use init_workspace, NOT hand-written manifests:
  init_workspace ecosystem=python dev_deps=["pytest"]
  init_workspace ecosystem=node   dev_deps=["vitest"]
  init_workspace ecosystem=rust   dev_deps=[]
  init_workspace ecosystem=go     dev_deps=[]
init_workspace emits the smallest valid manifest with EXACTLY the deps you list —
no extras. Then call setup_env to install. Refuses to overwrite existing manifests.

Existing workspace — list_directory(".") to detect, then setup_env directly:
  uv.lock / pyproject.toml only / requirements*.txt -> setup_env "uv sync"
                                                       (uv missing -> auto-fallback to
                                                        python3 -m venv + pip; transparent)
  poetry.lock           -> setup_env "poetry install"  (no fallback — needs poetry)
  package-lock.json     -> setup_env "npm ci"
  yarn.lock             -> setup_env "yarn install --frozen-lockfile"
  pnpm-lock.yaml        -> setup_env "pnpm install --frozen-lockfile"
  Cargo.toml            -> cargo must be on PATH (no auto-install); if a component
                           is missing, setup_env "rustup component add <name>"
  go.mod                -> setup_env "go mod download"  (go must be on PATH)

If setup_env returns "AGENT SHOULD: setup_env \"<alt-pm>\"" — follow it (alternate PM).
If setup_env returns "AGENT SHOULD: emit revision_needed" — emit revision_needed,
do NOT retry; the toolchain is genuinely missing and only the user can install it.

setup_env reads YOUR patched files in the shadow workspace. If you added a dep via
emit_patch (or init_workspace), the very next setup_env call sees it.

Concrete example (bare Python workspace):
  list_directory(".")           -> src/  (no manifest, no .venv)
  init_workspace ecosystem=python dev_deps=["pytest"]
                                -> Created pyproject.toml with 1 dep
  setup_env "uv sync"           -> if uv on PATH: uv installs into /real/.venv
                                   if uv missing: note: bootstrapped via python3 + pip
  run_command pytest tests/     -> auto-resolves /real/.venv/bin/pytest -> 1 passed
  verify_done verified=true

Concrete example (cargo missing — non-recoverable):
  list_directory(".")           -> Cargo.toml, src/main.rs
  run_command cargo test        -> Error: 'cargo' not found on PATH
  setup_env "cargo build"       -> Error: 'cargo' not found on PATH. Cannot bootstrap automatically.
                                   Install: https://rustup.rs
                                   AGENT SHOULD: emit revision_needed citing missing toolchain 'cargo'.
  revision_needed               -> reason="missing rust toolchain", evidence="cargo not on PATH"

READ/SEARCH BEHAVIOR BY PHASE — CRITICAL:

Phase 1 (EXPLORE, before first patch):
  read_file and search_code read the real workspace.
  Prior steps that were ACCEPTED have been promoted — their changes ARE visible when you read.
  Once you emit your first patch and Phase 2 begins, reads automatically switch to shadow.

Phase 2 (VERIFY, after first patch applied):
  read_file, search_code, and list_directory automatically switch to reading from
  the SHADOW workspace (all patches visible — prior accepted steps + your current step).
  Use them freely to check what your patch produced and find correct search text for
  follow-up patches. The switch is automatic — you do not need to do anything special.

  If a patch fails in verify phase — DO NOT re-emit immediately. Diagnose first:

  "search text not found":
    1. Call search_code or read_file to find the text as it exists in the shadow.
    2. Re-emit using the exact text you find.

  "search text appears N times (must be unique)":
    1. Call search_code with the ambiguous string to see all occurrences.
    2. Choose a longer surrounding context that is unique to the target location.
    3. Re-emit using that longer string as your search field.

PRIOR STEP FILES:

The "prior_step_files" field lists paths created or modified by earlier accepted steps.
Those changes are promoted to the real workspace — read_file returns current content.
  - NEVER emit create_file for a path in prior_step_files — it already exists.
  - Use read_file normally; your search strings will match the current (promoted) content.
  - Only add what is NEW and NECESSARY for your step. Do not re-implement prior work.

SCOPE VIOLATIONS:
ALWAYS emit_patch first — even when you need files outside your targets.
The system automatically approves conventional package-boundary files
(__init__.py, index.ts, mod.rs, conftest.py, etc.) without interrupting execution.
Never skip the patch attempt because you anticipate a scope issue.

If your patch is rejected and the system explicitly denies scope extension:
  - Implement within your allowed files if possible.
  - Otherwise emit revision_needed citing the missing file and why it is required.
  This is the only case where a scope denial triggers revision_needed.

Plan-correctness revision_needed (wrong API, missing dep, impossible target) does NOT
require a scope denial first — read the relevant files, confirm the problem, then emit.

OUTPUT — choose exactly one variant per turn:

Variant 1 — call a tool (required fields: type, thought, tool, args):
  {{"type": "tool_call", "thought": "<1-3 sentence reasoning>", "tool": "<tool_name>", "args": {{<tool args>}}}}

Variant 2 — emit patch ops (required fields: type, thought, patch_ops):
  {{"type": "emit_patch", "thought": "<final reasoning — MUST state which target files are covered and confirm none are missing>", "patch_ops": [{{<patch op>}}, ...]}}
  CHECKLIST before emitting: (1) list your targets, (2) confirm patch_ops covers each one, (3) add any missing ops.

Variant 3 — signal plan error (required fields: type, thought, reason, evidence, affected_steps):
  {{"type": "revision_needed", "thought": "...", "reason": "...", "evidence": "...", "affected_steps": [...]}}
  Use when: (a) scope extension was explicitly denied and you cannot proceed without the file, OR
            (b) the plan is fundamentally wrong — confirmed by reading the relevant files first.
  evidence must contain specific file content or tool output proving the problem.

Variant 4 — signal verify complete (required fields: type, thought, verified, test_output):
  {{"type": "verify_done", "thought": "...", "verified": true, "test_output": "full pytest or linter output"}}
  Use after ALL linters and tests pass. For non-executable files (docs/config/assets) with no
  testing_strategy, you may emit this immediately with test_output explaining why no checks ran.
"""


def build_tool_step_payload(
    step_context: dict[str, object],
    history: list[dict[str, object]],
    *,
    phase: str = "explore",
) -> dict[str, object]:
    """Build the user_payload dict for a single ReAct loop turn."""
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

    diagnostics = step_context.get("diagnostics")
    if diagnostics:
        payload["diagnostics"] = diagnostics

    plan_markdown = step_context.get("plan_markdown")
    if plan_markdown:
        payload["plan_markdown"] = plan_markdown

    if history:
        payload["conversation_history"] = history
        if phase == "verify":
            verify_iter = len(history) // 2
            if verify_iter >= 3:
                read_rule = (
                    "⚠ MANDATORY — YOU MUST DO THIS BEFORE EVERY emit_patch:\n"
                    "   call read_file on EVERY file you are about to patch.\n"
                    "   Do NOT use memory. Do NOT use text from earlier turns.\n"
                    "   The shadow file has changed since you last read it.\n"
                    "   Only text returned by read_file THIS turn is safe to use as a search string.\n"
                )
            else:
                read_rule = (
                    "RULE: Before every emit_patch, call read_file on the file you will patch "
                    "to get its current shadow content. Never patch from memory.\n"
                )
            payload["instruction"] = (
                f"{read_rule}"
                "\nVERIFY phase sequence:\n"
                "1. AUTO-CHECKS already ran (py_compile, ruff, mypy). Fix any failures\n"
                "   shown in the patch-applied message before proceeding.\n"
                "   Do NOT call run_command for ruff/mypy — they ran automatically.\n"
                "2. TESTS (scoped — NEVER run the full suite):\n"
                "   Derive test file from touched source file using search_code if needed.\n"
                "   Python → pytest tests/test_<module>.py -x -q\n"
                "   TypeScript → vitest run <spec_file>\n"
                "   Rust → cargo test <module_path>\n"
                "3. If a check FAILS:\n"
                "   a. read_file the failing file (shadow content) — get exact current text.\n"
                "   b. For undefined names: search_code to find where the definition lives.\n"
                "   c. emit_patch using only text from step (a). Re-run the same check.\n"
                "4. verify_done(verified=true) only when AUTO-CHECKS and tests both passed.\n"
                "   TIMEOUTS and 'not found on PATH' are failures — fix them, do not skip.\n"
                "   find_binary/list_directory/read_file do NOT count as verification.\n"
                "Reads in verify phase return the SHADOW workspace (your patched files)."
            )
        else:
            iteration = len(history) // 2
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
            elif iteration >= 12:
                payload["instruction"] = (
                    f"⚠ {iteration} tool calls used — pace up. "
                    "Wrap up exploration and move toward your next action."
                )
            elif iteration >= 6:
                payload["instruction"] = (
                    f"Tool calls used: {iteration}. Consider wrapping up exploration soon."
                )
            else:
                payload["instruction"] = "Continue."
    else:
        payload["instruction"] = "Start exploring — search or read to understand the code before making changes."

    return payload


def format_tool_system_prompt(tool_definitions: list[dict[str, object]]) -> str:
    tools_json = json.dumps(tool_definitions, indent=2)
    read_file_def = next((t for t in tool_definitions if t["name"] == "read_file"), {})
    search_code_def = next((t for t in tool_definitions if t["name"] == "search_code"), {})
    return TOOL_LOOP_SYSTEM_PROMPT.format(
        tools_json=tools_json,
        read_file_schema_json=json.dumps(read_file_def, indent=2),
        search_code_schema_json=json.dumps(search_code_def, indent=2),
    )
