import { useRef, useEffect, useState } from "react";
import { Icon } from "./Icon";
import { vscode } from "../vscodeApi";
import { parseSlashCommand } from "../slash";
import type { InputAvailability } from "../inputAvailability";

interface Props {
  availability: InputAvailability;
  draft: string;
  onDraftChange: (text: string) => void;
}

// 5 lines × ~19.2px line-height ≈ 96px. Caps the textarea's auto-grow.
const MAX_TEXTAREA_HEIGHT = 96;

/**
 * InputArea — the chat input bar.
 *
 * Auto-grows with content up to ~5 lines. Enter sends; Shift+Enter inserts a
 * newline. When availability.showStop is true, a Stop button appears on the
 * left side of the footer row and posts { type: "stopTurn" } once.
 */
export function InputArea({ availability, draft, onDraftChange }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [stopping, setStopping] = useState(false);
  // One-shot guard for the Tier B task-abort buttons (keep / revert).
  const [aborting, setAborting] = useState(false);
  // Per-task "Review each step" toggle — always sent; the backend applies it
  // only when the turn creates a task (large_change). Default on.
  const [stepReview, setStepReview] = useState(true);

  // Focus on mount and whenever disabled flips to false.
  useEffect(() => {
    if (!availability.disabled) {
      textareaRef.current?.focus();
    }
  }, [availability.disabled]);

  // Reset stopping state when showStop flips back to false (turn ended).
  useEffect(() => {
    if (!availability.showStop) {
      setStopping(false);
    }
  }, [availability.showStop]);

  // Reset abort guard when the task leaves an abortable phase.
  useEffect(() => {
    if (!availability.taskStop) {
      setAborting(false);
    }
  }, [availability.taskStop]);

  // Prompt-file expansion: the host replies to an expandPrompt request with the
  // substituted body, which fills the draft so the user can review/edit and send.
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      const m = e.data as Record<string, unknown>;
      if (m?.["type"] === "promptExpanded" && m["found"] === true) {
        onDraftChange(m["text"] as string);
      }
      // found=false → leave the draft as typed (soft no-op).
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [onDraftChange]);

  function autoGrow() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    onDraftChange(e.target.value);
    autoGrow();
  }

  function doSend() {
    if (availability.disabled) return;
    const trimmed = draft.trim();
    if (!trimmed) return;
    const slash = parseSlashCommand(trimmed);
    if (slash) {
      // Expand first; the host replies with promptExpanded which fills the draft.
      // The user then reviews/edits and sends again (now non-slash → real send).
      vscode.postMessage({ type: "expandPrompt", name: slash.name, args: slash.args });
      return;
    }
    vscode.postMessage({ type: "sendMessage", text: trimmed, stepReview });
    onDraftChange("");
    // Reset height after clearing.
    const el = textareaRef.current;
    if (el) el.style.height = "auto";
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
    // Shift+Enter falls through to default (newline insertion).
  }

  function handleStop() {
    if (stopping) return; // one-shot guard
    setStopping(true);
    vscode.postMessage({ type: "stopTurn" });
  }

  function handleAbort(revert: boolean) {
    if (aborting) return; // one-shot guard
    setAborting(true);
    vscode.postMessage({ type: "abortTask", revert });
  }

  const canSend = !availability.disabled && draft.trim().length > 0;

  return (
    <div
      className={[
        "rounded-[10px] border px-3 pt-2 pb-1.5",
        "transition-opacity duration-150",
        availability.disabled ? "opacity-55" : "opacity-100",
      ].join(" ")}
      style={{
        background: "var(--color-surface)",
        borderColor: "var(--color-border-strong)",
      }}
      // Focus-within ring is applied via inline style on a wrapper trick using
      // onFocusCapture/onBlurCapture to avoid Tailwind v4 focus-within issues.
    >
      {/* Textarea */}
      <textarea
        ref={textareaRef}
        rows={1}
        value={draft}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        disabled={availability.disabled}
        placeholder={availability.placeholder}
        aria-label="Chat input"
        className={[
          "w-full bg-transparent outline-none resize-none",
          "text-xs leading-relaxed text-text",
          "placeholder:text-text-4",
          "disabled:cursor-not-allowed",
        ].join(" ")}
        style={{
          fontFamily: "inherit",
          minHeight: "1.5em",
          maxHeight: MAX_TEXTAREA_HEIGHT,
          overflowY: "auto",
        }}
      />

      {/* Footer row */}
      <div className="flex items-center gap-1.5 pt-1">
        {/* Tier B: task-abort buttons — shown while a task is in an abortable phase.
            "Stop & keep" leaves applied changes; "Stop & revert" rolls the workspace back. */}
        {availability.taskStop && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => handleAbort(false)}
              disabled={aborting}
              aria-label="Stop and keep changes"
              title="Stop the task; keep the changes applied so far"
              className="flex items-center gap-1 h-6 px-2 rounded-[6px] border text-[10px] disabled:opacity-50 disabled:cursor-default"
              style={{
                background: "var(--color-surface-2)",
                borderColor: "var(--color-border-strong)",
                color: "var(--color-text-2)",
              }}
            >
              <Icon name="stop" size={9} />
              Stop &amp; keep
            </button>
            <button
              type="button"
              onClick={() => handleAbort(true)}
              disabled={aborting}
              aria-label="Stop and revert changes"
              title="Stop the task and roll the workspace back to its pre-task state"
              className="flex items-center gap-1 h-6 px-2 rounded-[6px] border text-[10px] disabled:opacity-50 disabled:cursor-default"
              style={{
                background: "var(--color-surface-2)",
                borderColor: "var(--red-brd)",
                color: "var(--color-red)",
              }}
            >
              <Icon name="stop" size={9} />
              Stop &amp; revert
            </button>
          </div>
        )}

        {/* Spacer */}
        <span className="flex-1" />

        {/* Review-each-step toggle */}
        <label className="flex items-center gap-1.5 text-[10px] text-text-3 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={stepReview}
            onChange={(e) => {
              const checked = e.target.checked;
              setStepReview(checked);
              // Live-mutable: a running task re-reads this before each step gate. Checked =
              // "review each step" = auto_accept false. A 409 (no task running) is benign on
              // the extension side — the value still governs the next task's creation default.
              vscode.postMessage({ type: "setReviewPref", autoAccept: !checked });
            }}
            className="accent-[var(--color-accent)] w-3 h-3"
          />
          Review each step
        </label>

        {/* ⌘↵ hint */}
        <span
          className="font-mono text-text-4 select-none"
          style={{ fontSize: "9.5px" }}
        >
          ⌘↵
        </span>

        {/* Right-hand action: Stop while a chat turn streams, otherwise Send. */}
        {availability.showStop ? (
          <button
            type="button"
            onClick={handleStop}
            disabled={stopping}
            aria-label="Stop"
            title="Stop"
            className={[
              "flex items-center justify-center w-6 h-6 rounded-[7px]",
              "transition-all duration-150",
              "disabled:opacity-50 disabled:cursor-default",
            ].join(" ")}
            style={{
              background: "var(--color-surface-2)",
              border: "1px solid var(--red-brd)",
              color: stopping ? "var(--color-text-4)" : "var(--color-red)",
            }}
          >
            <Icon name="stop" size={11} />
          </button>
        ) : (
          <button
            type="button"
            onClick={doSend}
            disabled={!canSend}
            aria-label="Send"
            className={[
              "flex items-center justify-center w-6 h-6 rounded-[7px]",
              "transition-all duration-150",
              "disabled:opacity-40 disabled:cursor-default",
            ].join(" ")}
            style={
              canSend
                ? {
                    background:
                      "linear-gradient(180deg, var(--color-accent-deep), var(--color-accent-hot))",
                    boxShadow:
                      "0 1px 4px rgba(0,0,0,.4), 0 0 12px var(--accent-glow)",
                    color: "#fff",
                  }
                : {
                    background: "var(--color-surface-2)",
                    borderColor: "var(--color-border)",
                    color: "var(--color-text-4)",
                    border: "1px solid var(--color-border)",
                  }
            }
          >
            <Icon name="send" size={12} />
          </button>
        )}
      </div>
    </div>
  );
}
