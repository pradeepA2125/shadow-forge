from __future__ import annotations

from agentd.domain.models import Diagnostic, TaskRecord


PLAN_SYSTEM_INSTRUCTIONS = (
    "You are AI Editor's deterministic planning engine for code-editing tasks.\n"
    "Your output drives downstream patch generation, so plans must be concrete and executable.\n"
    "\n"
    "Hard requirements:\n"
    "1) Return ONLY a single JSON object that matches the provided schema exactly.\n"
    "2) Do not output markdown, code fences, commentary, or any keys outside schema.\n"
    "3) Use repository-relative file paths in targets.\n"
    "4) Every step must be necessary to reach the goal and safe to execute in order.\n"
    "\n"
    "Planning quality rules:\n"
    "- Prefer small, ordered, implementation-focused steps.\n"
    "- Use explicit technical actions (edit function, add validation, update tests), not vague language.\n"
    "- expected_files should include all files likely to be touched.\n"
    "- stop_conditions should be measurable and validation-oriented.\n"
    "- Risk must be realistic: low for local/safe edits, med/high for cross-cutting or behavior-changing edits.\n"
    "\n"
    "Use retrieval_context to ground the plan in real files/symbols when available."
)

PATCH_SYSTEM_INSTRUCTIONS = (
    "You are AI Editor's deterministic patch generation engine.\n"
    "Generate patch operations that can be executed directly by the patch engine.\n"
    "\n"
    "Hard requirements:\n"
    "1) Return ONLY a single JSON object that matches the provided schema exactly.\n"
    "2) Do not output markdown, code fences, commentary, or any keys outside schema.\n"
    "3) Use repository-relative paths only; never absolute paths and never path traversal.\n"
    "4) Prefer minimal safe edits that satisfy the goal and current diagnostics.\n"
    "\n"
    "Patch operation policy:\n"
    "- Allowed ops are exactly: replace_range, insert_after_symbol, create_file, delete_file.\n"
    "- replace_range: use when replacing known line ranges in existing files.\n"
    "- insert_after_symbol: use only when anchor symbol is expected to exist in that file.\n"
    "- create_file: use for new files only.\n"
    "- delete_file: use only when removal is explicitly required by the goal.\n"
    "\n"
    "Behavior rules:\n"
    "- Respect the task plan and prioritize unresolved diagnostics when provided.\n"
    "- Keep changes cohesive and avoid speculative refactors.\n"
    "- Preserve existing behavior unless the goal explicitly requires behavior change."
)


def build_plan_payload(
    task: TaskRecord,
    *,
    workspace_path: str,
    retrieval_context: dict[str, object],
) -> dict[str, object]:
    return {
        "intent": {
            "task_type": "plan_generation",
            "goal": "Produce an ordered, executable plan for later patch generation.",
        },
        "task_id": task.task_id,
        "goal": task.goal,
        "workspace_path": workspace_path,
        "mode": task.mode,
        "budget": task.budget.model_dump(mode="json"),
        "modified_files": task.modified_files,
        "constraints": {
            "max_files_touched": task.budget.max_files_touched,
            "max_iterations": task.budget.max_iterations,
            "max_tokens": task.budget.max_tokens,
        },
        "output_contract": {
            "required_top_level_fields": [
                "analysis",
                "steps",
                "expected_files",
                "stop_conditions",
            ],
            "step_requirements": [
                "id must be stable and unique within plan",
                "goal must be implementation-focused",
                "targets must be repo-relative paths",
                "risk must be one of low|med|high",
            ],
        },
        "retrieval_context": retrieval_context,
    }


def build_patch_payload(
    task: TaskRecord,
    *,
    workspace_path: str,
    diagnostics: list[Diagnostic],
    retrieval_context: dict[str, object],
) -> dict[str, object]:
    return {
        "intent": {
            "task_type": "patch_generation",
            "goal": "Generate executable patch operations for this task.",
            "mvp_execution_mode": "full-plan single-shot patching",
        },
        "task_id": task.task_id,
        "goal": task.goal,
        "workspace_path": workspace_path,
        "mode": task.mode,
        "plan": task.plan.model_dump(mode="json") if task.plan else None,
        "completed_step_ids": task.completed_step_ids,
        "modified_files": task.modified_files,
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        "constraints": {
            "max_files_touched": task.budget.max_files_touched,
            "max_iterations": task.budget.max_iterations,
            "max_tokens": task.budget.max_tokens,
        },
        "patch_op_catalog": {
            "replace_range": {
                "requires": ["file", "anchor.start_line", "anchor.end_line", "content", "reason"],
                "use_when": "replacing known line ranges in an existing file",
            },
            "insert_after_symbol": {
                "requires": ["file", "anchor.symbol", "content", "reason"],
                "use_when": "inserting text immediately after an existing anchor symbol line",
            },
            "create_file": {
                "requires": ["file", "content", "reason"],
                "use_when": "creating a new file",
            },
            "delete_file": {
                "requires": ["file", "reason"],
                "use_when": "removing an existing file only when necessary",
            },
        },
        "output_contract": {
            "required_top_level_fields": ["patch_ops"],
            "allowed_op_values": [
                "replace_range",
                "insert_after_symbol",
                "create_file",
                "delete_file",
            ],
            "path_rules": [
                "file must be relative to workspace",
                "no absolute paths",
                "no path traversal",
            ],
        },
        "retrieval_context": retrieval_context,
    }
