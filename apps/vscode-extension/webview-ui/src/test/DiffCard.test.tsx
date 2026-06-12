import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { DiffCard } from "../components/messages/DiffCard";
import type { DiffEntry } from "../types";

vi.mock("../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let postMessage: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  const mod = await import("../vscodeApi");
  postMessage = mod.vscode.postMessage as ReturnType<typeof vi.fn>;
  postMessage.mockClear();
});

const ENTRIES: DiffEntry[] = [
  {
    path: "src/components/Foo.tsx",
    additions: 3,
    deletions: 1,
    temp_path: "/tmp/shadow/Foo.tsx",
  },
  {
    path: "services/agentd-py/agentd/api/routes.py",
    additions: 1,
    deletions: 0,
    temp_path: "/tmp/shadow/routes.py",
  },
];

// ── 1. Header stats ───────────────────────────────────────────────────────────

describe("DiffCard — header stats", () => {
  it("shows aggregate additions and deletions, plus file count badge", () => {
    render(
      <DiffCard taskId="t1" diffEntries={ENTRIES} />
    );

    // +3 + +1 = +4 additions; -1 + -0 = 1 deletion
    expect(screen.getByText("+4")).toBeTruthy();
    // The minus sign in the rendered text (uses HTML &minus; entity — actual char is −)
    // Allow for either the ASCII dash or the minus sign entity.
    const minusEl = screen.getByText((txt) => /[−-]1/.test(txt));
    expect(minusEl).toBeTruthy();

    // File count badge: "2 files"
    expect(screen.getByText("2 files")).toBeTruthy();
  });

  it("shows '1 file' (singular) for a single entry", () => {
    render(
      <DiffCard taskId="t1" diffEntries={[ENTRIES[0]]} />
    );
    expect(screen.getByText("1 file")).toBeTruthy();
  });
});

// ── 2. Expand shows file rows ─────────────────────────────────────────────────

describe("DiffCard — expand shows file rows", () => {
  it("click header expands and shows basenames", () => {
    render(<DiffCard taskId="t1" diffEntries={ENTRIES} />);

    // File rows not visible before expand.
    expect(screen.queryByText("Foo.tsx")).toBeNull();

    // Click header (first button).
    const header = screen.getAllByRole("button")[0];
    fireEvent.click(header);

    expect(screen.getByText("Foo.tsx")).toBeTruthy();
    expect(screen.getByText("routes.py")).toBeTruthy();
  });
});

// ── 3. View diff button ───────────────────────────────────────────────────────

describe("DiffCard — view diff button", () => {
  it("posts viewDiffFile with path and temp_path when view button clicked", () => {
    render(<DiffCard taskId="t1" diffEntries={ENTRIES} />);

    // Expand first to see the file rows.
    const header = screen.getAllByRole("button")[0];
    fireEvent.click(header);

    // Click the first "Open diff in editor" button.
    const viewBtns = screen.getAllByTitle("Open diff in editor");
    fireEvent.click(viewBtns[0]);

    expect(postMessage).toHaveBeenCalledWith({
      type: "viewDiffFile",
      path: "src/components/Foo.tsx",
      shadowPath: "/tmp/shadow/Foo.tsx",
    });
  });

  it("falls back to empty shadowPath when temp_path is undefined", () => {
    const entryNoTemp: DiffEntry = {
      path: "src/index.ts",
      additions: 2,
      deletions: 0,
    };
    render(<DiffCard taskId="t1" diffEntries={[entryNoTemp]} />);

    const header = screen.getAllByRole("button")[0];
    fireEvent.click(header);

    const viewBtn = screen.getByTitle("Open diff in editor");
    fireEvent.click(viewBtn);

    expect(postMessage).toHaveBeenCalledWith({
      type: "viewDiffFile",
      path: "src/index.ts",
      shadowPath: "",
    });
  });
});

// ── 4. Accept all one-shot ────────────────────────────────────────────────────

describe("DiffCard — Accept all", () => {
  it("posts applyInlineChange and swaps row to Applied; Reject gone", () => {
    render(<DiffCard taskId="task-77" diffEntries={ENTRIES} />);

    const acceptBtn = screen.getByRole("button", { name: /accept all/i });
    fireEvent.click(acceptBtn);

    // One-shot: both action buttons gone.
    expect(screen.queryByRole("button", { name: /accept all/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();

    // Resolved label visible.
    expect(screen.getByText("Applied")).toBeTruthy();

    expect(postMessage).toHaveBeenCalledWith({
      type: "applyInlineChange",
      taskId: "task-77",
    });
  });
});

// ── 5. Reject one-shot ────────────────────────────────────────────────────────

describe("DiffCard — Reject", () => {
  it("posts discardInlineChange and shows Discarded", () => {
    render(<DiffCard taskId="task-77" diffEntries={ENTRIES} />);

    const rejectBtn = screen.getByRole("button", { name: /reject/i });
    fireEvent.click(rejectBtn);

    expect(screen.queryByRole("button", { name: /accept all/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();

    expect(screen.getByText("Discarded")).toBeTruthy();

    expect(postMessage).toHaveBeenCalledWith({
      type: "discardInlineChange",
      taskId: "task-77",
    });
  });
});

// ── 6. resolved prop renders resolved state ───────────────────────────────────

describe("DiffCard — resolved prop", () => {
  it("renders Applied with no action buttons when resolved='applied'", () => {
    render(
      <DiffCard taskId="t1" diffEntries={ENTRIES} resolved="applied" />
    );

    // No action buttons.
    expect(screen.queryByRole("button", { name: /accept all/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject/i })).toBeNull();

    // Applied label present.
    expect(screen.getByText("Applied")).toBeTruthy();
  });

  it("renders Discarded with no action buttons when resolved='discarded'", () => {
    render(
      <DiffCard taskId="t1" diffEntries={ENTRIES} resolved="discarded" />
    );

    expect(screen.queryByRole("button", { name: /accept all/i })).toBeNull();
    expect(screen.getByText("Discarded")).toBeTruthy();
  });
});

// ── 6. Persisted tool pills ───────────────────────────────────────────────────

describe("DiffCard — persisted tool pills", () => {
  const TOOL_EVENTS = [
    {
      id: 0,
      tool: "read_file",
      args: { path: "a.py" },
      source: "explore" as const,
      output: "x = 1",
      isError: false,
      done: true,
    },
    {
      id: 1,
      tool: "run_command",
      args: { command: "pytest -q" },
      source: "execution" as const,
      output: "3 passed",
      isError: false,
      done: true,
    },
  ];

  it("renders a pill per persisted tool event", () => {
    render(
      <DiffCard taskId="t1" diffEntries={ENTRIES} toolEvents={TOOL_EVENTS} />
    );

    expect(screen.getByText("read_file")).toBeTruthy();
    expect(screen.getByText("run_command")).toBeTruthy();
  });

  it("expands a done pill to show input args and output", () => {
    render(
      <DiffCard taskId="t1" diffEntries={ENTRIES} toolEvents={TOOL_EVENTS} />
    );

    fireEvent.click(screen.getByText("run_command"));
    expect(screen.getByText("pytest -q")).toBeTruthy();
    expect(screen.getByText("3 passed")).toBeTruthy();
  });

  it("renders no pill strip when toolEvents is absent", () => {
    render(<DiffCard taskId="t1" diffEntries={ENTRIES} />);
    expect(screen.queryByText("read_file")).toBeNull();
  });
});

// ── 7. Resolved step-review record ────────────────────────────────────────────

describe("DiffCard — resolved step-review record", () => {
  it("renders a resolved step-review record inert with panes", () => {
    render(
      <DiffCard
        taskId="task-123"
        resolved="applied"
        diffEntries={[
          {
            path: "a.py", additions: 1, deletions: 0, temp_path: "/tmp/a.py",
            unified_diff: "@@ -1,1 +1,2 @@\n x = 1\n+y = 2",
          },
        ]}
      />,
    );
    expect(screen.getByText("Applied")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /accept all/i })).toBeNull();
    // Panes render from the persisted unified_diff once expanded.
    fireEvent.click(screen.getByText("Changes ready"));
    expect(screen.getByText("y = 2")).toBeTruthy();
  });
});
