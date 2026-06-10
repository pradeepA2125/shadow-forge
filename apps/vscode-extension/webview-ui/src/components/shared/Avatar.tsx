import { Icon } from "../Icon";

/**
 * Shared agent avatar: gradient square with spark icon and violet glow.
 * Matches .avatar in the hi-fi mockup.
 */
export function Avatar() {
  return (
    <div
      className="w-5 h-5 rounded-md flex-shrink-0 mt-px flex items-center justify-center"
      style={{
        background: "linear-gradient(135deg, var(--color-accent-deep), var(--color-accent-hot))",
        boxShadow: "0 0 10px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,.25)",
        color: "#fff",
      }}
      aria-hidden="true"
    >
      <Icon name="spark" size={11} />
    </div>
  );
}
