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


def can_transition(current_state: IncidentState, next_state: IncidentState) -> bool:
    return next_state in VALID_TRANSITIONS[current_state]


def validate_transition(current_state: IncidentState, next_state: IncidentState) -> None:
    if not can_transition(current_state, next_state):
        raise ValueError(f"Invalid transition from {current_state.value} to {next_state.value}")