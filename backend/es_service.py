from __future__ import annotations

import time
from typing import Any

from langchain_core.documents import Document

from documents_service import build_chunk_id
from rag_config import ES_ENABLED, ES_ENABLED_CONFIGURED, ES_INDEX_NAME, ES_PARENT_INDEX_NAME, ES_URL

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None


_es_client = None
_es_unavailable_reason: str | None = None
_es_disabled_warned = False
_es_analyzer_fallback_warned = False


def _resolve_analyzer_mode_from_mapping(mapping: dict[str, Any]) -> str:
    properties = mapping.get("properties", {}) if isinstance(mapping, dict) else {}
    content_mapping = properties.get("content", {}) if isinstance(properties, dict) else {}
    analyzer = str(content_mapping.get("analyzer", "")).strip()
    search_analyzer = str(content_mapping.get("search_analyzer", "")).strip()

    if analyzer == "ik_max_word" and search_analyzer == "ik_smart":
        return "ik"
    if analyzer or search_analyzer:
        return "custom"
    return "standard"

def _text_field_mapping(use_ik_analyzer: bool) -> dict[str, Any]:
    if not use_ik_analyzer:
        return {"type": "text"}
    return {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"}


def _build_index_body(use_ik_analyzer: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "knowledge_base": {"type": "keyword"},
                "source_file": {"type": "keyword"},
                "domain": {"type": "keyword"},
                "type": {"type": "keyword"},
                "Header 1": {"type": "keyword"},
                "Header 2": {"type": "keyword"},
                "Header 3": {"type": "keyword"},
                "related_domains": {"type": "keyword"},
                "content": _text_field_mapping(use_ik_analyzer),
                "header_text": _text_field_mapping(use_ik_analyzer),
                "summary": _text_field_mapping(use_ik_analyzer),
                "keywords_text": _text_field_mapping(use_ik_analyzer),
                "sections_text": _text_field_mapping(use_ik_analyzer),
                "related_domains_text": _text_field_mapping(use_ik_analyzer),
                "parent_title": _text_field_mapping(use_ik_analyzer),
                "chunk_index": {"type": "integer"},
                "is_faq": {"type": "boolean"},
                "faq": {"type": "boolean"},
                "faq_question": _text_field_mapping(use_ik_analyzer),
            },
        },
    }
    if use_ik_analyzer:
        body["settings"] = {
            "analysis": {
                "analyzer": {
                    "default": {
                        "type": "ik_max_word"
                    },
                    "default_search": {
                        "type": "ik_smart"
                    }
                }
            }
        }
    return body


def _coerce_text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _coerce_keyword_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        cleaned = value.strip()
        raw_values = [cleaned] if cleaned else []
    else:
        raw_values = []

    normalized_values: list[str] = []
    for item in raw_values:
        cleaned_item = str(item).strip()
        if cleaned_item and cleaned_item not in normalized_values:
            normalized_values.append(cleaned_item)
    return normalized_values


def _drop_empty_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", [])
    }


def _coerce_list_to_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return _coerce_text(value)


def _coerce_sections_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    titles: list[str] = []
    for item in value:
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip()
        else:
            title = str(item).strip()
        if title:
            titles.append(title)
    return " ".join(titles)


def _build_header_text(metadata: dict[str, Any]) -> str:
    header_values = [
        str(metadata.get(header_key, "")).strip()
        for header_key in ("Header 1", "Header 2", "Header 3")
        if str(metadata.get(header_key, "")).strip()
    ]
    return " > ".join(header_values)


def _mark_unavailable(reason: str) -> None:
    global _es_unavailable_reason

    if _es_unavailable_reason is None:
        _es_unavailable_reason = reason
        print(f"[elasticsearch] unavailable: {reason}")


def _raise_unavailable() -> None:
    reason = _es_unavailable_reason or "Elasticsearch is unavailable"
    raise RuntimeError(f"Elasticsearch is unavailable: {reason}")


def _warn_disabled(action: str) -> None:
    global _es_disabled_warned

    if _es_disabled_warned:
        return

    _es_disabled_warned = True
    if ES_ENABLED_CONFIGURED:
        reason = "disabled by config"
    else:
        reason = "not configured"
    print(f"[elasticsearch] {reason}; skip {action}")


def _warn_analyzer_fallback() -> None:
    global _es_analyzer_fallback_warned

    if _es_analyzer_fallback_warned:
        return

    _es_analyzer_fallback_warned = True
    print("[elasticsearch] IK analyzer unavailable; fallback to standard text analyzer")


def get_es_client():
    global _es_client

    if not ES_ENABLED:
        _warn_disabled("client initialization")
        return None

    if _es_client is not None:
        return _es_client

    if _es_unavailable_reason is not None:
        _raise_unavailable()

    if Elasticsearch is None:
        _mark_unavailable("python package 'elasticsearch' is not installed")
        _raise_unavailable()

    try:
        client = Elasticsearch(ES_URL, request_timeout=5)
        if not client.ping():
            raise RuntimeError(f"cannot connect to {ES_URL}")
        ensure_index(client)
        _es_client = client
        print(f"[elasticsearch] connected: {ES_URL}, index={ES_INDEX_NAME}")
        return _es_client
    except Exception as exc:
        _mark_unavailable(str(exc))
        _raise_unavailable()


def ensure_index(client=None, index_name: str = ES_INDEX_NAME, recreate: bool = False) -> bool:
    if not ES_ENABLED:
        return False

    resolved_client = client or get_es_client()

    if recreate and resolved_client.indices.exists(index=index_name):
        resolved_client.indices.delete(index=index_name)

    if resolved_client.indices.exists(index=index_name):
        return True

    try:
        resolved_client.indices.create(index=index_name, body=_build_index_body(use_ik_analyzer=True))
    except Exception as exc:
        if "Unknown analyzer type" not in str(exc):
            raise
        _warn_analyzer_fallback()
        resolved_client.indices.create(index=index_name, body=_build_index_body(use_ik_analyzer=False))
    return True


def _build_es_source(doc: Document) -> tuple[str, dict[str, Any]]:
    metadata = dict(doc.metadata or {})
    chunk_index = _coerce_int(metadata.get("chunk_index", 1), default=1)
    chunk_id = build_chunk_id(metadata, chunk_index, doc.page_content)

    raw_sections = metadata.get("sections")
    sections_text = _coerce_sections_text(raw_sections)
    related_domains = _coerce_keyword_list(metadata.get("related_domains"))

    source = _drop_empty_fields({
        "chunk_id": chunk_id,
        "knowledge_base": _coerce_text(metadata.get("knowledge_base")),
        "source_file": _coerce_text(metadata.get("source_file")),
        "domain": _coerce_text(metadata.get("domain")),
        "type": _coerce_text(metadata.get("type")),
        "chunk_index": chunk_index,
        "content": doc.page_content,
        "header_text": _build_header_text(metadata),
        "Header 1": _coerce_text(metadata.get("Header 1")),
        "Header 2": _coerce_text(metadata.get("Header 2")),
        "Header 3": _coerce_text(metadata.get("Header 3")),
        "summary": _coerce_text(metadata.get("summary") or metadata.get("description")),
        "keywords_text": _coerce_list_to_text(metadata.get("keywords")),
        "sections_text": sections_text,
        "related_domains": related_domains,
        "related_domains_text": _coerce_list_to_text(related_domains),
        "parent_title": _coerce_text(metadata.get("parent_title")),
        "faq": _coerce_bool(metadata.get("faq")),
        "is_faq": _coerce_bool(metadata.get("is_faq")),
        "faq_question": _coerce_text(metadata.get("faq_question")),
    })
    return chunk_id, source


def upsert_chunks_to_es(chunks: list[Document], index_name: str = ES_INDEX_NAME) -> dict[str, Any]:
    if not chunks:
        return {
            "enabled": ES_ENABLED,
            "index_name": index_name,
            "indexed_count": 0,
            "sync_seconds": 0,
        }

    if not ES_ENABLED:
        _warn_disabled("BM25 upsert")
        return {
            "enabled": False,
            "index_name": index_name,
            "indexed_count": 0,
            "sync_seconds": 0,
        }

    client = get_es_client()

    ensure_index(client, index_name=index_name)
    operations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_count = 0
    for doc in chunks:
        chunk_id, source = _build_es_source(doc)
        if chunk_id in seen_ids:
            duplicate_count += 1
            continue

        seen_ids.add(chunk_id)
        operations.append({"index": {"_index": index_name, "_id": chunk_id}})
        operations.append(source)

    if duplicate_count:
        print(f"[elasticsearch] skipped {duplicate_count} duplicate chunks by chunk_id")

    sync_start = time.perf_counter()
    response = client.bulk(operations=operations, refresh=True)
    sync_elapsed = time.perf_counter() - sync_start
    items = response.get("items", [])
    error_count = sum(1 for item in items if item.get("index", {}).get("error"))

    if error_count:
        print(f"[elasticsearch] bulk upsert completed with {error_count} errors")

    return {
        "enabled": True,
        "index_name": index_name,
        "indexed_count": max(len(seen_ids) - error_count, 0),
        "sync_seconds": round(sync_elapsed, 2),
    }


def delete_by_source_file_in_es(source_file: str, index_name: str = ES_INDEX_NAME) -> int:
    cleaned = source_file.strip().lstrip("/\\")
    if not cleaned:
        raise ValueError("source_file is empty")

    if not ES_ENABLED:
        _warn_disabled("BM25 delete")
        return 0

    client = get_es_client()
    if not client.indices.exists(index=index_name):
        return 0

    response = client.delete_by_query(
        index=index_name,
        query={"term": {"source_file": cleaned}},
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted", 0))


def clear_es_index(index_name: str = ES_INDEX_NAME, recreate: bool = False) -> int:
    if not ES_ENABLED:
        _warn_disabled("BM25 clear")
        return 0

    client = get_es_client()
    if not client.indices.exists(index=index_name):
        if recreate:
            ensure_index(client, index_name=index_name)
        return 0

    existing_count = int(client.count(index=index_name).get("count", 0))

    if recreate:
        ensure_index(client, index_name=index_name, recreate=True)
        return existing_count

    response = client.delete_by_query(
        index=index_name,
        query={"match_all": {}},
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted", 0))


def search_bm25_documents(
    query: str,
    source_files: list[str] | None,
    limit: int,
    domains: list[str] | None = None,
    faq_only: bool = False,
    index_name: str = ES_INDEX_NAME,
) -> list[Document]:
    if limit <= 0:
        return []

    if not ES_ENABLED:
        _warn_disabled("BM25 search")
        return []

    client = get_es_client()

    filters: list[dict[str, Any]] = []
    if source_files:
        filters.append({"terms": {"source_file": source_files}})
    if domains:
        filters.append({"terms": {"domain": domains}})
    if faq_only:
        filters.append({"term": {"is_faq": True}})

    response = client.search(
        index=index_name,
        query={
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "type": "most_fields",
                            "fields": [
                                "content",
                                "header_text^3",
                                "faq_question^3",
                                "summary^2",
                                "keywords_text^3",
                                "sections_text^2",
                                "related_domains_text",
                                "parent_title^2",
                            ],
                        }
                    }
                ],
                "filter": filters,
            }
        },
        size=limit,
    )

    documents: list[Document] = []
    for rank, hit in enumerate(response.get("hits", {}).get("hits", []), start=1):
        source = dict(hit.get("_source", {}))
        page_content = str(source.pop("content", "")).strip()
        if not page_content:
            continue

        source.setdefault("chunk_id", hit.get("_id"))
        source["bm25_score"] = round(float(hit.get("_score", 0.0)), 6)
        source["bm25_rank"] = rank
        documents.append(Document(page_content=page_content, metadata=source))

    return documents


def get_es_runtime_config(index_names: list[str] | None = None) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "enabled": ES_ENABLED,
        "configured": ES_ENABLED_CONFIGURED,
        "url": ES_URL,
        "available": False,
        "analyzer_mode": "disabled" if not ES_ENABLED else "unknown",
        "analyzer_modes_by_index": {},
        "index_exists_by_name": {},
        "ik_fallback_active": False,
        "unavailable_reason": _es_unavailable_reason,
    }

    if not ES_ENABLED:
        return runtime

    try:
        client = get_es_client()
    except Exception as exc:
        runtime["unavailable_reason"] = str(exc)
        return runtime

    runtime["available"] = client is not None
    if client is None:
        return runtime

    analyzer_modes_by_index: dict[str, str] = {}
    index_exists_by_name: dict[str, bool] = {}

    resolved_index_names = index_names or [ES_INDEX_NAME, ES_PARENT_INDEX_NAME]

    for index_name in resolved_index_names:
        exists = bool(client.indices.exists(index=index_name))
        index_exists_by_name[index_name] = exists
        if not exists:
            continue

        mapping_payload = client.indices.get_mapping(index=index_name)
        mapping = mapping_payload.get(index_name, {}).get("mappings", {})
        analyzer_modes_by_index[index_name] = _resolve_analyzer_mode_from_mapping(mapping)

    runtime["index_exists_by_name"] = index_exists_by_name
    runtime["analyzer_modes_by_index"] = analyzer_modes_by_index
    runtime["ik_fallback_active"] = any(mode == "standard" for mode in analyzer_modes_by_index.values())

    distinct_modes = sorted(set(analyzer_modes_by_index.values()))
    if not distinct_modes:
        runtime["analyzer_mode"] = "unknown"
    elif len(distinct_modes) == 1:
        runtime["analyzer_mode"] = distinct_modes[0]
    else:
        runtime["analyzer_mode"] = "mixed"

    return runtime