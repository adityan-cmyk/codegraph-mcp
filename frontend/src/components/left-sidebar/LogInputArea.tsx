type LogInputAreaProps = {
  placeholder?: string;
};

export function LogInputArea({ placeholder = "Paste Coralogix or Prometheus logs..." }: LogInputAreaProps) {
  return (
    <label style={{ display: "grid", gap: "0.5rem" }}>
      <span>Log Input</span>
      <textarea
        placeholder={placeholder}
        rows={12}
        style={{ width: "100%", resize: "vertical" }}
      />
    </label>
  );
}