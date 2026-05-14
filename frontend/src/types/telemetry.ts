export type ConfidenceScore = {
  label: string;
  value: number | string;
};

export type ToolExecution = {
  toolName: string;
  status: "queued" | "running" | "passed" | "failed";
  output: string[];
};