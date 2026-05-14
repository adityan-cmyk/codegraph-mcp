from app.schemas.incident import IncidentFingerprint


def lookup_resolved_error(fingerprint: IncidentFingerprint) -> dict[str, object]:
    return {"match": None, "fingerprint": fingerprint.model_dump()}