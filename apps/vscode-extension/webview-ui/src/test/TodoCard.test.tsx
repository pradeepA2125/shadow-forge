import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { TodoCard } from "../components/messages/TodoCard";

describe("TodoCard", () => {
  it("renders a progress header and each item with a status glyph", () => {
    render(<TodoCard items={[
      { title: "Enemies", status: "done", note: "" },
      { title: "Jump", status: "in_progress", note: "" },
      { title: "Timer", status: "pending", note: "" },
      { title: "Sound", status: "blocked", note: "needs asset" },
    ]} />);
    expect(screen.getByText("1/4")).toBeTruthy();   // done/total count (cancelled excluded from total)
    expect(screen.getByText("Enemies")).toBeTruthy();
    expect(screen.getByText("Jump")).toBeTruthy();
    expect(screen.getByText(/needs asset/i)).toBeTruthy();  // blocked reason shown
  });

  it("excludes cancelled items from the done/total count but still lists them", () => {
    render(<TodoCard items={[
      { title: "A", status: "done", note: "" },
      { title: "B", status: "cancelled", note: "superseded" },
    ]} />);
    expect(screen.getByText("1/1")).toBeTruthy();   // cancelled not counted in total
    expect(screen.getByText("B")).toBeTruthy();
  });
});
