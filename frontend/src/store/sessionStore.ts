export type IncidentState =
  | "CREATED"
  | "INGESTING"
  | "RETRIEVING"
  | "GRAPH_EXPANDING"
  | "GENERATING_PATCH"
  | "VALIDATING"
  | "RESOLVED"
  | "FAILED";

export type SessionStore = {
  incidentId: string;
  state: IncidentState;
};

export const initialSessionStore: SessionStore = {
  incidentId: "incident-demo",
  state: "CREATED",
};