import { useState } from "react";
import { Icon } from "../Icon";
import { ThinkingBlock } from "../shared/ThinkingBlock";
import { vscode } from "../../vscodeApi";
import type { DiffEntry } from "../../types";

interface Props {
  taskId: string;
  diffEntries: DiffEntry[];
  resolved?: "applied" | "discarded" | null;
  thinkingLog?: string[];
}

/** Returns a color class/style for the file-type dot based on extension. */
function fileDotStyle(path: string): React.CSSProperties {
  if (path.endsWith(".ts") || path.endsWith(".tsx")) {
    return { background: "#3b82f6" };
  }
  if (path.endsWith(".py")) {
    return { background: "var(--color-amber)" };
  }
  return { background: "var(--color-text-4)" };
}

/** Split a path into basename + directory components for display. */
function splitPath(path: string): { base: string; dir: string } {
  const clean = path.endsWith("/") ? path.slice(0, -1) : path;
  const slash = clean.lastIndexOf("/");
  if (slash === -1) return { base: clean, dir: "" };
  return { base: clean.slice(slash + 1), dir: clean.slice(0, slash) };
}

/**
 * DiffCard — inline change result card with file-by-file rows and accept/reject actions.
 * Matches .card / .diff-card / .dstats / .fdot in the hi-fi mockup.
 */
export function DiffCard({ taskId, diffEntries, resolved, thinkingLog }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [localResolved, setLocalResolved] = useState<"applied" | "discarded" | null>(null);

  // Effective resolution: local optimistic state takes priority.
  const effectiveResolved = localResolved ?? resolved ?? null;

  // Aggregate stats.
  const totalAdditions = diffEntries.reduce((s, e) => s + (e.additions ?? 0), 0);
  const totalDeletions = diffEntries.reduce((s, e) => s + (e.deletions ?? 0), 0);
  const fileCount = diffEntries.length;

  function handleAccept() {
    if (effectiveResolved !== null) return; // one-shot guard (UX Rule 2)
    setLocalResolved("applied");
    vscode.postMessage({ type: "applyInlineChange", taskId });
  }

  function handleReject() {
    if (effectiveResolved !== null) return; // one-shot guard (UX Rule 2)
    setLocalResolved("discarded");
    vscode.postMessage({ type: "discardInlineChange", taskId });
  }

  // Border tint by resolution state.
  const borderColor =
    effectiveResolved === "applied"
      ? "var(--green-brd)"
      : effectiveResolved === "discarded"
      ? "var(--red-brd)"
      : expanded
      ? "var(--accent-brd)"
      : "var(--color-border)";

  return (
    <div
      className={[
        "rounded-[10px] overflow-hidden",
        "bg-surface",
        "shadow-[inset_0_1px_0_var(--hairline),0_10px_24px_-14px_rgba(0,0,0,.55)]",
      ].join(" ")}
      style={{ border: `1px solid ${borderColor}`, transition: "border-color 0.2s" }}
    >
      {/* ── Header ── */}
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center gap-2 px-3 py-[9px] cursor-pointer select-none bg-transparent border-0 text-left"
      >
        {/* Icon */}
        <span className="text-accent flex-shrink-0">
          <Icon name="diff" size={13} />
        </span>

        {/* Title */}
        <span className="text-xs font-semibold text-text">Changes ready</span>

        {/* Aggregate +/- stats */}
        <span className="font-mono text-[10px] font-semibold flex items-center gap-1 flex-1 min-w-0">
          <span className="text-green">+{totalAdditions}</span>
          <span className="text-red">&minus;{totalDeletions}</span>
        </span>

        {/* File count badge */}
        <span
          className="text-[9.5px] font-semibold px-[7px] py-[1.5px] rounded-full flex-shrink-0"
          style={{
            color: "var(--color-accent-ink)",
            background: "var(--accent-bg)",
            border: "1px solid var(--accent-brd)",
          }}
        >
          {fileCount} {fileCount === 1 ? "file" : "files"}
        </span>

        {/* Chevron */}
        <span
          className="text-text-4 flex-shrink-0 transition-transform duration-[180ms]"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }}
        >
          <Icon name="chev-d" size={12} />
        </span>
      </button>

      {/* ── Optional ThinkingBlock ── */}
      {thinkingLog && thinkingLog.length > 0 && (
        <div className="px-3 pb-1">
          <ThinkingBlock entries={thinkingLog} />
        </div>
      )}

      {/* ── Body (expanded file rows) ── */}
      {expanded && (
        <div className="anim-rise border-t border-border py-1">
          {diffEntries.map((entry, idx) => {
            const { base, dir } = splitPath(entry.path);
            return (
              <div
                key={`${entry.path}-${idx}`}
                className="flex items-center gap-2 px-3 py-1.5"
              >
                {/* File-type dot */}
                <span
                  className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                  style={fileDotStyle(entry.path)}
                />

                {/* Filename + dir */}
                <span className="flex-1 min-w-0 font-mono text-[11px] flex items-baseline gap-1 overflow-hidden">
                  <span className="text-text-2 flex-shrink-0">{base}</span>
                  {dir && (
                    <span className="text-text-4 truncate">{dir}</span>
                  )}
                </span>

                {/* Per-file stats */}
                <span className="font-mono text-[10px] font-semibold flex items-center gap-1 flex-shrink-0">
                  {entry.additions > 0 && (
                    <span className="text-green">+{entry.additions}</span>
                  )}
                  {entry.deletions > 0 && (
                    <span className="text-red">&minus;{entry.deletions}</span>
                  )}
                </span>

                {/* View diff button — always active (read-only affordance even when resolved) */}
                <button
                  type="button"
                  title="Open diff in editor"
                  onClick={() =>
                    vscode.postMessage({
                      type: "viewDiffFile",
                      path: entry.path,
                      shadowPath: entry.temp_path ?? "",
                    })
                  }
                  className="flex-shrink-0 w-[22px] h-[22px] rounded flex items-center justify-center text-text-3 bg-transparent border border-transparent cursor-pointer hover:border-border-strong hover:text-text-2 transition-colors duration-150"
                >
                  <Icon name="file" size={11} />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Actions row ── */}
      <div className="flex gap-1.5 px-2.5 py-2 border-t border-border">
        {effectiveResolved === null && (
          <>
            <button
              type="button"
              onClick={handleAccept}
              className="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-[6px] rounded-md text-[11px] font-[550] text-white cursor-pointer border border-transparent"
              style={{
                background:
                  "linear-gradient(180deg, var(--color-accent-deep), var(--color-accent-hot))",
                boxShadow:
                  "0 1px 2px rgba(0,0,0,.4), 0 0 16px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,.18)",
              }}
            >
              <Icon name="check" size={11} />
              Accept all
            </button>
            <button
              type="button"
              onClick={handleReject}
              className="inline-flex items-center justify-center px-3 py-[6px] rounded-md text-[11px] font-[550] cursor-pointer bg-transparent border transition-colors duration-150"
              style={{
                color: "var(--color-red)",
                borderColor: "var(--red-brd)",
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background =
                  "var(--red-bg)";
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background =
                  "transparent";
              }}
            >
              Reject
            </button>
          </>
        )}

        {effectiveResolved === "applied" && (
          <div className="flex items-center gap-1.5">
            <span style={{ color: "var(--color-green)" }}>
              <Icon name="check" size={12} />
            </span>
            <span
              className="text-[11px] font-[550]"
              style={{ color: "var(--color-green)" }}
            >
              Applied
            </span>
          </div>
        )}

        {effectiveResolved === "discarded" && (
          <div className="flex items-center gap-1.5">
            <span style={{ color: "var(--color-red)" }}>
              <Icon name="x" size={12} />
            </span>
            <span
              className="text-[11px] font-[550]"
              style={{ color: "var(--color-red)" }}
            >
              Discarded
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
