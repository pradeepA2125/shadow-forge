#!/usr/bin/env python3
"""End-to-end driver for the env-profile feature.

Two modes:

  --mode profile-only  (default)
    1. Subscribes SSE on a synthetic channel.
    2. POSTs /v1/workspaces/env-profile with channel_id=<that channel>.
    3. Asserts env_profile_building + env_profile_built fired.
    4. GETs the profile and dumps key fields (ecosystems, diagnostics).
    5. Prints a summary.

  --mode resume-task
    1. Resumes a FAILED task (default: task-ca567078e853) via direct API.
    2. Subscribes SSE on the resulting child task channel.
    3. Auto-approves every gate (plan / step / command / validation).
    4. Tracks env_profile_* and env_install_* events.
    5. Exits when the child task hits a terminal state or our budget runs out.

The script never blocks on user input; every decision route is hit with sensible
defaults. Use --backend to point at a non-default agentd.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from typing import Any

import httpx

DEFAULT_BACKEND = "http://localhost:8000"
ENV_PROFILE_EVENTS = {
    "env_profile_building",
    "env_profile_built",
    "env_install_running",
    "env_install_done",
}


def stamp() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


async def subscribe(client: httpx.AsyncClient, channel_id: str, out: list, stop: asyncio.Event) -> None:
    # Use the permissive /channels endpoint so we can subscribe to any
    # workspace-level channel without requiring it to be a task_id.
    url = f"{client.base_url}/v1/channels/{channel_id}/stream"
    try:
        async with client.stream("GET", url, timeout=None) as resp:
            if resp.status_code != 200:
                print(f"[{stamp()}] SSE subscribe HTTP {resp.status_code} for {channel_id}")
                return
            async for line in resp.aiter_lines():
                if stop.is_set():
                    break
                if not line.startswith("data: "):
                    continue
                try:
                    evt: dict[str, Any] = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                evt["_t"] = time.time()
                out.append(evt)
                t = evt.get("type", "?")
                payload_brief = json.dumps(evt.get("payload", {}), default=str)[:160]
                badge = "* " if t in ENV_PROFILE_EVENTS else "  "
                print(f"[{stamp()}] {badge}SSE {channel_id[:18]:<18} {t:<28} {payload_brief}")
                if t == "done":
                    return
    except (httpx.ReadError, asyncio.CancelledError):
        pass
    except Exception as exc:
        print(f"[{stamp()}] SSE stream error on {channel_id}: {exc}")


async def post(client: httpx.AsyncClient, path: str, *, params: dict | None = None, body: Any = None) -> httpx.Response:
    return await client.post(path, params=params, json=body, timeout=120)


async def get(client: httpx.AsyncClient, path: str, *, params: dict | None = None) -> httpx.Response:
    return await client.get(path, params=params, timeout=30)


async def mode_profile_only(client: httpx.AsyncClient, workspace: str) -> int:
    channel_id = f"demo-profile-{int(time.time())}"
    print(f"[{stamp()}] mode=profile-only workspace={workspace} channel={channel_id}")

    events: list[dict] = []
    stop = asyncio.Event()
    sub = asyncio.create_task(subscribe(client, channel_id, events, stop))
    await asyncio.sleep(0.5)  # let subscriber attach

    t0 = time.time()
    print(f"[{stamp()}] POST /v1/workspaces/env-profile?workspace=...&channel_id={channel_id}")
    r = await post(client, "/v1/workspaces/env-profile",
                   params={"workspace": workspace, "channel_id": channel_id})
    elapsed = time.time() - t0
    print(f"[{stamp()}] POST {r.status_code} took {elapsed:.2f}s")

    if r.status_code != 200:
        print(f"[{stamp()}] body: {r.text[:500]}")
        stop.set()
        sub.cancel()
        return 1

    body = r.json()
    print(f"[{stamp()}] profile.bootstrap_needed = {body.get('bootstrap_needed')}")
    print(f"[{stamp()}] profile.ecosystems       = {len(body.get('ecosystems', []))}")
    for e in body.get("ecosystems", []):
        print(f"          - {e['ecosystem']:<6} subdir={e['subdir']!r:<25} "
              f"pm={e['package_manager']:<6} install={e['install_command']!r:<28} "
              f"interp={e['interpreter_or_runner']!r}")
        print(f"            test={e.get('test_command')!r:<20} deps_top={len(e.get('declared_dependencies_top', []))}")
    print(f"[{stamp()}] profile.conventions_notes = {body.get('conventions_notes')!r}")
    print(f"[{stamp()}] profile.diagnostics:")
    for d in body.get("diagnostics", []):
        print(f"          * {d}")

    # Let SSE catch any late events
    await asyncio.sleep(1.0)
    stop.set()
    sub.cancel()
    try:
        await sub
    except asyncio.CancelledError:
        pass

    print()
    print(f"=== SUMMARY ({time.time() - t0:.2f}s total) ===")
    counts = Counter(e["type"] for e in events)
    for t, c in sorted(counts.items()):
        print(f"  {t:<28} {c}")
    print()

    required = {"env_profile_building", "env_profile_built"}
    missing = required - set(counts)
    if missing:
        print(f"FAIL: expected events not seen: {sorted(missing)}")
        return 2
    print("PASS: env_profile_building + env_profile_built both fired on subscribed channel")
    return 0


async def mode_resume_task(client: httpx.AsyncClient, parent_task_id: str,
                            timeout_sec: int = 1800) -> int:
    print(f"[{stamp()}] mode=resume-task parent={parent_task_id}")
    parent_r = await get(client, f"/v1/tasks/{parent_task_id}")
    if parent_r.status_code != 200:
        print(f"[{stamp()}] FAIL: parent task fetch HTTP {parent_r.status_code}")
        return 1
    print(f"[{stamp()}] parent status: {parent_r.json().get('status')}")

    events: list[dict] = []
    parent_stop = asyncio.Event()
    # Subscribe to parent first so we catch env_profile_* fired by ensure() in
    # resume_from_execute (those go on parent.task_id channel).
    parent_sub = asyncio.create_task(subscribe(client, parent_task_id, events, parent_stop))
    await asyncio.sleep(0.5)

    print(f"[{stamp()}] POST /v1/tasks/{parent_task_id}/resume stage=execute")
    r = await post(client, f"/v1/tasks/{parent_task_id}/resume",
                   body={"stage": "execute"})
    if r.status_code != 200:
        print(f"[{stamp()}] FAIL: resume HTTP {r.status_code} body={r.text[:300]}")
        parent_stop.set()
        parent_sub.cancel()
        return 1
    child_id = r.json().get("task_id")
    print(f"[{stamp()}] child task: {child_id}")

    child_stop = asyncio.Event()
    child_sub = asyncio.create_task(subscribe(client, child_id, events, child_stop))

    t0 = time.time()
    deadline = t0 + timeout_sec
    seen_done = False
    last_status_seen = None

    # Decision driver: react to events as they arrive.
    while time.time() < deadline and not seen_done:
        await asyncio.sleep(2)
        # Catch up on events
        for evt in list(events):
            t = evt.get("type")
            payload = evt.get("payload", {})
            if t == "task_status_changed":
                st = payload.get("status")
                if st != last_status_seen:
                    print(f"[{stamp()}]   >>> status={st}")
                    last_status_seen = st
                if st == "AWAITING_PLAN_APPROVAL":
                    print(f"[{stamp()}]   >>> auto-approving plan")
                    await post(client, f"/v1/tasks/{child_id}/plan/feedback",
                               body={"feedback": None})
                    evt["_handled"] = True
            elif t == "step_review_requested" and not evt.get("_handled"):
                print(f"[{stamp()}]   >>> auto-accepting step {payload.get('step_id')}")
                await post(client, f"/v1/tasks/{child_id}/step-decision",
                           body={"decision": "accept", "step_id": payload.get("step_id")})
                evt["_handled"] = True
            elif t == "command_approval_requested" and not evt.get("_handled"):
                print(f"[{stamp()}]   >>> auto-approving command")
                await post(client, f"/v1/tasks/{child_id}/command-decision",
                           body={"decision_id": payload.get("decision_id"),
                                 "approve": True, "remember": False})
                evt["_handled"] = True
            elif t == "validation_decision_requested" and not evt.get("_handled"):
                print(f"[{stamp()}]   >>> auto-accepting validation")
                await post(client, f"/v1/tasks/{child_id}/validation-decision",
                           body={"accept": True})
                evt["_handled"] = True
            elif t == "scope_extension_requested" and not evt.get("_handled"):
                print(f"[{stamp()}]   >>> auto-approving scope extension")
                await post(client, f"/v1/tasks/{child_id}/scope-decision",
                           body={"decision_id": payload.get("decision_id"),
                                 "approve": True,
                                 "approved_files": payload.get("files", []),
                                 "remember": False})
                evt["_handled"] = True
            elif t == "done":
                seen_done = True

    parent_stop.set()
    child_stop.set()
    parent_sub.cancel()
    child_sub.cancel()
    for tsk in (parent_sub, child_sub):
        try:
            await tsk
        except asyncio.CancelledError:
            pass

    print()
    print(f"=== SUMMARY ({time.time() - t0:.1f}s total) ===")
    counts = Counter(e["type"] for e in events)
    for t, c in sorted(counts.items()):
        marker = " *" if t in ENV_PROFILE_EVENTS else "  "
        print(f"  {marker}{t:<32} {c}")
    print()

    # Fetch final state
    final = await get(client, f"/v1/tasks/{child_id}")
    if final.status_code == 200:
        final_body = final.json()
        print(f"  final status:    {final_body.get('status')}")
        print(f"  resume_of:       {final_body.get('resume_of_task_id')}")

    # Pass/fail criterion: env_profile_built should have fired
    if counts.get("env_profile_built", 0) == 0:
        print("FAIL: env_profile_built never fired on the child task channel")
        return 2
    print("PASS: env-profile lifecycle events observed on resume")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=DEFAULT_BACKEND)
    ap.add_argument("--mode", choices=("profile-only", "resume-task"), default="profile-only")
    ap.add_argument("--workspace", help="workspace path (profile-only mode)")
    ap.add_argument("--task-id", default="task-ca567078e853", help="parent task id (resume-task mode)")
    ap.add_argument("--timeout-sec", type=int, default=1800)
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=args.backend) as client:
        # Health check
        try:
            health = await client.get("/health", timeout=3)
        except httpx.RequestError as exc:
            print(f"[{stamp()}] FAIL: cannot reach backend at {args.backend}: {exc}")
            return 1
        if health.status_code != 200:
            print(f"[{stamp()}] FAIL: /health returned {health.status_code}")
            return 1
        print(f"[{stamp()}] backend up at {args.backend}")

        if args.mode == "profile-only":
            if not args.workspace:
                print("FAIL: --workspace is required for profile-only mode")
                return 1
            return await mode_profile_only(client, args.workspace)
        return await mode_resume_task(client, args.task_id, args.timeout_sec)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
