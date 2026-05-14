def run_in_sandbox(command: list[str]) -> dict[str, object]:
    return {"command": command, "stdout": [], "stderr": [], "exit_code": 0}