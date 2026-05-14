def get_blast_radius(symbol_id: str) -> dict[str, list[str]]:
    return {"symbol_id": symbol_id, "upstream": [], "downstream": []}