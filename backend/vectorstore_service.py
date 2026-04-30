import json
import time
import sys
from typing import List

from langchain_core.documents import Document

from documents_service import build_chunk_id, sanitize_metadata
from retrieval_response_formatter import extract_context_labels
from rag_config import REMOTE_RPC_GET_RECORDS_PAGE_SIZE, REMOTE_RPC_MAX_BATCH_BYTES


_progress_line_length = 0
MAX_RPC_BATCH_CHUNKS = 500


def _iter_vectorstore_records(vectorstore, include: List[str], batch_size: int = REMOTE_RPC_GET_RECORDS_PAGE_SIZE):
    if hasattr(vectorstore, "iter_records"):
        yield from vectorstore.iter_records(include=include, batch_size=batch_size)
        return

    offset = 0
    while True:
        records = vectorstore.get_records(include=include, limit=batch_size, offset=offset)
        yield records
        next_offset = int(records.get("next_offset", offset + len(records.get("ids", []))))
        if not records.get("has_more") or next_offset <= offset:
            break
        offset = next_offset


def _write_progress_line(message: str) -> None:
    global _progress_line_length

    padding = max(_progress_line_length - len(message), 0)
    sys.stdout.write(f"\r{message}{' ' * padding}")
    _progress_line_length = len(message)
    sys.stdout.flush()


def _estimate_upsert_payload_bytes(chunk_id: str, document: str, metadata: dict) -> int:
    metadata_json = json.dumps(metadata, ensure_ascii=True, default=str)
    return len(chunk_id.encode("utf-8")) + len(document.encode("utf-8")) + len(metadata_json.encode("utf-8")) + 256


def _build_upsert_batches(ids: list[str], documents: list[str], metadatas: list[dict]) -> list[tuple[list[str], list[str], list[dict]]]:
    batches: list[tuple[list[str], list[str], list[dict]]] = []
    current_ids: list[str] = []
    current_documents: list[str] = []
    current_metadatas: list[dict] = []
    current_bytes = 0

    for chunk_id, document, metadata in zip(ids, documents, metadatas):
        item_bytes = _estimate_upsert_payload_bytes(chunk_id, document, metadata)
        should_flush = bool(current_ids) and (
            len(current_ids) >= MAX_RPC_BATCH_CHUNKS
            or current_bytes + item_bytes > REMOTE_RPC_MAX_BATCH_BYTES
        )
        if should_flush:
            batches.append((current_ids, current_documents, current_metadatas))
            current_ids = []
            current_documents = []
            current_metadatas = []
            current_bytes = 0

        current_ids.append(chunk_id)
        current_documents.append(document)
        current_metadatas.append(metadata)
        current_bytes += item_bytes

    if current_ids:
        batches.append((current_ids, current_documents, current_metadatas))

    return batches


def upsert_chunks(chunks: List[Document], vectorstore) -> dict:
    if not chunks:
        return {
            "chunk_count": 0,
            "embed_seconds": 0,
            "add_documents_seconds": 0,
        }

    unique_ids = []
    unique_documents = []
    unique_metadatas = []
    seen_ids = set()
    duplicate_count = 0

    for doc in chunks:
        chunk_id = build_chunk_id(doc.metadata, int(doc.metadata.get("chunk_index", 1)), doc.page_content)
        if chunk_id in seen_ids:
            duplicate_count += 1
            continue

        seen_ids.add(chunk_id)
        unique_ids.append(chunk_id)
        unique_documents.append(doc.page_content)
        unique_metadatas.append(sanitize_metadata(doc.metadata))

    if duplicate_count:
        print(f"[upsert_chunks] skipped {duplicate_count} duplicate chunks by chunk_id")

    global _progress_line_length

    upsert_start = time.perf_counter()
    total_documents = len(unique_documents)
    batches = _build_upsert_batches(unique_ids, unique_documents, unique_metadatas)

    for batch_index, (batch_ids, batch_docs, batch_metas) in enumerate(batches, start=1):
        _write_progress_line(f"[add_documents] submitting batch {batch_index}/{len(batches)} ({len(batch_docs)} chunks) to RPC")
        vectorstore.upsert(
            ids=batch_ids,
            documents=batch_docs,
            metadatas=batch_metas,
            embeddings=[],  # 传空向量从而触发 RAG-RPC 进行向量计算
        )

    add_elapsed = time.perf_counter() - upsert_start
    
    sys.stdout.write("\r")
    if _progress_line_length:
        sys.stdout.write(" " * _progress_line_length)
        sys.stdout.write("\r")
    _progress_line_length = 0
    sys.stdout.flush()
    print(f"[add_documents] RPC completely processed {total_documents} chunks in {add_elapsed:.2f}s")

    return {
        "chunk_count": len(unique_documents),
        "embed_seconds": 0.0,
        "add_documents_seconds": round(add_elapsed, 2),
    }


def delete_by_source_file(source_file: str, vectorstore) -> int:
    cleaned = source_file.strip().lstrip("/\\")
    if not cleaned:
        raise ValueError("source_file is empty")

    return vectorstore.delete_by_source_file(cleaned)


def clear_vectorstore(vectorstore) -> int:
    return vectorstore.clear()


def get_chunk_statistics(vectorstore) -> dict:
    domain_counts = {}
    type_counts = {}
    source_file_counts = {}
    header_path_counts = {}
    chunk_lengths = []

    for records in _iter_vectorstore_records(vectorstore, include=["metadatas", "documents"]):
        metadatas = records.get("metadatas", [])
        documents = records.get("documents", [])

        for metadata, document in zip(metadatas, documents):
            domain, doc_type, headers = extract_context_labels(metadata)
            source_file = str(metadata.get("source_file", "unknown"))

            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1
            source_file_counts[source_file] = source_file_counts.get(source_file, 0) + 1
            header_path_counts[headers] = header_path_counts.get(headers, 0) + 1
            chunk_lengths.append(len(document))

    total_chunks = len(chunk_lengths)
    average_chunk_length = round(sum(chunk_lengths) / total_chunks, 2) if total_chunks else 0

    return {
        "total_chunks": total_chunks,
        "domain_counts": domain_counts,
        "type_counts": type_counts,
        "source_file_counts": source_file_counts,
        "header_path_counts": header_path_counts,
        "average_chunk_length": average_chunk_length,
        "max_chunk_length": max(chunk_lengths) if chunk_lengths else 0,
        "min_chunk_length": min(chunk_lengths) if chunk_lengths else 0,
    }


def get_grouped_source_files_from_vectorstore(vectorstore) -> List[dict]:
    grouped: dict[str, set[str]] = {}
    for records in _iter_vectorstore_records(vectorstore, include=["metadatas"]):
        for metadata in records.get("metadatas", []):
            domain = str(metadata.get("domain", "unknown"))
            source_file = str(metadata.get("source_file", "")).strip()
            if not source_file:
                continue

            if domain not in grouped:
                grouped[domain] = set()
            grouped[domain].add(source_file)

    return [
        {
            "domain": domain,
            "document_count": len(source_files),
            "source_files": sorted(source_files),
        }
        for domain, source_files in sorted(grouped.items(), key=lambda item: item[0])
    ]
