import type { ReactNode } from "react";

type ThreePaneLayoutProps = {
  left: ReactNode;
  center: ReactNode;
  right: ReactNode;
};

export function ThreePaneLayout({ left, center, right }: ThreePaneLayoutProps) {
  return (
    <main
      style={{
        display: "grid",
        gridTemplateColumns: "280px minmax(0, 1fr) 320px",
        minHeight: "100vh",
        gap: "1px",
        background: "#d1d5db",
      }}
    >
      <section style={{ background: "#f8fafc", padding: "1rem" }}>{left}</section>
      <section style={{ background: "#ffffff", padding: "1rem" }}>{center}</section>
      <section style={{ background: "#111827", color: "#f9fafb", padding: "1rem" }}>{right}</section>
    </main>
  );
}