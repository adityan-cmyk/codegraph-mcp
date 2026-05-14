def run_cargo_test(test_target: str) -> dict[str, str]:
    return {"target": test_target, "status": "queued"}