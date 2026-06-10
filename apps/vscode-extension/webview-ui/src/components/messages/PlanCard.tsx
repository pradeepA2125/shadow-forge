import { useState, useRef } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "../Icon";
import { vscode } from "../../vscodeApi";

interface Props {
  content: string;
  taskId: string;
  readOnly?: boolean;
  /** When provided and > 1, renders a "v{version}" violet pill in the header. */
  version?: number;
}

type ActionMode = "idle" | "feedback" | "implementing" | "feedbackSent";

/** Best-effort step count: count common markdown step markers. */
function countSteps(markdown: string): number {
  return markdown.match(/^(#{2,3} |\d+\. |- \[ \] )/gm)?.length ?? 0;
}

/**
 * PlanCard — collapsible plan markdown with implement / give-feedback actions.
 * Matches .card / .plan-card / .steps-wrap / .steps-fade / .fb-row in the hi-fi mockup.
 */
export function PlanCard({ content, taskId, readOnly = false, version }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [mode, setMode] = useState<ActionMode>("idle");
  const [feedbackText, setFeedbackText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const stepCount = countSteps(content);
  const showVersionBadge = typeof version === "number" && version > 1;

  function handleImplement() {
    if (mode !== "idle") return; // one-shot guard (UX Rule 2): a rapid double-click sees stale state
    setMode("implementing");
    vscode.postMessage({ type: "implementPlan", taskId });
  }

  function handleGiveFeedback() {
    setMode("feedback");
    // Focus the input on the next frame after the row renders.
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  function handleCancelFeedback() {
    setMode("idle");
    setFeedbackText("");
  }

  function handleSendFeedback() {
    if (mode !== "feedback") return; // one-shot guard (UX Rule 2)
    const trimmed = feedbackText.trim();
    if (!trimmed) return;
    vscode.postMessage({ type: "planFeedback", taskId, feedback: trimmed });
    setMode("feedbackSent");
  }

  return (
    <div
      className={[
        "rounded-[10px] border overflow-hidden",
        "bg-surface",
        // Hairline inset + soft shadow
        "shadow-[inset_0_1px_0_var(--hairline),0_10px_24px_-14px_rgba(0,0,0,.55)]",
        // Accent border when expanded
        expanded ? "border-[var(--accent-brd)]" : "border-border",
      ].join(" ")}
    >
      {/* ── Header (clickable toggle) ── */}
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center gap-2 px-3 py-[9px] cursor-pointer select-none bg-transparent border-0 text-left"
      >
        {/* Icon */}
        <span className="text-accent flex-shrink-0">
          <Icon name="list" size={13} />
        </span>

        {/* Title */}
        <span className="text-xs font-semibold text-text">Plan</span>

        {/* Step count subtitle */}
        {stepCount > 0 && (
          <span className="text-[11px] text-text-3 flex-1 min-w-0 truncate">
            {stepCount} {stepCount === 1 ? "step" : "steps"}
          </span>
        )}
        {stepCount === 0 && <span className="flex-1" />}

        {/* Version badge */}
        {showVersionBadge && (
          <span
            className="text-[9.5px] font-semibold px-[7px] py-[1.5px] rounded-full flex-shrink-0"
            style={{
              color: "var(--color-accent-ink)",
              background: "var(--accent-bg)",
              border: "1px solid var(--accent-brd)",
            }}
          >
            v{version}
          </span>
        )}

        {/* Chevron */}
        <span
          className="text-text-4 flex-shrink-0 transition-transform duration-[180ms]"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }}
        >
          <Icon name="chev-d" size={12} />
        </span>
      </button>

      {/* ── Body (collapsible markdown) ── */}
      <div
        data-expanded={expanded ? "true" : "false"}
        className="border-t border-border relative overflow-hidden"
        style={{
          maxHeight: expanded ? "60vh" : "102px",
          transition: "max-height 0.28s ease",
          overflowY: expanded ? "auto" : "hidden",
        }}
      >
        {/* Markdown content */}
        <div
          className={[
            "px-3 py-2 text-xs text-text-2 leading-relaxed",
            "[&_code]:font-mono [&_code]:text-code [&_code]:bg-surface-2 [&_code]:px-1 [&_code]:rounded",
            "[&_pre]:font-mono [&_pre]:bg-surface-2 [&_pre]:rounded [&_pre]:p-2 [&_pre]:overflow-x-auto",
            "[&_p]:mb-1.5 [&_p:last-child]:mb-0",
            "[&_ul]:list-disc [&_ul]:pl-4 [&_ul]:mb-1.5",
            "[&_ol]:list-decimal [&_ol]:pl-4 [&_ol]:mb-1.5",
            "[&_h2]:text-[11px] [&_h2]:font-semibold [&_h2]:text-text [&_h2]:mt-2 [&_h2]:mb-1",
            "[&_h3]:text-[11px] [&_h3]:font-semibold [&_h3]:text-text [&_h3]:mt-1.5 [&_h3]:mb-0.5",
          ].join(" ")}
        >
          <ReactMarkdown>{content}</ReactMarkdown>
        </div>

        {/* Fade overlay — hidden when expanded */}
        {!expanded && (
          <div
            data-testid="plan-fade"
            className="absolute bottom-0 left-0 right-0 h-[52px] pointer-events-none"
            style={{
              background: "linear-gradient(transparent, var(--color-surface))",
            }}
          />
        )}
      </div>

      {/* ── Actions row ── */}
      {!readOnly && (
        <>
          {/* idle: Implement + Give feedback */}
          {mode === "idle" && (
            <div className="flex gap-1.5 px-2.5 py-2 border-t border-border">
              <button
                type="button"
                onClick={handleImplement}
                className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-[6px] rounded-md text-[11px] font-[550] text-white cursor-pointer border border-transparent"
                style={{
                  background:
                    "linear-gradient(180deg, var(--color-accent-deep), var(--color-accent-hot))",
                  boxShadow:
                    "0 1px 2px rgba(0,0,0,.4), 0 0 16px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,.18)",
                }}
              >
                <Icon name="bolt" size={11} />
                Implement
              </button>
              <button
                type="button"
                onClick={handleGiveFeedback}
                className="inline-flex items-center justify-center px-3 py-[6px] rounded-md text-[11px] font-[550] text-text-2 cursor-pointer bg-transparent border border-border-strong hover:bg-surface-2 hover:text-text transition-colors duration-150"
              >
                Give feedback
              </button>
            </div>
          )}

          {/* feedback: inline input row */}
          {mode === "feedback" && (
            <div className="flex gap-1.5 px-2.5 py-2 border-t border-border anim-rise">
              <input
                ref={inputRef}
                type="text"
                value={feedbackText}
                onChange={(e) => setFeedbackText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSendFeedback();
                  if (e.key === "Escape") handleCancelFeedback();
                }}
                placeholder="What should change in this plan?"
                className="flex-1 min-w-0 bg-surface-2 border border-border-strong rounded-md px-2.5 py-[6px] text-[11px] text-text outline-none placeholder:text-text-4"
                style={{
                  // Focus ring via inline style to avoid Tailwind focus variant conflicts
                  // handled by onFocus/onBlur approach below via a wrapper class
                }}
              />
              <button
                type="button"
                onClick={handleSendFeedback}
                disabled={feedbackText.trim() === ""}
                className="inline-flex items-center justify-center px-3 py-[6px] rounded-md text-[11px] font-[550] text-white cursor-pointer border border-transparent disabled:opacity-50 disabled:cursor-default"
                style={{
                  background:
                    "linear-gradient(180deg, var(--color-accent-deep), var(--color-accent-hot))",
                  boxShadow:
                    "0 1px 2px rgba(0,0,0,.4), 0 0 16px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,.18)",
                }}
              >
                Send
              </button>
              <button
                type="button"
                onClick={handleCancelFeedback}
                className="inline-flex items-center justify-center px-3 py-[6px] rounded-md text-[11px] font-[550] text-text-2 cursor-pointer bg-transparent border border-border-strong hover:bg-surface-2 hover:text-text transition-colors duration-150"
              >
                Cancel
              </button>
            </div>
          )}

          {/* implementing: one-shot resolved row */}
          {mode === "implementing" && (
            <div className="flex items-center gap-1.5 px-2.5 py-2 border-t border-border">
              <span className="text-green">
                <Icon name="check" size={12} />
              </span>
              <span className="text-[11px] text-text-2">Implementing…</span>
            </div>
          )}

          {/* feedbackSent: one-shot resolved row */}
          {mode === "feedbackSent" && (
            <div className="flex items-center gap-1.5 px-2.5 py-2 border-t border-border">
              <span className="text-accent">
                <Icon name="retry" size={12} />
              </span>
              <span className="text-[11px] text-text-2">
                Regenerating with your feedback…
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
