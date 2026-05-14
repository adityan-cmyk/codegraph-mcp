from pydantic import BaseModel


class IncidentFingerprint(BaseModel):
    service: str
    panic_type: str
    top_frame: str
    commit_hash: str


class ResolutionPackage(BaseModel):
    fingerprint: IncidentFingerprint
    root_cause: str
    patch: str