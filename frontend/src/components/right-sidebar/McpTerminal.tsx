type McpTerminalProps = {
  lines?: string[];
};

export function McpTerminal({ lines = ["Compiling...", "test failed: panic at src/db.rs:42"] }: McpTerminalProps) {
  return (
    <section>
      <h2>MCP Terminal</h2>
      <pre style={{ whiteSpace: "pre-wrap" }}>{lines.join("\n")}</pre>
    </section>
  );
}