import time
from typing import List

from langchain_core.documents import Document

from documents_service import build_chunk_id, sanitize_metadata
from retrieval_response_formatter import extract_context_labels


def upsert_chunks(chunks: List[Document], embedding_function, vectorstore) -> dict:
    if not chunks:
        return {
            "chunk_count": 0,
            "embed_seconds": 0,
            "add_documents_seconds": 0,
        }

    ids = [build_chunk_id(doc.metadata, int(doc.metadata.get("chunk_index", 1))) for doc in chunks]
    documents = [doc.page_content for doc in chunks]
    metadatas = [sanitize_metadata(doc.metadata) for doc in chunks]

    print(f"[embed_documents] start embedding {len(chunks)} chunks with local BGE model...")
    add_start = time.perf_counter()
    embeddings = embedding_function.embed_documents(documents)
    embed_elapsed = time.perf_counter() - add_start

    print(f"[embed_documents] embedded {len(chunks)} chunks in {embed_elapsed:.2f}s")
    print(f"[add_documents] start upserting {len(chunks)} chunks...")
    upsert_start = time.perf_counter()
    vectorstore._collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=embeddings,
    )
    add_elapsed = time.perf_counter() - upsert_start
    print(f"[add_documents] upserted {len(chunks)} chunks in {add_elapsed:.2f}s")

    return {
        "chunk_count": len(chunks),
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
