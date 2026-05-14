from enum import Enum


class IncidentState(str, Enum):
    CREATED = "CREATED"
    INGESTING = "INGESTING"
    RETRIEVING = "RETRIEVING"
    GRAPH_EXPANDING = "GRAPH_EXPANDING"
    GENERATING_PATCH = "GENERATING_PATCH"
    VALIDATING = "VALIDATING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


VALID_TRANSITIONS = {
    IncidentState.CREATED: {IncidentState.INGESTING, IncidentState.FAILED},
    IncidentState.INGESTING: {IncidentState.RETRIEVING, IncidentState.FAILED},
    IncidentState.RETRIEVING: {IncidentState.GRAPH_EXPANDING, IncidentState.FAILED},
    IncidentState.GRAPH_EXPANDING: {IncidentState.GENERATING_PATCH, IncidentState.FAILED},
    IncidentState.GENERATING_PATCH: {IncidentState.VALIDATING, IncidentState.FAILED},
    IncidentState.VALIDATING: {IncidentState.RESOLVED, IncidentState.FAILED},
    IncidentState.RESOLVED: set(),
    IncidentState.FAILED: set(),
}