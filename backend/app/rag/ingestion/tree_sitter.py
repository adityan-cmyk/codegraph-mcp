def generate_symbol_id(module_path: str, symbol_name: str) -> str:
    normalized_module = module_path.replace("/", "::").strip(":")
    return f"{normalized_module}::{symbol_name}"