import { IncidentChat } from "../components/center-console/IncidentChat";
import { LlmStream } from "../components/center-console/LlmStream";
import { ResolveModal } from "../components/center-console/ResolveModal";
import { ThreePaneLayout } from "../components/layout/ThreePaneLayout";
import { EnvSelector } from "../components/left-sidebar/EnvSelector";
import { LogInputArea } from "../components/left-sidebar/LogInputArea";
import { SyncKbButton } from "../components/left-sidebar/SyncKbButton";
import { LangfuseTrace } from "../components/right-sidebar/LangfuseTrace";
import { McpTerminal } from "../components/right-sidebar/McpTerminal";

export function Dashboard() {
  return (
    <ThreePaneLayout
      left={
        <div style={{ display: "grid", gap: "1rem" }}>
          <EnvSelector buildId="build-001" environment="UAT" />
          <LogInputArea />
          <SyncKbButton />
          <button>Start Debugging</button>
        </div>
      }
      center={
        <div style={{ display: "grid", gap: "1rem" }}>
          <IncidentChat />
          <LlmStream />
          <ResolveModal />
        </div>
      }
      right={
        <div style={{ display: "grid", gap: "1rem" }}>
          <LangfuseTrace />
          <McpTerminal />
        </div>
      }
    />
  );
}