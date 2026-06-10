/**
 * Right-aligned user bubble.
 * Matches .ubub in the hi-fi mockup.
 *
 * Inline backtick spans rendered as <code> (mono, text-code) — no markdown engine.
 * Arbitrary border-radius matches the mockup's 12px 12px 4px 12px shape.
 */
export function UserMessage({ content }: { content: string }) {
  const parts = content.split(/(`[^`]+`)/);

  return (
    <div
      className="self-end max-w-[86%] px-3 py-2 text-xs leading-relaxed text-text whitespace-pre-wrap break-words"
      style={{
        background: "linear-gradient(180deg, var(--color-surface-2), var(--color-surface))",
        border: "1px solid var(--color-border-strong)",
        boxShadow: "inset 0 1px 0 var(--hairline)",
        borderRadius: "12px 12px 4px 12px",
      }}
    >
      {parts.map((part, i) => {
        if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
          return (
            <code key={i} className="mono text-code">
              {part.slice(1, -1)}
            </code>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </div>
  );
}
