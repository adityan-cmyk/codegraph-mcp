import { Badge } from "../shared/Badge";

export function LlmStream() {
  return (
    <section>
      <h2>LLM Output</h2>
      <p>Verified root causes and Rust patches stream into this panel.</p>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <Badge label="Retrieval: 0.91" tone="neutral" />
        <Badge label="Graph: 0.88" tone="neutral" />
        <Badge label="Sandbox: Passed" tone="success" />
      </div>
    </section>
  );
}