import { describe, expect, test } from "vitest";
import { PatchDocumentSchema, PlanSchema } from "../src/domain/schemas.js";

describe("schema validation", () => {
  test("accepts valid plan schema", () => {
    const value = PlanSchema.parse({
      analysis: "do things",
      steps: [{ id: "S1", goal: "edit", targets: ["a.ts"], risk: "low" }],
      expected_files: ["a.ts"],
      stop_conditions: ["tests pass"]
    });

    expect(value.steps).toHaveLength(1);
  });

  test("rejects invalid patch op", () => {
    expect(() =>
      PatchDocumentSchema.parse({
        patch_ops: [
          {
            op: "replace_range",
            file: "a.ts",
            anchor: { start_line: 10, end_line: 3 },
            content: "x",
            reason: "bad"
          }
        ]
      })
    ).toThrow();
  });
});
