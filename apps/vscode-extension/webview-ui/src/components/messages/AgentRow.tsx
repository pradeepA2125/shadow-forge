import { useState } from "react";
import type { ToolEventView } from "../../types";
import { Avatar } from "../shared/Avatar";
import { ThinkingBlock } from "../shared/ThinkingBlock";
import { ToolPill } from "../shared/ToolPill";
import { Icon } from "../Icon";

interface Props {
  content: string;
  breadcrumb?: boolean;
  thinkingLog?: string[];
  toolEvents?: ToolEventView[];
  streaming?: boolean;
  streamingThinkingEntries?: string[];
  streamingThinkingChunk?: string;
}

/**
 * Generic agent row — handles breadcrumbs, tool pills, streaming content.
 * Matches .turn / .crumb / .stream-line / .caret in the hi-fi mockup.
 *
 * breadcrumb: compact icon+text row, strips leading marker character.
 * normal: plain text, optional streaming caret.
 * Copy button on hover (not while streaming).
 */
export function AgentRow({
  content,
  breadcrumb,
  thinkingLog,
  toolEvents,
  streaming,
  streamingThinkingEntries,
  streamingThinkingChunk,
}: Props) {
  const [copyLabel, setCopyLabel] = useState<"Copy" | "Copied ✓">("Copy");

  const thinkEntries = thinkingLog ?? streamingThinkingEntries ?? [];
  const pills = toolEvents ?? [];

  function handleCopy() {
    const parts = [
      ...pills.map((t) => t.tool + (t.done ? " ok" : " pending")),
      content,
    ].join("\n");
    navigator.clipboard.writeText(parts).catch(() => {
      // Clipboard denied; fail silently.
    });
    setCopyLabel("Copied ✓");
    setTimeout(() => setCopyLabel("Copy"), 1200);
  }

  return (
    <div className="group relative flex gap-2.5 items-start">
      <Avatar />

      <div className="flex-1 min-w-0 flex flex-col gap-2">
        {/* Thinking block */}
        {(thinkEntries.length > 0 || !!streamingThinkingChunk || streaming) && (
          <ThinkingBlock
            entries={thinkEntries}
            activeChunk={streamingThinkingChunk}
            streaming={streaming}
          />
        )}

        {/* Tool pills row */}
        {pills.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {pills.map((event) => (
              <ToolPill key={event.id} event={event} />
            ))}
          </div>
        )}

        {/* Content */}
        {breadcrumb ? (
          <BreadcrumbLine text={content} />
        ) : (
          <div className="text-xs text-text-3 whitespace-pre-wrap">
            {content}
            {streaming && (
              <span
                className="inline-block w-px h-3.5 bg-accent align-middle ml-px"
                style={{ animation: "blink 1s steps(2) infinite" }}
              />
            )}
          </div>
        )}
      </div>

      {/* Copy button on hover — hidden while streaming */}
      {!streaming && (
        <button
          type="button"
          onClick={handleCopy}
          className={[
            "absolute top-0 right-0",
            "opacity-0 group-hover:opacity-100 transition-opacity duration-150",
            "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] text-text-3 cursor-pointer",
            "bg-surface-2 border border-border-strong",
            "hover:text-text hover:border-[var(--accent-brd)]",
          ].join(" ")}
          aria-label="Copy message"
        >
          <Icon name="copy" size={10} />
          {copyLabel}
        </button>
      )}
    </div>
  );
}

// ── Breadcrumb line ────────────────────────────────────────────────────────────

/** Markers and their icon mappings. */
const MARKER_ICONS: Array<{ char: string; icon: "check" | "x" | "retry"; color: string }> = [
  { char: "✓", icon: "check", color: "text-green" },
  { char: "✗", icon: "x", color: "text-red" },
  { char: "↻", icon: "retry", color: "text-accent" },
  { char: "↩", icon: "retry", color: "text-accent" },
];

function BreadcrumbLine({ text }: { text: string }) {
  // Check if the text starts with a known marker character.
  const match = MARKER_ICONS.find((m) => text.startsWith(m.char));
  const displayText = match ? text.slice(match.char.length).trimStart() : text;

  return (
    <div className="flex items-center gap-2 text-[11px] text-text-2">
      {match && (
        <Icon name={match.icon} size={11} className={match.color} />
      )}
      <span>{displayText}</span>
    </div>
  );
}
