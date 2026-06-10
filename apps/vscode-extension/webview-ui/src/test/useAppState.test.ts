import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { ExtensionMessage } from "../types";

vi.mock("../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

// Import AFTER mock is set up.
import { useAppState } from "../hooks/useAppState";

// ── Helper ───────────────────────────────────────────────────────────────────

function fireMessage(data: ExtensionMessage): void {
  window.dispatchEvent(new MessageEvent("message", { data }));
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("useAppState", () => {
  // 1. renderThreadList
  it("renderThreadList populates threads and activeThreadId", () => {
    const { result } = renderHook(() => useAppState());

    act(() => {
      fireMessage({
        type: "renderThreadList",
        threads: [{ threadId: "t1", title: "Thread 1", createdAt: "2024-01-01" }],
        activeThreadId: "t1",
      });
    });

    expect(result.current.state.threads).toHaveLength(1);
    expect(result.current.state.threads[0].threadId).toBe("t1");
    expect(result.current.state.activeThreadId).toBe("t1");
  });

  // 2. plan_card dedup
  it("plan_card dedup: identical content collapses; new content appends as second version", () => {
    const { result } = renderHook(() => useAppState());

    const planMsg: ExtensionMessage = {
      type: "appendMessage",
      message: {
        role: "agent",
        content: "## Plan\n- Step 1",
        type: "plan_card",
        taskId: "task-1",
        timestamp: "t",
        metadata: { taskId: "task-1" },
      },
    };

    // Fire the same plan twice — should collapse to one.
    act(() => { fireMessage(planMsg); });
    act(() => { fireMessage(planMsg); });

    expect(result.current.state.messages.filter((m) => m.type === "plan_card")).toHaveLength(1);

    // Fire a plan with different content — should append as a second version.
    act(() => {
      fireMessage({
        type: "appendMessage",
        message: {
          role: "agent",
          content: "## Plan\n- Step 1\n- Step 2",
          type: "plan_card",
          taskId: "task-1",
          timestamp: "t2",
          metadata: { taskId: "task-1" },
        },
      });
    });

    expect(result.current.state.messages.filter((m) => m.type === "plan_card")).toHaveLength(2);
  });

  // 3. streaming chunks accumulate
  it("streaming chunks accumulate correctly", () => {
    const { result } = renderHook(() => useAppState());

    act(() => { fireMessage({ type: "appendChunk", chunk: "Hello" }); });
    act(() => { fireMessage({ type: "appendChunk", chunk: " world" }); });

    expect(result.current.state.streaming?.text).toBe("Hello world");
  });

  // 4. finalizeAgentMessage seals the bubble
  it("finalizeAgentMessage seals the streaming bubble into a persisted agent message", () => {
    const { result } = renderHook(() => useAppState());

    act(() => { fireMessage({ type: "appendChunk", chunk: "Done" }); });
    act(() => { fireMessage({ type: "finalizeAgentMessage" }); });

    expect(result.current.state.streaming).toBeNull();
    const msgs = result.current.state.messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].role).toBe("agent");
    expect(msgs[0].content).toBe("Done");
    expect(msgs[0].type).toBe("text");
    // The sealed message must carry the caller-supplied timestamp (non-empty ISO string).
    expect(typeof (msgs[0] as { timestamp: string }).timestamp).toBe("string");
    expect((msgs[0] as { timestamp: string }).timestamp).not.toBe("");
  });

  // 5. resolveInlineChangeCard patches metadata.resolved
  it("resolveInlineChangeCard patches metadata.resolved of the matching diff_card", () => {
    const { result } = renderHook(() => useAppState());

    act(() => {
      fireMessage({
        type: "appendMessage",
        message: {
          role: "agent",
          content: "",
          type: "diff_card",
          taskId: "inline-task-42",
          timestamp: "t",
          metadata: { taskId: "inline-task-42" },
        },
      });
    });

    act(() => {
      fireMessage({ type: "resolveInlineChangeCard", taskId: "inline-task-42", resolution: "applied" });
    });

    const card = result.current.state.messages.find((m) => m.type === "diff_card");
    expect(card?.metadata.resolved).toBe("applied");
  });

  // 6. thinking chunk-then-entry preserves BOTH
  it("appendThinkingChunk followed by appendThinkingEntry preserves both", () => {
    const { result } = renderHook(() => useAppState());

    act(() => { fireMessage({ type: "appendThinkingChunk", chunk: "loading weights" }); });
    act(() => { fireMessage({ type: "appendThinkingEntry", text: "classified intent" }); });

    expect(result.current.state.streaming?.thinkingEntries).toEqual([
      "loading weights",
      "classified intent",
    ]);
    expect(result.current.state.streaming?.activeThinkingChunk).toBe("");
  });

  // 7. tool event pairing
  it("tool event pairing: appendToolResult marks the matching event done", () => {
    const { result } = renderHook(() => useAppState());

    act(() => {
      fireMessage({
        type: "appendToolEvent",
        event: { id: 1, tool: "read_file", args: { path: "a.ts" }, source: "execution" },
      });
    });
    act(() => {
      fireMessage({ type: "appendToolResult", id: 1, output: "line1", isError: false });
    });
    act(() => {
      fireMessage({
        type: "appendToolEvent",
        event: { id: 2, tool: "search_code", args: { query: "fn" }, source: "execution" },
      });
    });

    const events = result.current.state.streaming?.toolEvents ?? [];
    expect(events).toHaveLength(2);
    expect(events[0].done).toBe(true);
    expect(events[0].output).toBe("line1");
    expect(events[1].done).toBe(false);
  });

  // 8. plan_card messages do NOT seal-append as text
  it("appendMessage plan_card does not generate a phantom text message", () => {
    const { result } = renderHook(() => useAppState());

    act(() => {
      fireMessage({
        type: "appendMessage",
        message: {
          role: "agent",
          content: "## Plan\n- Step 1",
          type: "plan_card",
          taskId: "task-2",
          timestamp: "t",
          metadata: { taskId: "task-2" },
        },
      });
    });

    expect(result.current.state.messages).toHaveLength(1);
    expect(result.current.state.messages[0].type).toBe("plan_card");
  });

  // 9. updateWorkbar + liveStatus
  it("updateWorkbar sets and clears workbar; liveStatus sets liveStatus", () => {
    const { result } = renderHook(() => useAppState());

    act(() => {
      fireMessage({ type: "updateWorkbar", info: { stepIndex: 1, totalSteps: 3, stepTitle: "Step 1" } });
    });
    expect(result.current.state.workbar).toMatchObject({ stepIndex: 1, totalSteps: 3 });

    act(() => { fireMessage({ type: "updateWorkbar", info: null }); });
    expect(result.current.state.workbar).toBeNull();

    act(() => { fireMessage({ type: "liveStatus", status: "EXECUTING" }); });
    expect(result.current.state.liveStatus).toBe("EXECUTING");
  });

  // 10. finalizeAgentMessage with open activeThinkingChunk seals it as a thinking_log entry
  it("finalize with open activeThinkingChunk seals it as a final thinking_log entry in metadata", () => {
    const { result } = renderHook(() => useAppState());

    act(() => { fireMessage({ type: "appendThinkingChunk", chunk: "reasoning step" }); });
    act(() => { fireMessage({ type: "appendChunk", chunk: "Answer" }); });
    act(() => { fireMessage({ type: "finalizeAgentMessage" }); });

    const msg = result.current.state.messages[0];
    expect(msg.metadata.thinking_log).toEqual(["reasoning step"]);
    expect(msg.content).toBe("Answer");
  });
});
