import type { ReactNode } from "react";

export type IconName =
  | "spark" | "search" | "plus" | "clock" | "chev-r" | "chev-l" | "chev-d"
  | "check" | "x" | "copy" | "file" | "term" | "list" | "diff" | "warn"
  | "send" | "stop" | "retry" | "bolt" | "bug";

interface Props {
  name: IconName;
  size?: number;
  className?: string;
}

// Each entry is the inner content of the 16×16 viewBox symbol, converted to JSX.
// Colors remain currentColor so CSS drives them — no hardcoded fills/strokes.
const ICONS: Record<IconName, ReactNode> = {
  spark: (
    <path fill="currentColor" d="M8 1l1.7 4.6L14.5 7 9.7 8.7 8 13.5 6.3 8.7 1.5 7l4.8-1.4L8 1z" />
  ),

  search: (
    <>
      <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </>
  ),

  plus: (
    <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  ),

  clock: (
    <>
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 4.5V8l2.4 1.6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </>
  ),

  "chev-r": (
    <path d="M6 3.5L10.5 8 6 12.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  ),

  "chev-l": (
    <path d="M10 3.5L5.5 8 10 12.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  ),

  "chev-d": (
    <path d="M3.5 6L8 10.5 12.5 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  ),

  check: (
    <path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  ),

  x: (
    <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  ),

  copy: (
    <>
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M3 10.5V3.8C3 3.36 3.36 3 3.8 3h6.7" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </>
  ),

  file: (
    <>
      <path d="M4 1.5h5L13 5.5v8a1 1 0 01-1 1H4a1 1 0 01-1-1V2.5a1 1 0 011-1z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M9 1.5V6h4" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </>
  ),

  term: (
    <>
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M4.5 6l2.5 2-2.5 2M8.5 10.5h3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),

  list: (
    <>
      <path d="M5.5 4h8M5.5 8h8M5.5 12h8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="2.5" cy="4" r="1" fill="currentColor" />
      <circle cx="2.5" cy="8" r="1" fill="currentColor" />
      <circle cx="2.5" cy="12" r="1" fill="currentColor" />
    </>
  ),

  diff: (
    <>
      <path d="M5 2v7M2.5 6.5L5 9l2.5-2.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M11 14V7M8.5 9.5L11 7l2.5 2.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),

  warn: (
    <>
      <path d="M8 2L15 13.5H1L8 2z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M8 6.5v3.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="8" cy="11.8" r=".9" fill="currentColor" />
    </>
  ),

  send: (
    <>
      <path d="M2.5 8L13.5 2.8 11 13.4 7.8 9.6 2.5 8z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M7.8 9.6L13.5 2.8" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </>
  ),

  stop: (
    <rect x="4.5" y="4.5" width="7" height="7" rx="1.2" fill="currentColor" />
  ),

  retry: (
    <>
      <path d="M13.5 8a5.5 5.5 0 11-1.6-3.9" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M13.7 1.8v2.7h-2.7" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),

  bolt: (
    <path d="M8.8 1.5L3.5 9h3.4l-.7 5.5L11.5 7H8.1l.7-5.5z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
  ),

  bug: (
    <>
      <circle cx="8" cy="9" r="3.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M8 5.5V4M5 3l1.2 1.5M11 3L9.8 4.5M3 9h1.5M11.5 9H13M4 12.5l1.3-1.2M12 12.5l-1.3-1.2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </>
  ),
};

export function Icon({ name, size = 12, className }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      className={className}
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  );
}
