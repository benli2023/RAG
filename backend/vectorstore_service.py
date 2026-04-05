import time
import sys
from typing import Iterable, List

from langchain_core.documents import Document

from documents_service import build_chunk_id, sanitize_metadata
from retrieval_response_formatter import extract_context_labels


DEFAULT_EMBEDDING_BATCH_SIZE = 32
_progress_line_length = 0


def _write_progress_line(message: str) -> None:
    global _progress_line_length

    padding = max(_progress_line_length - len(message), 0)
    sys.stdout.write(f"\r{message}{' ' * padding}")
    _progress_line_length = len(message)
    sys.stdout.flush()


def _batch_iter(items: list[str], batch_size: int) -> Iterable[tuple[int, list[str]]]:
    for start_index in range(0, len(items), batch_size):
        yield start_index, items[start_index : start_index + batch_size]


def upsert_chunks(chunks: List[Document], embedding_function, vectorstore) -> dict:
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

    total_documents = len(unique_documents)
    batch_size = min(DEFAULT_EMBEDDING_BATCH_SIZE, total_documents)
    _write_progress_line(f"[embed_documents] 0/{total_documents} (0%) start embedding chunks")
    add_start = time.perf_counter()
    embeddings = []
    embedded_count = 0

    for batch_start, document_batch in _batch_iter(unique_documents, batch_size):
        batch_start_time = time.perf_counter()
        batch_embeddings = embedding_function.embed_documents(document_batch)
        embeddings.extend(batch_embeddings)
        embedded_count += len(document_batch)
        batch_elapsed = time.perf_counter() - batch_start_time
        progress_ratio = embedded_count / total_documents if total_documents else 1.0
        _write_progress_line(
            f"[embed_documents] {embedded_count}/{total_documents} ({progress_ratio:.0%}) "
            f"batch={batch_start // batch_size + 1} size={len(document_batch)} "
            f"batch_seconds={batch_elapsed:.2f}s"
        )

    embed_elapsed = time.perf_counter() - add_start

    global _progress_line_length

    sys.stdout.write("\r")
    if _progress_line_length:
        sys.stdout.write(" " * _progress_line_length)
        sys.stdout.write("\r")
    _progress_line_length = 0
    sys.stdout.flush()
    print(f"[embed_documents] embedded {len(unique_documents)} chunks in {embed_elapsed:.2f}s")
    _write_progress_line(f"[add_documents] upserting {len(unique_documents)} chunks")
    upsert_start = time.perf_counter()
    vectorstore._collection.upsert(
        ids=unique_ids,
        documents=unique_documents,
        metadatas=unique_metadatas,
        embeddings=embeddings,
    )
    add_elapsed = time.perf_counter() - upsert_start
    sys.stdout.write("\r")
    if _progress_line_length:
        sys.stdout.write(" " * _progress_line_length)
        sys.stdout.write("\r")
    _progress_line_length = 0
    sys.stdout.flush()
    print(f"[add_documents] upserted {len(unique_documents)} chunks in {add_elapsed:.2f}s")

    return {
        "chunk_count": len(unique_documents),
        "embed_seconds": round(embed_elapsed, 2),
        "add_documents_seconds": round(add_elapsed, 2),
    }


def delete_by_source_file(source_file: str, vectorstore) -> int:
    cleaned = source_file.strip().lstrip("/\\")
    if not cleaned:
        raise ValueError("source_file is empty")

    records = vectorstore._collection.get(where={"source_file": cleaned}, include=[])
    ids = records.get("ids", [])
    if not ids:
        return 0

    vectorstore._collection.delete(ids=ids)
    return len(ids)


def clear_vectorstore(vectorstore) -> int:
    existing = vectorstore._collection.get(include=[])
    ids = existing.get("ids", [])
    if not ids:
        return 0

    vectorstore._collection.delete(ids=ids)
    return len(ids)


def get_chunk_statistics(vectorstore) -> dict:
    records = vectorstore._collection.get(include=["metadatas", "documents"])
    metadatas = records.get("metadatas", [])
    documents = records.get("documents", [])

    domain_counts = {}
    type_counts = {}
    source_file_counts = {}
    header_path_counts = {}
    chunk_lengths = []

    for metadata, document in zip(metadatas, documents):
        domain, doc_type, headers = extract_context_labels(metadata)
        source_file = str(metadata.get("source_file", "unknown"))

        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        type_counts[doc_type] = type_counts.get(doc_type, 0) + 1
        source_file_counts[source_file] = source_file_counts.get(source_file, 0) + 1
        header_path_counts[headers] = header_path_counts.get(headers, 0) + 1
        chunk_lengths.append(len(document))

    total_chunks = len(documents)
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
    records = vectorstore._collection.get(include=["metadatas"])
    metadatas = records.get("metadatas", [])

    grouped: dict[str, set[str]] = {}
    for metadata in metadatas:
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
