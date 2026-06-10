import { useReducer, useEffect, useCallback } from "react";
import type { AppState, ExtensionMessage, ChatMsg, StreamingBubble } from "../types";
import { vscode } from "../vscodeApi";

// ── Stable content signatures ────────────────────────────────────────────────

/** djb2 over a string, base36 — stable content signature (also used by LiveSlot keys). */
export function sig(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/** Plan-card version signature: same task + identical content collapses; a new
 *  feedback-regenerated version gets a distinct signature and appends. Mirrors chat.js. */
export function planSig(taskId: string, content: string): string {
  return sig(`${taskId}::${content}`);
}

// ── Initial state ────────────────────────────────────────────────────────────

const INITIAL: AppState = {
  view: "history",
  threads: [],
  activeThreadId: "",
  messages: [],
  streaming: null,
  thinkingStatus: null,
  inputEnabled: true,
  liveGate: null,
  livePlan: null,
  liveReview: null,
  liveError: null,
  workbar: null,
  liveStatus: null,
};

// ── Action types ─────────────────────────────────────────────────────────────

type Action =
  | { type: "EXT"; msg: ExtensionMessage; at: string }
  | { type: "SET_VIEW"; view: "history" | "thread" };

// ── Helpers ──────────────────────────────────────────────────────────────────

function ensureStreaming(state: AppState): StreamingBubble {
  return state.streaming ?? {
    text: "",
    thinkingEntries: [],
    activeThinkingChunk: "",
    toolEvents: [],
  };
}

/**
 * Convert the current streaming bubble into a persisted agent message and
 * append it to messages. If the bubble is completely empty (no text, no
 * thinking entries, no tool events), just clear it without appending.
 */
function sealStreaming(state: AppState, at: string): AppState {
  if (!state.streaming) return state;

  const bubble = state.streaming;

  // Seal any trailing activeThinkingChunk as a final thinking_log entry.
  const entries: string[] = bubble.activeThinkingChunk
    ? [...bubble.thinkingEntries, bubble.activeThinkingChunk]
    : [...bubble.thinkingEntries];

  const isEmpty =
    bubble.text === "" && entries.length === 0 && bubble.toolEvents.length === 0;

  if (isEmpty) {
    return { ...state, streaming: null, thinkingStatus: null };
  }

  const metadata: Record<string, unknown> = {};
  if (entries.length > 0) metadata.thinking_log = entries;
  if (bubble.toolEvents.length > 0) metadata.tool_events = bubble.toolEvents;

  const msg: ChatMsg = {
    role: "agent",
    content: bubble.text,
    type: "text",
    timestamp: at,
    metadata,
  };

  return {
    ...state,
    streaming: null,
    thinkingStatus: null,
    messages: [...state.messages, msg],
  };
}

// ── Reducer ──────────────────────────────────────────────────────────────────

function reducer(state: AppState, action: Action): AppState {
  if (action.type === "SET_VIEW") {
    return { ...state, view: action.view };
  }

  // EXT actions — switch on the extension message type
  const { msg, at } = action;

  switch (msg.type) {
    case "renderThreadList":
      return { ...state, threads: msg.threads, activeThreadId: msg.activeThreadId };

    case "clearThread":
      return {
        ...state,
        messages: [],
        streaming: null,
        thinkingStatus: null,
        workbar: null,
      };

    case "setInputEnabled":
      return { ...state, inputEnabled: msg.enabled };

    case "showThinking":
    case "updateThinking":
      return { ...state, thinkingStatus: msg.message };

    case "hideThinking":
      return { ...state, thinkingStatus: null };

    case "appendChunk": {
      const prev = ensureStreaming(state);
      // If this is the first text and there is an open activeThinkingChunk,
      // seal it into thinkingEntries before appending text.
      // Protocol assumption: thinking chunks always precede response text within a turn; a thinking chunk arriving AFTER text would only be sealed at finalize.
      const updatedEntries =
        prev.text === "" && prev.activeThinkingChunk
          ? [...prev.thinkingEntries, prev.activeThinkingChunk]
          : prev.thinkingEntries;
      const sealedChunk = prev.text === "" && prev.activeThinkingChunk ? "" : prev.activeThinkingChunk;
      return {
        ...state,
        thinkingStatus: null,
        streaming: {
          ...prev,
          text: prev.text + msg.chunk,
          thinkingEntries: updatedEntries,
          activeThinkingChunk: sealedChunk,
        },
      };
    }

    case "appendThinkingEntry": {
      const prev = ensureStreaming(state);
      // Seal any open activeThinkingChunk first, then append the new entry.
      const entries: string[] = prev.activeThinkingChunk
        ? [...prev.thinkingEntries, prev.activeThinkingChunk]
        : [...prev.thinkingEntries];
      return {
        ...state,
        streaming: {
          ...prev,
          thinkingEntries: [...entries, msg.text],
          activeThinkingChunk: "",
        },
      };
    }

    case "appendThinkingChunk": {
      const prev = ensureStreaming(state);
      return {
        ...state,
        streaming: {
          ...prev,
          activeThinkingChunk: prev.activeThinkingChunk + msg.chunk,
        },
      };
    }

    case "appendToolEvent": {
      const prev = ensureStreaming(state);
      return {
        ...state,
        streaming: {
          ...prev,
          toolEvents: [...prev.toolEvents, { ...msg.event, done: false }],
        },
      };
    }

    case "appendToolResult": {
      const prev = ensureStreaming(state);
      return {
        ...state,
        streaming: {
          ...prev,
          toolEvents: prev.toolEvents.map((t) =>
            t.id === msg.id
              ? { ...t, output: msg.output, isError: msg.isError, done: true }
              : t,
          ),
        },
      };
    }

    case "finalizeAgentMessage":
      return sealStreaming(state, at);

    case "appendMessage": {
      // Any persisted message arriving mid-stream implicitly terminates the open bubble — protocol guarantee: the extension finalizes or appends in order, so sealing here is safe for ALL card types.
      const next = sealStreaming(state, at);
      const m = msg.message;

      if (m.type === "plan_card") {
        const taskId = (m.metadata?.taskId as string) ?? m.taskId ?? "";
        const s = planSig(taskId, m.content);
        // Dedup: if the exact same plan version is already in the transcript, skip.
        if (next.messages.some((existing) => existing._sig === s)) {
          return next;
        }
        return {
          ...next,
          messages: [...next.messages, { ...m, _sig: s }],
        };
      }

      if (m.type === "diff_card") {
        const taskId = m.taskId ?? (m.metadata?.taskId as string) ?? "";
        return {
          ...next,
          messages: [...next.messages, { ...m, taskId }],
        };
      }

      return { ...next, messages: [...next.messages, m] };
    }

    case "resolveInlineChangeCard":
      return {
        ...state,
        messages: state.messages.map((m) => {
          if (
            m.type === "diff_card" &&
            (m.taskId === msg.taskId || m.metadata?.taskId === msg.taskId)
          ) {
            return { ...m, metadata: { ...m.metadata, resolved: msg.resolution } };
          }
          return m;
        }),
      };

    case "thread_title_updated":
      return {
        ...state,
        threads: state.threads.map((t) =>
          t.threadId === msg.payload.thread_id
            ? { ...t, title: msg.payload.title }
            : t,
        ),
      };

    case "renderLiveGate":
      return { ...state, liveGate: msg.gate };

    case "clearLiveGate":
      return { ...state, liveGate: null };

    case "renderLivePlan":
      return { ...state, livePlan: msg.plan };

    case "clearLivePlan":
      return { ...state, livePlan: null };

    case "renderLiveReview":
      return { ...state, liveReview: msg.review };

    case "clearLiveReview":
      return { ...state, liveReview: null };

    case "renderLiveError":
      return { ...state, liveError: msg.error };

    case "clearLiveError":
      return { ...state, liveError: null };

    case "updateWorkbar":
      return { ...state, workbar: msg.info };

    case "liveStatus":
      return { ...state, liveStatus: msg.status };

    default:
      return state;
  }
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useAppState() {
  const [state, dispatch] = useReducer(reducer, INITIAL);

  useEffect(() => {
    const handler = (event: MessageEvent<ExtensionMessage>) =>
      dispatch({ type: "EXT", msg: event.data, at: new Date().toISOString() });
    window.addEventListener("message", handler);
    vscode.postMessage({ type: "webviewReady" });
    return () => window.removeEventListener("message", handler);
  }, []);

  const setView = useCallback(
    (view: "history" | "thread") => dispatch({ type: "SET_VIEW", view }),
    [],
  );

  return { state, setView };
}
