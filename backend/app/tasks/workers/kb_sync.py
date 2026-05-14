def enqueue_kb_sync(old_commit: str, new_commit: str) -> dict[str, str]:
    return {"old_commit": old_commit, "new_commit": new_commit, "status": "queued"}