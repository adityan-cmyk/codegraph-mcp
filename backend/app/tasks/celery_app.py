def create_celery_app() -> dict[str, str]:
    return {"broker": "redis", "status": "stubbed"}