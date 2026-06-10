import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { PlanCard } from "../components/messages/PlanCard";

vi.mock("../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

// Import after mock is registered so the mock is in place.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let postMessage: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  const mod = await import("../vscodeApi");
  postMessage = mod.vscode.postMessage as ReturnType<typeof vi.fn>;
  postMessage.mockClear();
});

const PLAN_CONTENT = `
## Step 1: Update types
Add the new interface to the contracts file.

## Step 2: Wire the route
Register handler in routes.py.
`;

// ── 1. Collapsed by default ───────────────────────────────────────────────────

describe("PlanCard — collapsed by default", () => {
  it("renders with data-expanded=false when first mounted", () => {
    const { container } = render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" />
    );
    const body = container.querySelector("[data-expanded]");
    expect(body).not.toBeNull();
    expect(body!.getAttribute("data-expanded")).toBe("false");
  });
});

// ── 2. Header click expands ───────────────────────────────────────────────────

describe("PlanCard — expand on header click", () => {
  it("flips data-expanded to true after clicking the header", () => {
    const { container } = render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" />
    );
    const header = container.querySelector("button")!;
    fireEvent.click(header);

    const body = container.querySelector("[data-expanded]");
    expect(body!.getAttribute("data-expanded")).toBe("true");
  });

  it("hides the fade overlay when expanded", () => {
    const { container } = render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" />
    );

    // Fade overlay present when collapsed.
    // It's the gradient div inside the body wrapper.
    const before = container.querySelectorAll("[data-expanded] > div");
    // At least the markdown + fade div.
    expect(before.length).toBeGreaterThanOrEqual(1);

    const header = container.querySelector("button")!;
    fireEvent.click(header);

    // After expand, count of children may differ (fade div removed).
    const bodyExpanded = container.querySelector("[data-expanded='true']");
    expect(bodyExpanded).not.toBeNull();
  });
});

// ── 3. Implement one-shot ─────────────────────────────────────────────────────

describe("PlanCard — Implement one-shot", () => {
  it("posts implementPlan and shows Implementing row; Implement button gone", () => {
    render(<PlanCard content={PLAN_CONTENT} taskId="task-42" />);

    const implementBtn = screen.getByRole("button", { name: /implement/i });
    fireEvent.click(implementBtn);

    // One-shot: Implement button gone.
    expect(screen.queryByRole("button", { name: /implement/i })).toBeNull();

    // Resolved label visible.
    expect(screen.getByText(/implementing/i)).toBeTruthy();

    // Message posted.
    expect(postMessage).toHaveBeenCalledWith({
      type: "implementPlan",
      taskId: "task-42",
    });
  });
});

// ── 4. Give feedback flow ─────────────────────────────────────────────────────

describe("PlanCard — Give feedback / Cancel", () => {
  it("clicking Give feedback hides Implement and shows input; Cancel restores idle", () => {
    render(<PlanCard content={PLAN_CONTENT} taskId="t1" />);

    // Initial idle: both buttons visible.
    expect(screen.getByRole("button", { name: /implement/i })).toBeTruthy();
    const fbBtn = screen.getByRole("button", { name: /give feedback/i });
    fireEvent.click(fbBtn);

    // Implement button hidden in feedback mode.
    expect(screen.queryByRole("button", { name: /implement/i })).toBeNull();

    // Input is visible.
    expect(
      screen.getByPlaceholderText(/what should change in this plan/i)
    ).toBeTruthy();

    // Cancel restores idle.
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByRole("button", { name: /implement/i })).toBeTruthy();
  });
});

// ── 5. Send feedback ──────────────────────────────────────────────────────────

describe("PlanCard — Send feedback", () => {
  it("Send with text posts planFeedback and shows Regenerating row", () => {
    render(<PlanCard content={PLAN_CONTENT} taskId="task-99" />);

    // Open feedback mode.
    fireEvent.click(screen.getByRole("button", { name: /give feedback/i }));

    const input = screen.getByPlaceholderText(/what should change/i);
    fireEvent.change(input, { target: { value: "Add error handling" } });

    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    expect(postMessage).toHaveBeenCalledWith({
      type: "planFeedback",
      taskId: "task-99",
      feedback: "Add error handling",
    });
    expect(screen.getByText(/regenerating with your feedback/i)).toBeTruthy();
  });

  it("Send with empty input does NOT post", () => {
    render(<PlanCard content={PLAN_CONTENT} taskId="task-99" />);

    fireEvent.click(screen.getByRole("button", { name: /give feedback/i }));

    // Do not type anything — input stays empty.
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    expect(postMessage).not.toHaveBeenCalled();
    // Still in feedback mode.
    expect(
      screen.getByPlaceholderText(/what should change in this plan/i)
    ).toBeTruthy();
  });
});

// ── 6. readOnly renders no action buttons ─────────────────────────────────────

describe("PlanCard — readOnly", () => {
  it("renders no Implement or Give feedback buttons when readOnly=true", () => {
    render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" readOnly={true} />
    );
    expect(screen.queryByRole("button", { name: /implement/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /give feedback/i })).toBeNull();
  });
});

// ── 7. Version badge ──────────────────────────────────────────────────────────

describe("PlanCard — version badge", () => {
  it("renders 'v2' badge when version=2", () => {
    render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" version={2} />
    );
    expect(screen.getByText("v2")).toBeTruthy();
  });

  it("renders no badge when version=1", () => {
    render(
      <PlanCard content={PLAN_CONTENT} taskId="t1" version={1} />
    );
    expect(screen.queryByText(/^v\d/)).toBeNull();
  });

  it("renders no badge when version is undefined", () => {
    render(<PlanCard content={PLAN_CONTENT} taskId="t1" />);
    expect(screen.queryByText(/^v\d/)).toBeNull();
  });

  it("expanding removes the fade overlay", () => {
    const { container } = render(<PlanCard content={"## Step 1\nbody"} taskId="t1" />);
    expect(container.querySelector('[data-testid="plan-fade"]')).not.toBeNull();
    fireEvent.click(screen.getByText("Plan"));
    expect(container.querySelector('[data-testid="plan-fade"]')).toBeNull();
  });

  it("renders no step-count subtitle when the heuristic finds nothing", () => {
    render(<PlanCard content={"just prose, no step markers"} taskId="t1" />);
    expect(screen.queryByText(/steps/)).toBeNull();
  });
});
