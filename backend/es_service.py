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
    "mappings": {
        "dynamic": True,
        "properties": {
            "chunk_id": {"type": "keyword"},
            "source_file": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "type": {"type": "keyword"},
            "content": {"type": "text"},
            "chunk_index": {"type": "integer"},
            "is_faq": {"type": "boolean"},
            "faq": {"type": "boolean"},
            "faq_question": {"type": "text"},
        },
    }
}


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


def ensure_index(client=None) -> bool:
    if not ES_ENABLED:
        return False

    resolved_client = client or get_es_client()

    if resolved_client.indices.exists(index=ES_INDEX_NAME):
        return True

    resolved_client.indices.create(index=ES_INDEX_NAME, body=_INDEX_BODY)
    return True


def _build_es_source(doc: Document) -> tuple[str, dict[str, Any]]:
    metadata = sanitize_metadata(doc.metadata)
    chunk_index = int(metadata.get("chunk_index", 1))
    chunk_id = build_chunk_id(metadata, chunk_index, doc.page_content)
    source = {
        **metadata,
        "chunk_id": chunk_id,
        "content": doc.page_content,
    }
    return chunk_id, source


def upsert_chunks_to_es(chunks: list[Document]) -> dict[str, Any]:
    if not chunks:
        return {
            "enabled": ES_ENABLED,
            "index_name": ES_INDEX_NAME,
            "indexed_count": 0,
            "sync_seconds": 0,
        }

    if not ES_ENABLED:
        _warn_disabled("BM25 upsert")
        return {
            "enabled": False,
            "index_name": ES_INDEX_NAME,
            "indexed_count": 0,
            "sync_seconds": 0,
        }

    client = get_es_client()

    ensure_index(client)
    operations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_count = 0
    for doc in chunks:
        chunk_id, source = _build_es_source(doc)
        if chunk_id in seen_ids:
            duplicate_count += 1
            continue

        seen_ids.add(chunk_id)
        operations.append({"index": {"_index": ES_INDEX_NAME, "_id": chunk_id}})
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
        "index_name": ES_INDEX_NAME,
        "indexed_count": max(len(seen_ids) - error_count, 0),
        "sync_seconds": round(sync_elapsed, 2),
    }


def delete_by_source_file_in_es(source_file: str) -> int:
    cleaned = source_file.strip().lstrip("/\\")
    if not cleaned:
        raise ValueError("source_file is empty")

    if not ES_ENABLED:
        _warn_disabled("BM25 delete")
        return 0

    client = get_es_client()
    if not client.indices.exists(index=ES_INDEX_NAME):
        return 0

    response = client.delete_by_query(
        index=ES_INDEX_NAME,
        query={"term": {"source_file": cleaned}},
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted", 0))


def clear_es_index() -> int:
    if not ES_ENABLED:
        _warn_disabled("BM25 clear")
        return 0

    client = get_es_client()
    if not client.indices.exists(index=ES_INDEX_NAME):
        return 0

    response = client.delete_by_query(
        index=ES_INDEX_NAME,
        query={"match_all": {}},
        conflicts="proceed",
        refresh=True,
    )
    return int(response.get("deleted", 0))


def search_bm25_documents(query: str, source_files: list[str] | None, limit: int) -> list[Document]:
    if limit <= 0:
        return []

    if not ES_ENABLED:
        _warn_disabled("BM25 search")
        return []

    client = get_es_client()

    filters: list[dict[str, Any]] = []
    if source_files:
        filters.append({"terms": {"source_file": source_files}})

    response = client.search(
        index=ES_INDEX_NAME,
        query={
            "bool": {
                "must": [{"match": {"content": {"query": query}}}],
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