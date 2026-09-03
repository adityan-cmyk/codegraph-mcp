#!/usr/bin/env python3
"""Container watchdog — monitors Docker containers, self-heals, and sends email notifications.

Runs on the HOST (not in Docker) so it can heal the stack even when the
backend is dead. Independent of the app — reads SMTP config directly from .env.

Usage:
    Run via cron every 5 minutes:
    */5 * * * * /path/to/container-watchdog.py >> /path/to/logs/watchdog.log 2>&1

Healing actions:
    - Container exited          → docker compose up -d <service>
    - Container unhealthy       → docker restart <container>
    - Container restart-looping → docker compose up -d --force-recreate <service>
    - Weaviate cluster-stuck    → wipe volume + recreate (last resort)

Each action triggers an email notification.
"""

import json
import os
import smtplib
import subprocess
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
LOG_PREFIX = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]"

CONTAINERS = {
    "oncall-postgres": "postgres",
    "oncall-redis": "redis",
    "oncall-weaviate": "weaviate",
    "oncall-neo4j": "neo4j",
    "oncall-backend": "backend",
    "oncall-celery-worker": "celery-worker",
}

# Track repeated failures to detect restart loops
STATE_FILE = PROJECT_ROOT / "logs" / "watchdog-state.json"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def send_email(subject: str, body: str, env: dict) -> None:
    host = env.get("SMTP_HOST")
    user = env.get("SMTP_USER")
    password = env.get("SMTP_PASSWORD")
    to_raw = env.get("SMTP_TO", "[]")
    if not host or not user or not password or not to_raw:
        log(f"SMTP not configured, skipping: {subject}")
        return
    try:
        recipients = json.loads(to_raw) if to_raw.startswith("[") else [to_raw]
    except json.JSONDecodeError:
        recipients = [to_raw]

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = env.get("SMTP_FROM") or user
        msg["To"] = ", ".join(recipients)
        port = int(env.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=10) as server:
            if env.get("SMTP_USE_TLS", "true").lower() == "true":
                server.starttls()
            server.login(user, password)
            server.send_message(msg)
        log(f"Email sent: {subject}")
    except Exception as exc:
        log(f"Email FAILED ({subject}): {exc}")


def docker(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120
    )
    output = result.stdout.strip()
    if not output:
        output = result.stderr.strip()
    return result.returncode, output


def get_container_state(container: str) -> dict:
    code, output = docker(
        "inspect",
        "--format",
        "{{json .State}}",
        container,
    )
    if code != 0:
        return {"Status": "missing", "Running": False}
    try:
        state = json.loads(output)
        # Normalize keys — Docker uses capitalized keys
        return {
            "Status": state.get("Status", "unknown"),
            "Running": state.get("Running", False),
            "Restarting": state.get("Restarting", False),
        }
    except json.JSONDecodeError:
        return {"Status": "unknown", "Running": False}


def get_health(container: str) -> str:
    code, output = docker(
        "inspect",
        "--format",
        "{{.State.Health.Status}}",
        container,
    )
    if code != 0:
        return "no-healthcheck"
    return output.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_restart_count(container: str) -> int:
    code, output = docker("inspect", "--format", "{{.RestartCount}}", container)
    try:
        return int(output.strip())
    except (ValueError, AttributeError):
        return 0


def heal_container(container: str, service: str, state: dict, env: dict) -> None:
    """Attempt to heal a container based on its failure mode."""
    container_state = get_container_state(container)
    status = container_state.get("Status", "unknown")
    health = get_health(container)
    restart_count = get_restart_count(container)

    state_key = f"{container}:failure_count"
    failure_count = state.get(state_key, 0) + 1
    state[state_key] = failure_count
    state[f"{container}:last_failure"] = time.time()

    log(f"{container}: status={status} health={health} failure_count={failure_count}")

    # Decide healing action based on failure mode
    if status in ("exited", "dead", "missing"):
        action = f"docker compose up -d {service}"
        code, output = docker("compose", "-f", str(PROJECT_ROOT / "docker-compose.yml"), "up", "-d", service)
        action_result = f"exit={code} {output[:200]}"
    elif health in ("unhealthy",) and failure_count >= 3:
        # Unhealthy 3+ times in a row — force recreate
        if container == "oncall-weaviate" and failure_count >= 6:
            # Weaviate cluster-stuck last resort: wipe and recreate
            action = "docker compose down weaviate + volume wipe + recreate"
            compose_file = str(PROJECT_ROOT / "docker-compose.yml")
            docker("compose", "-f", compose_file, "down", "weaviate")
            docker("volume", "rm", "on-call-assistance_weaviate_data")
            code, output = docker("compose", "-f", compose_file, "up", "-d", "weaviate")
            action_result = f"exit={code} {output[:200]}"
            send_email(
                subject=f"[watchdog] {container} volume WIPED (cluster-stuck) — full reindex needed",
                body="\n".join([
                    f"Container: {container}",
                    f"Health: {health}",
                    f"Consecutive failures: {failure_count}",
                    f"Action: {action}",
                    f"Result: {action_result}",
                    "",
                    "Weaviate data was corrupted (cluster join loop).",
                    "ACTION REQUIRED: trigger full reindex:",
                    "  curl -X POST http://localhost:8000/api/index/repository -H 'Content-Type: application/json' -d '{\"repository_path\": \"/repos/codebase\"}'",
                ]),
                env=env,
            )
            state[state_key] = 0
            save_state(state)
            return
        else:
            action = f"docker compose up -d --force-recreate {service}"
            compose_file = str(PROJECT_ROOT / "docker-compose.yml")
            code, output = docker("compose", "-f", compose_file, "up", "-d", "--force-recreate", service)
            action_result = f"exit={code} {output[:200]}"
    elif status == "restarting" and restart_count > 10:
        action = f"docker compose up -d --force-recreate {service} (restart loop: {restart_count})"
        compose_file = str(PROJECT_ROOT / "docker-compose.yml")
        code, output = docker("compose", "-f", compose_file, "up", "-d", "--force-recreate", service)
        action_result = f"exit={code} {output[:200]}"
    else:
        # Still running but unhealthy for the first time — just restart
        action = f"docker restart {container}"
        code, output = docker("restart", container)
        action_result = f"exit={code} {output[:200]}"

    send_email(
        subject=f"[watchdog] Healed {container} ({status}/{health})",
        body="\n".join([
            f"Container: {container}",
            f"Service: {service}",
            f"Status: {status}",
            f"Health: {health}",
            f"Restart count: {restart_count}",
            f"Consecutive failures: {failure_count}",
            f"Action taken: {action}",
            f"Result: {action_result}",
        ]),
        env=env,
    )
    save_state(state)


def main() -> None:
    env = load_env()
    state = load_state()
    healed = []
    all_ok = True

    for container, service in CONTAINERS.items():
        container_state = get_container_state(container)
        status = container_state.get("Status", "unknown")
        running = container_state.get("Running", False)
        health = get_health(container)

        if not running or (health == "unhealthy"):
            all_ok = False
            heal_container(container, service, state, env)
            healed.append(container)
        else:
            # Healthy — reset failure count
            state_key = f"{container}:failure_count"
            if state.get(state_key, 0) > 0:
                state[state_key] = 0
                save_state(state)

    if all_ok:
        log("All containers healthy")

    # Backend-specific check: if backend is running but not responding
    if "oncall-backend" not in healed:
        code, _ = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "10", "http://localhost:8000/health"],
            capture_output=True, text=True, timeout=15,
        ).returncode, None
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "10", "http://localhost:8000/health"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or result.stdout.strip() != "200":
            log("Backend container running but not responding — restarting")
            docker("restart", "oncall-backend")
            send_email(
                subject="[watchdog] Backend not responding — restarted",
                body="Backend container was running but /health failed. Restarted.",
                env=env,
            )


if __name__ == "__main__":
    main()
