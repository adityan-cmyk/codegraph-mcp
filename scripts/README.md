# Infra Scripts

These helper scripts wrap Docker Compose commands for local infrastructure.

## Files

- `up-infra.sh`: Start Postgres, Redis, Neo4j, Weaviate, and transformer inference.
- `status-infra.sh`: Show container status for the infra stack.
- `down-infra.sh`: Stop the infra stack.

## Usage

Run from repository root:

```bash
./scripts/up-infra.sh
./scripts/status-infra.sh
./scripts/down-infra.sh
```

## Notes

- Compose file used: `docker-compose.db.yml` in repo root.
- If Docker Desktop is not running, these scripts will fail fast.
- Service credentials and backend settings are documented in `handoff.md`.
