import { describe, expect, it } from "vitest";
import { PendingGateSchema } from "../src/contracts/task-contracts";

describe("mcp_tool gate contract", () => {
  it("parses a kind=mcp_tool pending gate (a kind missing from the Zod enum makes the /live parse throw and the gate silently never renders)", () => {
    const gate = PendingGateSchema.parse({
      kind: "mcp_tool",
      payload: { server: "gh", tool: "create_issue", args: { title: "x" } },
    });
    expect(gate.kind).toBe("mcp_tool");
  });
});
