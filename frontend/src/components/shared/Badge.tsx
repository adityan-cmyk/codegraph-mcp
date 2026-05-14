type BadgeProps = {
  label: string;
  tone?: "neutral" | "success";
};

export function Badge({ label, tone = "neutral" }: BadgeProps) {
  const palette = tone === "success" ? { background: "#dcfce7", color: "#166534" } : { background: "#e5e7eb", color: "#111827" };

  return (
    <span
      style={{
        borderRadius: "999px",
        padding: "0.25rem 0.75rem",
        fontSize: "0.875rem",
        ...palette,
      }}
    >
      {label}
    </span>
  );
}