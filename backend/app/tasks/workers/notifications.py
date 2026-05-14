def publish_notification(message: str) -> dict[str, str]:
    return {"message": message, "channel": "incident-updates"}