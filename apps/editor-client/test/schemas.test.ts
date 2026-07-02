import { describe, expect, test } from "vitest";
import { PatchDocumentSchema, PlanSchema } from "../src/domain/schemas.js";
import {
  RecallTraceSchema,
  MemoryViewSchema,
  BackendConfigSchema,
} from "../src/contracts/task-contracts.js";

describe("schema validation", () => {
  test("accepts valid plan schema", () => {
    const value = PlanSchema.parse({
      analysis: "do things",
      steps: [
        {
          id: "S1",
          goal: "edit",
          targets: [{ path: "a.ts", intent: "existing" }],
          risk: "low"
        }
      ],
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

describe("memory inspector schemas (Phase 3-B)", () => {
  test("RecallTraceSchema parses a full trace with all five signals", () => {
    const trace = RecallTraceSchema.parse({
      query: "what does X do",
      scopeKind: "workspace",
      scopeId: "/ws",
      k: 8,
      floor: 0.15,
      reranked: false,
      entries: [
        {
          memoryId: "a",
          kind: "semantic",
          content: "auth flow",
          importance: 5,
          signals: { semantic: 1, lexical: 0.5, structural: 0, importance: 0.4, recency: 0.9 },
          fusedScore: 0.99,
          rerankScore: null,
          finalRank: 0,
          injected: true,
        },
      ],
    });
    expect(trace.entries[0].memoryId).toBe("a");
    expect(trace.entries[0].signals.recency).toBe(0.9);
    expect(trace.entries[0].rerankScore).toBeNull();
  });

  test("MemoryViewSchema parses a memory with nullable lifecycle fields", () => {
    const m = MemoryViewSchema.parse({
      id: "m1",
      scopeKind: "workspace",
      scopeId: "/ws",
      kind: "episodic",
      content: "user did X",
      entities: ["src/a.py"],
      importance: 3,
      validFrom: "2026-06-29T00:00:00Z",
      validTo: null,
      supersededBy: null,
      sourceKind: "consolidation",
      sourceRef: "r",
      sourceSeqLo: null,
      sourceSeqHi: null,
      createdAt: "2026-06-29T00:00:00Z",
    });
    expect(m.id).toBe("m1");
    expect(m.validTo).toBeNull();
    expect(m.entities).toEqual(["src/a.py"]);
  });

  test("BackendConfigSchema includes memoryEnabled", () => {
    const c = BackendConfigSchema.parse({
      taskSubsystemEnabled: false,
      chatControllerEnabled: true,
      memoryEnabled: true,
      skillsEnabled: false,
      mcpEnabled: false,
    });
    expect(c.memoryEnabled).toBe(true);
  });
});
