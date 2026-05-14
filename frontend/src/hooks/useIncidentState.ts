import { useState } from "react";
import type { IncidentState } from "../store/sessionStore";

export function useIncidentState(initialState: IncidentState = "CREATED") {
  const [state, setState] = useState<IncidentState>(initialState);

  return {
    state,
    advance: setState,
  };
}