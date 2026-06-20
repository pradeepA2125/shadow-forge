import { useState } from "react";
import { Icon } from "../../Icon";
import { vscode } from "../../../vscodeApi";
import { CardShell } from "../../shared/CardShell";
import { BtnPrimary, BtnGhost } from "../../shared/buttons";

interface Props {
  /** Carries the threadId (controller gates have no task — LiveSlot passes activeTaskId ?? threadId). */
  taskId: string;
  payload: Record<string, unknown>;
}

interface ModeOption {
  mode: string;
  label: string;
  description: string;
}

/** Parse the propose_mode options — tolerates missing or malformed entries. */
function parseOptions(payload: Record<string, unknown>): ModeOption[] {
  if (!Array.isArray(payload.options)) return [];
  return (payload.options as Array<Record<string, unknown>>).map((o) => ({
    mode: String(o.mode ?? ""),
    label: String(o.label ?? o.mode ?? ""),
    description: String(o.description ?? ""),
  }));
}

/**
 * ModeGate — the controller's mode-recommendation gate (mirror of the plan-approval gate).
 *
 * Shows the lightweight `plan_sketch` (the agent's intended approach) plus the
 * recommended mode and alternatives. Picking an option posts a modeDecision; the
 * user can also keep chatting (the "Discuss / refine" path) by typing a message.
 */
export function ModeGate({ taskId, payload }: Props) {
  const planSketch = String(payload.plan_sketch ?? "");
  const reason = String(payload.reason ?? "");
  const recommended = payload.recommended ? String(payload.recommended) : "";
  const options = parseOptions(payload);

  const [resolved, setResolved] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function handlePick(mode: string, label: string) {
    if (resolved !== null) return; // one-shot guard
    setResolved(label);
    vscode.postMessage({ type: "modeDecision", threadId: taskId, mode });
  }

  function handleChatAbout() {
    if (resolved !== null) return; // one-shot — shared with the mode picks
    const text = draft.trim();
    if (!text) return;
    setResolved("Discussing…");
    // A fresh turn supersedes the gate (handle_message clears it at start).
    vscode.postMessage({ type: "sendMessage", text });
  }

  return (
    <CardShell
      icon="bolt"
      title="How should I proceed?"
      subtitle={reason || undefined}
      borderColor="var(--accent-brd)"
      headerTint="linear-gradient(180deg, var(--accent-bg), transparent)"
    >
      {/* ── Approach sketch ── */}
      {planSketch && (
        <div className="px-2.5 py-2 text-[12px] text-text-1 whitespace-pre-wrap border-t border-border">
          {planSketch}
        </div>
      )}

      {/* ── Mode options ── */}
      {resolved === null ? (
        <div className="flex flex-col gap-1.5 px-2.5 py-2 border-t border-border">
          {options.map((opt) => {
            const isRecommended = opt.mode === recommended;
            const inner = (
              <span className="flex flex-col items-start text-left">
                <span>
                  {opt.label}
                  {isRecommended ? " (recommended)" : ""}
                </span>
                {opt.description && (
                  <span className="text-[10px] text-text-2">{opt.description}</span>
                )}
              </span>
            );
            return isRecommended ? (
              <BtnPrimary key={opt.mode} flex onClick={() => handlePick(opt.mode, opt.label)}>
                {inner}
              </BtnPrimary>
            ) : (
              <BtnGhost
                key={opt.mode}
                className="flex-1"
                onClick={() => handlePick(opt.mode, opt.label)}
              >
                {inner}
              </BtnGhost>
            );
          })}
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleChatAbout();
              }
            }}
            placeholder="Chat about this approach…"
            className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-[11px] text-text-1 placeholder:text-text-2"
          />
        </div>
      ) : (
        <div className="flex items-center gap-1.5 px-2.5 py-2 border-t border-border">
          <span style={{ color: "var(--color-green)" }}>
            <Icon name="check" size={12} />
          </span>
          <span className="text-[11px] text-text-2">{resolved}</span>
        </div>
      )}
    </CardShell>
  );
}
