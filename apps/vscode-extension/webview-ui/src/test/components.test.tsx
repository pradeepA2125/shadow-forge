import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ToolPill } from "../components/shared/ToolPill";
import { ThinkingBlock } from "../components/shared/ThinkingBlock";
import { AgentRow } from "../components/messages/AgentRow";
import { UserMessage } from "../components/messages/UserMessage";
import type { ToolEventView } from "../types";

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeEvent(overrides: Partial<ToolEventView> = {}): ToolEventView {
  return {
    id: 1,
    tool: "read_file",
    args: { path: "src/foo.ts", start_line: 10 },
    source: "execution",
    done: false,
    ...overrides,
  };
}

// ── 1. ToolPill running ───────────────────────────────────────────────────────

describe("ToolPill", () => {
  it("running: shows tool name, no panel toggle arrow hint", () => {
    const event = makeEvent({ done: false });
    render(<ToolPill event={event} />);
    expect(screen.getByText("read_file")).toBeTruthy();
    // Panel must not be visible since not done.
    expect(screen.queryByText(/INPUT/i)).toBeNull();
  });

  it("done: click pill opens panel with INPUT key/value and OUTPUT", () => {
    const event = makeEvent({
      done: true,
      isError: false,
      args: { path: "src/foo.ts" },
      output: "line 1\nline 2",
    });
    const { container } = render(<ToolPill event={event} />);

    // Panel not visible before click.
    expect(screen.queryByText(/INPUT/)).toBeNull();

    // Click the pill button.
    const pill = container.querySelector("button")!;
    fireEvent.click(pill);

    // Panel should appear.
    expect(screen.getByText("INPUT")).toBeTruthy();
    expect(screen.getByText("OUTPUT")).toBeTruthy();
    // Key in args.
    expect(screen.getByText("path:")).toBeTruthy();
    // Output text.
    expect(screen.getByText(/line 1/)).toBeTruthy();

    // Click again — panel collapses.
    fireEvent.click(pill);
    expect(screen.queryByText(/INPUT/)).toBeNull();
  });

  it("error: renders with output text; isError styling path covered", () => {
    const event = makeEvent({
      done: true,
      isError: true,
      args: { cmd: "npm test" },
      output: "FAIL src/foo.ts",
    });
    render(<ToolPill event={event} />);

    // Pill renders the tool name.
    expect(screen.getByText("read_file")).toBeTruthy();

    // Click to expand and confirm error output renders.
    const pill = screen.getByRole("button");
    fireEvent.click(pill);
    expect(screen.getByText(/FAIL src\/foo.ts/)).toBeTruthy();
  });
});

// ── 2. ThinkingBlock ──────────────────────────────────────────────────────────

describe("ThinkingBlock", () => {
  it("streaming: shows Thinking label", () => {
    render(<ThinkingBlock entries={[]} streaming={true} />);
    expect(screen.getByText(/Thinking/)).toBeTruthy();
  });

  it("idle with entries: shows count, collapsed by default, click expands", () => {
    render(<ThinkingBlock entries={["step one", "step two"]} streaming={false} />);
    expect(screen.getByText("Thinking (2 steps)")).toBeTruthy();

    // Detail not visible before click.
    expect(screen.queryByText("step one")).toBeNull();

    // Click the header pill.
    const btn = screen.getByRole("button");
    fireEvent.click(btn);

    // Entries visible after expand.
    expect(screen.getByText(/step one/)).toBeTruthy();
    expect(screen.getByText(/step two/)).toBeTruthy();
  });

  it("streaming → false: collapses via useEffect", () => {
    const { rerender } = render(
      <ThinkingBlock entries={["loaded weights"]} streaming={true} />
    );
    // Initially open because streaming=true.
    const btn = screen.getByRole("button");
    fireEvent.click(btn); // toggle open (it opens automatically from streaming)
    // Now set streaming=false via rerender.
    rerender(<ThinkingBlock entries={["loaded weights"]} streaming={false} />);
    // After streaming ends, collapses — detail should not be visible.
    expect(screen.queryByText("loaded weights")).toBeNull();
  });
});

// ── 3. AgentRow breadcrumb ────────────────────────────────────────────────────

describe("AgentRow", () => {
  it("breadcrumb: renders text without leading marker character appearing twice", () => {
    render(
      <AgentRow content="✓ Plan approved" breadcrumb={true} />
    );
    const textEl = screen.getByText("Plan approved");
    expect(textEl).toBeTruthy();

    // The literal "✓" should NOT appear in the text node.
    expect(textEl.textContent).not.toContain("✓");
  });
});

// ── 4. UserMessage backtick rendering ────────────────────────────────────────

describe("UserMessage", () => {
  it("renders inline backtick spans as <code> elements", () => {
    const { container } = render(
      <UserMessage content="Run `npm install` to start" />
    );
    const codeEls = container.querySelectorAll("code");
    expect(codeEls.length).toBe(1);
    expect(codeEls[0].textContent).toBe("npm install");
  });

  it("renders plain text without code when no backticks", () => {
    const { container } = render(<UserMessage content="Hello world" />);
    expect(container.querySelectorAll("code").length).toBe(0);
    expect(screen.getByText("Hello world")).toBeTruthy();
  });
});
