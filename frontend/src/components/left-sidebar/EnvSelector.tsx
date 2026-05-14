type EnvSelectorProps = {
  buildId: string;
  environment: "UAT" | "PROD";
};

export function EnvSelector({ buildId, environment }: EnvSelectorProps) {
  return (
    <div>
      <h2>Context</h2>
      <p>Build: {buildId || "pending"}</p>
      <p>Environment: {environment}</p>
    </div>
  );
}