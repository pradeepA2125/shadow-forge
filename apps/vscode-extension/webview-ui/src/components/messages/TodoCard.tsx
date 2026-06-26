import { Icon } from "../Icon";
import { CardShell } from "../shared/CardShell";
import type { TodoItem } from "../../types";

type Status = TodoItem["status"];

// Per-status title styling. The leading indicator is drawn by StatusPip below.
// Colors come from the violet-accent design tokens so the card matches its siblings.
const TITLE_CLASS: Record<Status, string> = {
  done: "text-text-3 line-through",
  in_progress: "text-text font-medium",
  pending: "text-text-2",
  blocked: "text-text-2",
  cancelled: "text-text-4 line-through",
};

/** Leading status indicator — an icon for terminal states, an accent dot for active,
 * a hollow ring for pending. Mirrors the accent/semantic palette used across cards. */
function StatusPip({ status }: { status: Status }) {
  switch (status) {
    case "done":
      return (
        <span style={{ color: "var(--color-green)" }}>
          <Icon name="check" size={12} />
        </span>
      );
    case "blocked":
      return (
        <span style={{ color: "var(--color-amber)" }}>
          <Icon name="warn" size={12} />
        </span>
      );
    case "cancelled":
      return (
        <span className="text-text-4">
          <Icon name="x" size={11} />
        </span>
      );
    case "in_progress":
      return (
        <span
          className="block h-[9px] w-[9px] rounded-full"
          style={{
            background: "var(--color-accent)",
            boxShadow: "0 0 0 3px var(--accent-bg-2)",
            animation: "pulse 1.6s ease-in-out infinite",
          }}
        />
      );
    default: // pending
      return (
        <span
          className="block h-[9px] w-[9px] rounded-full"
          style={{ border: "1.5px solid var(--color-text-4)" }}
        />
      );
  }
}

/**
 * TodoCard — the controller's live todo ledger, styled to match the card design
 * language (CardShell + violet accent + a progress bar). Read-only; nested items +
 * per-mutation approval are deferred (spec §9); v1 is a flat list.
 */
export function TodoCard({ items }: { items: TodoItem[] }) {
  // cancelled items are listed (audit) but excluded from the progress denominator.
  const counted = items.filter((i) => i.status !== "cancelled");
  const total = counted.length;
  const done = counted.filter((i) => i.status === "done").length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <CardShell
      icon="list"
      title="Todo"
      trailing={
        <span className="flex-shrink-0 text-[11px] tabular-nums text-text-3">
          {`${done}/${total}`}
        </span>
      }
    >
      {/* progress bar — accent fill over a recessed track */}
      <div className="px-3 pb-2 pt-0.5">
        <div className="h-1 w-full overflow-hidden rounded-full bg-surface-3">
          <div
            className="h-full rounded-full transition-[width] duration-300 ease-out"
            style={{
              width: `${pct}%`,
              background:
                "linear-gradient(90deg, var(--color-accent-deep), var(--color-accent))",
            }}
          />
        </div>
      </div>

      {/* items */}
      <ul className="flex flex-col border-t border-border py-0.5">
        {items.map((it, idx) => (
          <li key={`${idx}:${it.title}`} className="flex items-start gap-2 px-3 py-[5px]">
            <span className="mt-[2px] flex h-[14px] w-[14px] flex-shrink-0 items-center justify-center">
              <StatusPip status={it.status} />
            </span>
            <span className={`text-[11px] leading-[1.5] ${TITLE_CLASS[it.status]}`}>
              {it.title}
              {it.note && (it.status === "blocked" || it.status === "cancelled") && (
                <span className="text-text-4"> — {it.note}</span>
              )}
            </span>
          </li>
        ))}
      </ul>
    </CardShell>
  );
}
