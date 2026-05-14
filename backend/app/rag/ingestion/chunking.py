def chunk_rust_source(source: str) -> list[str]:
    return [block.strip() for block in source.split("\n\n") if block.strip()]