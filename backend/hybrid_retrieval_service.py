from __future__ import annotations

import hashlib

from documents_service import build_chunk_id
from langchain_core.documents import Document


def _document_key(doc: Document) -> str:
    metadata = doc.metadata or {}
    chunk_id = metadata.get("chunk_id")
    if chunk_id not in (None, ""):
        return str(chunk_id)

    source_file = metadata.get("source_file")
    chunk_index = metadata.get("chunk_index")
    if source_file not in (None, "") and chunk_index not in (None, ""):
        try:
            normalized_chunk_id = build_chunk_id(metadata, int(chunk_index), doc.page_content)
            doc.metadata["chunk_id"] = normalized_chunk_id
            return normalized_chunk_id
        except (TypeError, ValueError):
            pass

    stable_parts = [
        source_file,
        chunk_index,
        metadata.get("type"),
        metadata.get("Header 1"),
        metadata.get("Header 2"),
        metadata.get("Header 3"),
    ]
    stable_key = "|".join(str(value) for value in stable_parts if value not in (None, ""))
    if stable_key:
        return stable_key
    return hashlib.sha256(doc.page_content.encode("utf-8")).hexdigest()


def reciprocal_rank_fusion(vector_results: list[Document], bm25_results: list[Document], k: int = 60) -> list[Document]:
    rrf_scores: dict[str, float] = {}
    fused_docs: dict[str, Document] = {}

    def add_channel(results: list[Document], channel_name: str) -> None:
        for rank, doc in enumerate(results, start=1):
            doc_key = _document_key(doc)
            if doc_key not in fused_docs:
                fused_docs[doc_key] = Document(page_content=doc.page_content, metadata=dict(doc.metadata))

            fused_doc = fused_docs[doc_key]
            score = 1.0 / (k + rank)
            rrf_scores[doc_key] = rrf_scores.get(doc_key, 0.0) + score
            fused_doc.metadata[f"{channel_name}_rank"] = rank
            fused_doc.metadata["rrf_score"] = round(rrf_scores[doc_key], 6)

            retrieval_sources = list(fused_doc.metadata.get("retrieval_sources", []))
            if channel_name not in retrieval_sources:
                retrieval_sources.append(channel_name)
            fused_doc.metadata["retrieval_sources"] = retrieval_sources

    add_channel(vector_results, "vector")
    add_channel(bm25_results, "bm25")

    return sorted(
        fused_docs.values(),
        key=lambda doc: (
            float(doc.metadata.get("rrf_score", 0.0)),
            -int(doc.metadata.get("vector_rank", 10**6)),
            -int(doc.metadata.get("bm25_rank", 10**6)),
        ),
        reverse=True,
    )