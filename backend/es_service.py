from __future__ import annotations

import time
from typing import Any

from langchain_core.documents import Document

from documents_service import build_chunk_id, sanitize_metadata
from rag_config import ES_ENABLED, ES_ENABLED_CONFIGURED, ES_INDEX_NAME, ES_URL

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None


_es_client = None
_es_unavailable_reason: str | None = None
_es_disabled_warned = False

_INDEX_BODY = {
    "settings": {
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
    },
    "mappings": {
        "dynamic": True,
        "properties": {
            "chunk_id": {"type": "keyword"},
            "source_file": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "type": {"type": "keyword"},
            "content": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "header_text": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "summary": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "keywords_text": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "sections_text": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "related_domains_text": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "parent_title": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
            "chunk_index": {"type": "integer"},
            "is_faq": {"type": "boolean"},
            "faq": {"type": "boolean"},
            "faq_question": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        },
    }
}


def _coerce_text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


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


def ensure_index(client=None, index_name: str = ES_INDEX_NAME) -> bool:
    if not ES_ENABLED:
        return False

    resolved_client = client or get_es_client()

    if resolved_client.indices.exists(index=index_name):
        return True

    resolved_client.indices.create(index=index_name, body=_INDEX_BODY)
    return True


def _build_es_source(doc: Document) -> tuple[str, dict[str, Any]]:
    metadata = sanitize_metadata(doc.metadata)
    chunk_index = int(metadata.get("chunk_index", 1))
    chunk_id = build_chunk_id(metadata, chunk_index, doc.page_content)

    # Build sections_text from the original (pre-sanitized) metadata to handle
    # list-of-dict structures that sanitize_metadata may have JSON-encoded.
    raw_sections = doc.metadata.get("sections") if doc.metadata else None
    sections_text = _coerce_sections_text(raw_sections)

    source = {
        **metadata,
        "chunk_id": chunk_id,
        "content": doc.page_content,
        "header_text": _build_header_text(metadata),
        "summary": _coerce_text(metadata.get("summary") or metadata.get("description")),
        "keywords_text": _coerce_list_to_text(metadata.get("keywords")),
        "sections_text": sections_text,
        "related_domains_text": _coerce_list_to_text(metadata.get("related_domains")),
        "parent_title": _coerce_text(metadata.get("parent_title")),
    }
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


def clear_es_index(index_name: str = ES_INDEX_NAME) -> int:
    if not ES_ENABLED:
        _warn_disabled("BM25 clear")
        return 0

    client = get_es_client()
    if not client.indices.exists(index=index_name):
        return 0

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