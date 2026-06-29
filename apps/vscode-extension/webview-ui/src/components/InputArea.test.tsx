import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InputArea } from "./InputArea";
import { vscode } from "../vscodeApi";

vi.mock("../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

const availability = {
  disabled: false,
  showStop: false,
  taskStop: false,
  placeholder: "Message",
} as const;

function Harness() {
  const [draft, setDraft] = useState("/review src/a.py");
  return <InputArea availability={availability} draft={draft} onDraftChange={setDraft} />;
}

describe("InputArea slash-command expansion", () => {
  beforeEach(() => vi.clearAllMocks());

  it("Enter on a /name command posts expandPrompt, not sendMessage", () => {
    render(<Harness />);
    const ta = screen.getByLabelText("Chat input");
    fireEvent.keyDown(ta, { key: "Enter" });
    const calls = (vscode.postMessage as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(calls).toContainEqual({ type: "expandPrompt", name: "review", args: "src/a.py" });
    expect(calls.find((c) => c.type === "sendMessage")).toBeUndefined();
  });

  it("a promptExpanded message replaces the draft", () => {
    render(<Harness />);
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "promptExpanded", name: "review", found: true, text: "Review src/a.py" },
        })
      );
    });
    const ta = screen.getByLabelText("Chat input") as HTMLTextAreaElement;
    expect(ta.value).toBe("Review src/a.py");
  });
});
