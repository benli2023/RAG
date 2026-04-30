from __future__ import annotations

import re
from pathlib import Path

from rag_config import DEFAULT_KNOWLEDGE_BASE, DOCS_DIR, ES_INDEX_NAME


_COLLECTION_SAFE_PATTERN = re.compile(r"[^a-z0-9_]+")
_ES_INDEX_SAFE_PATTERN = re.compile(r"[^a-z0-9_-]+")


def normalize_knowledge_base_name(knowledge_base: str | None, *, fallback: str = DEFAULT_KNOWLEDGE_BASE) -> str:
    candidate = str(knowledge_base or "").strip()
    if not candidate:
        candidate = fallback.strip()

    if not candidate:
        raise ValueError("knowledge_base is empty")

    if "/" in candidate or "\\" in candidate or candidate in {".", ".."}:
        raise ValueError(f"invalid knowledge_base: {knowledge_base}")

    return candidate


def list_knowledge_bases() -> list[str]:
    if not DOCS_DIR.is_dir():
        return []

    return sorted(
        directory.name
        for directory in DOCS_DIR.iterdir()
        if directory.is_dir() and not directory.name.startswith(".")
    )


def create_knowledge_base(knowledge_base: str | None) -> tuple[Path, bool]:
    normalized_name = normalize_knowledge_base_name(knowledge_base, fallback="")
    if normalized_name.startswith("."):
        raise ValueError(f"invalid knowledge_base: {knowledge_base}")

    docs_root = DOCS_DIR.resolve()
    knowledge_base_dir = (docs_root / normalized_name).resolve()

    try:
        knowledge_base_dir.relative_to(docs_root)
    except ValueError as exc:
        raise ValueError(f"invalid knowledge_base: {knowledge_base}") from exc

    if knowledge_base_dir.exists() and not knowledge_base_dir.is_dir():
        raise FileExistsError(f"knowledge base path is not a directory: {normalized_name}")

    created = not knowledge_base_dir.exists()
    knowledge_base_dir.mkdir(parents=True, exist_ok=True)

    module_directory_file = knowledge_base_dir / "module-directory.yaml"
    if not module_directory_file.exists():
        module_directory_file.write_text("modules: []\n", encoding="utf-8")

    return knowledge_base_dir, created


def get_knowledge_base_dir(knowledge_base: str | None = None) -> Path:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    docs_dir = (DOCS_DIR / normalized_name).resolve()

    if not docs_dir.is_dir():
        raise FileNotFoundError(f"knowledge base not found: {normalized_name}")

    return docs_dir


def get_module_directory_file(knowledge_base: str | None = None) -> Path:
    return get_knowledge_base_dir(knowledge_base) / "module-directory.yaml"


def _to_collection_safe_name(value: str) -> str:
    cleaned = _COLLECTION_SAFE_PATTERN.sub("_", value.strip().lower()).strip("_")
    return cleaned or "default"


def _to_es_index_safe_name(value: str) -> str:
    cleaned = _ES_INDEX_SAFE_PATTERN.sub("-", value.strip().lower()).strip("-")
    return cleaned or "default"


def get_child_collection_name(knowledge_base: str | None = None) -> str:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    return f"kb_{_to_collection_safe_name(normalized_name)}__documents"


def get_parent_collection_name(knowledge_base: str | None = None) -> str:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    return f"kb_{_to_collection_safe_name(normalized_name)}__parent_documents"


def get_child_es_index_name(knowledge_base: str | None = None) -> str:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    return f"{_to_es_index_safe_name(ES_INDEX_NAME)}--{_to_es_index_safe_name(normalized_name)}"


def get_parent_es_index_name(knowledge_base: str | None = None) -> str:
    return f"{get_child_es_index_name(knowledge_base)}--parent"