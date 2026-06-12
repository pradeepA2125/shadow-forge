import { z } from "zod";

const RiskSchema = z.enum(["low", "med", "high"]);
const PlanTargetIntentSchema = z.enum(["existing", "new"]);

export const PlanTargetSchema = z.preprocess(
  (input) => {
    if (typeof input === "string") {
      return { path: input, intent: "existing" };
    }

    if (Array.isArray(input) && input.length >= 1 && typeof input[0] === "string") {
      return {
        path: input[0],
        intent: typeof input[1] === "string" ? input[1] : "existing"
      };
    }

    return input;
  },
  z.object({
    path: z.string().min(1),
    intent: PlanTargetIntentSchema
  })
);

export const PlanStepSchema = z.object({
  id: z.string().min(1),
  goal: z.string().min(1),
  // Empty targets = a command-only step (run build/tests, nothing to patch).
  // A .min(1) here silently broke the ReviewCard for such plans: getTaskResult
  // threw on parse every poll and the live slot never rendered.
  targets: z.array(PlanTargetSchema),
  risk: RiskSchema
});

export const PlanSchema = z.object({
  analysis: z.string().min(1),
  steps: z.array(PlanStepSchema).min(1),
  expected_files: z.array(z.string().min(1)),
  stop_conditions: z.array(z.string().min(1)).min(1)
});

const ReplaceRangeOpSchema = z.object({
  op: z.literal("replace_range"),
  file: z.string().min(1),
  anchor: z.object({
    start_line: z.number().int().positive(),
    end_line: z.number().int().positive()
  }),
  content: z.string(),
  reason: z.string().min(1)
});

const InsertAfterSymbolOpSchema = z.object({
  op: z.literal("insert_after_symbol"),
  file: z.string().min(1),
  anchor: z.object({
    symbol: z.string().min(1)
  }),
  content: z.string(),
  reason: z.string().min(1)
});

const CreateFileOpSchema = z.object({
  op: z.literal("create_file"),
  file: z.string().min(1),
  content: z.string(),
  reason: z.string().min(1)
});

const DeleteFileOpSchema = z.object({
  op: z.literal("delete_file"),
  file: z.string().min(1),
  reason: z.string().min(1)
});

export const PatchOperationSchema = z.discriminatedUnion("op", [
  ReplaceRangeOpSchema,
  InsertAfterSymbolOpSchema,
  CreateFileOpSchema,
  DeleteFileOpSchema
]);

export const PatchDocumentSchema = z.object({
  patch_ops: z.array(PatchOperationSchema).min(1)
}).superRefine((value, ctx) => {
  value.patch_ops.forEach((op, index) => {
    if (op.op !== "replace_range") {
      return;
    }

    if (op.anchor.end_line < op.anchor.start_line) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["patch_ops", index, "anchor"],
        message: "replace_range end_line must be >= start_line"
      });
    }
  });
});

export const DiagnosticsSchema = z.array(
  z.object({
    source: z.string().min(1),
    file: z.string().min(1).optional(),
    line: z.number().int().positive().optional(),
    column: z.number().int().positive().optional(),
    message: z.string().min(1),
    level: z.enum(["error", "warning"])
  })
);
