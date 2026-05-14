from fastapi import FastAPI

from app.core.state_machine import IncidentState


app = FastAPI(title="On-call Assistant API")


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "default_state": IncidentState.CREATED.value}