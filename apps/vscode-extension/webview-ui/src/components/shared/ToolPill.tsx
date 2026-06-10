import { useState } from "react";
import type { ToolEventView } from "../../types";
import { Icon } from "../Icon";
import type { IconName } from "../Icon";

interface Props {
  event: ToolEventView;
}

/** Map tool names to icons. */
function toolIcon(tool: string): IconName {
  const map: Record<string, IconName> = {
    search_code: "search",
    read_file: "file",
    run_command: "term",
    query_graph: "diff",
    list_directory: "list",
    search_semantic: "search",
  };
  return map[tool] ?? "bolt";
}

/**
 * Single tool pill with expand/collapse panel.
 * Matches .pill / .pill.live / .pill.on / .toolpanel in the hi-fi mockup.
 *
 * Running: shimmer-bg gradient with spinner.
 * Done ok: surface bg with green check.
 * Done error: red x with red border.
 * Click (done only): toggles expanded tool panel below.
 */
export function ToolPill({ event }: Props) {
  const [expanded, setExpanded] = useState(false);

  const running = !event.done;
  const isError = event.isError === true;

  const outputLineCount = event.output ? event.output.split("\n").length : 0;

  function handleClick() {
    if (!event.done) return;
    setExpanded((v) => !v);
  }

  // Pill classes
  const pillBase =
    "inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full mono text-[11px] border transition-colors duration-150";

  let pillStyle: React.CSSProperties = {};
  let pillClass = pillBase;

  if (running) {
    pillClass += " shimmer-bg border-[var(--accent-brd)] text-accent-ink cursor-default";
  } else if (expanded) {
    // .pill.on
    pillClass += " border-[var(--accent-brd)] text-accent-ink";
    pillStyle = { background: "var(--accent-bg)" };
  } else if (isError) {
    pillClass += " bg-surface border-[var(--red-brd)] text-text-3 cursor-pointer";
  } else {
    pillClass += " bg-surface border-border text-text-3 cursor-pointer hover:border-border-strong hover:text-text-2";
  }

  const icon = toolIcon(event.tool);

  return (
    <div className="flex flex-col gap-1.5">
      {/* The pill button itself */}
      <button
        type="button"
        className={pillClass}
        style={pillStyle}
        onClick={handleClick}
        aria-expanded={event.done ? expanded : undefined}
      >
        <Icon name={icon} size={10} />
        <span>{event.tool}</span>

        {running && (
          <span
            className="w-[9px] h-[9px] rounded-full border-2 border-t-accent animate-spin"
            style={{ borderColor: "var(--accent-brd)", borderTopColor: "var(--color-accent)" }}
          />
        )}

        {!running && !isError && (
          <Icon name="check" size={9} className="text-green" />
        )}

        {!running && isError && (
          <Icon name="x" size={9} className="text-red" />
        )}

        {event.done && (
          <Icon
            name="chev-d"
            size={9}
            className={[
              "transition-transform duration-150",
              expanded ? "rotate-180" : "",
            ].join(" ")}
          />
        )}
      </button>

      {/* Expanded tool panel */}
      {expanded && event.done && (
        <div
          className="anim-rise rounded-lg border overflow-hidden"
          style={{
            borderColor: "var(--accent-brd)",
            background: "var(--color-surface)",
            boxShadow: "inset 0 1px 0 var(--hairline), 0 8px 20px -10px rgba(0,0,0,.5)",
          }}
        >
          {/* Panel header */}
          <div
            className="flex items-center gap-1.5 px-[11px] py-[7px] border-b border-border"
            style={{ background: "linear-gradient(180deg, var(--accent-bg), transparent)" }}
          >
            <Icon name={icon} size={11} className="text-accent" />
            <span className="mono text-[11px] font-semibold text-accent-ink">{event.tool}</span>

            {event.thought && (
              <span className="text-[11px] italic text-text-3 ml-1">{event.thought}</span>
            )}

            <span className="ml-auto flex items-center gap-2 text-[9.5px] text-text-4">
              {outputLineCount > 0 && (
                <span
                  className="text-[9px] font-semibold text-green px-1.5 py-px rounded-full border"
                  style={{ background: "var(--green-bg)", borderColor: "var(--green-brd)" }}
                >
                  {outputLineCount} lines
                </span>
              )}
              <span>collapse</span>
            </span>
          </div>

          {/* Input section */}
          <div className="px-[11px] py-2 border-b border-border">
            <div className="text-[9px] font-semibold uppercase tracking-widest text-text-4 mb-1.5">
              INPUT
            </div>
            <div className="mono text-[10.5px] leading-[1.8]">
              {Object.entries(event.args).map(([k, v]) => (
                <div key={k}>
                  <span className="text-text-3">{k}:</span>{" "}
                  <span className="text-accent-ink">
                    {typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v)}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Output section */}
          <div className="px-[11px] py-2">
            <div className="text-[9px] font-semibold uppercase tracking-widest text-text-4 mb-1.5">
              OUTPUT
            </div>
            <pre
              className={[
                "mono text-[10.5px] leading-[1.75] max-h-24 overflow-y-auto whitespace-pre-wrap break-all",
                isError ? "text-red" : "text-text-2",
              ].join(" ")}
            >
              {event.output ?? ""}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
