from app.schemas.codebase import CodeChunk


def build_code_chunk(
    *,
    symbol_id: str,
    file_path: str,
    kind: str,
    content: str,
    start_line: int,
    end_line: int,
) -> CodeChunk:
    return CodeChunk(
        symbol_id=symbol_id,
        file_path=file_path,
        kind=kind,
        content=content.strip(),
        start_line=start_line,
        end_line=end_line,
    )