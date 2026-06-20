import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModeGate } from "./ModeGate";
import { vscode } from "../../../vscodeApi";

vi.mock("../../../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

const payload = {
  options: [{ mode: "edit", label: "Edit inline", description: "" }],
  recommended: "edit",
};

describe("ModeGate — chat about this", () => {
  it("renders the chat-about-this input instead of a hint", () => {
    render(<ModeGate taskId="chat-1" payload={payload} />);
    expect(screen.getByPlaceholderText(/chat about this/i)).toBeInTheDocument();
  });

  it("submitting posts exactly one sendMessage and is one-shot", () => {
    render(<ModeGate taskId="chat-1" payload={payload} />);
    const input = screen.getByPlaceholderText(/chat about this/i);
    fireEvent.change(input, { target: { value: "make it minimal" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" }); // second press ignored (one-shot)
    const calls = (vscode.postMessage as ReturnType<typeof vi.fn>).mock.calls
      .filter((c) => c[0]?.type === "sendMessage");
    expect(calls).toHaveLength(1);
    expect(calls[0][0].text).toBe("make it minimal");
  });
});
