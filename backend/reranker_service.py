from __future__ import annotations

from typing import Iterable

from langchain_core.documents import Document

from rag_config import RERANKER_DEVICE, RERANKER_ENABLED, RERANKER_MODEL_NAME

try:
    from sentence_transformers.cross_encoder import CrossEncoder
except ImportError:
    CrossEncoder = None


_reranker = None
_reranker_unavailable_reason: str | None = None


def _coerce_score(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def get_reranker():
    global _reranker, _reranker_unavailable_reason

    if not RERANKER_ENABLED:
        if _reranker_unavailable_reason is None:
            _reranker_unavailable_reason = "disabled by config"
            print(f"[reranker] disabled: {_reranker_unavailable_reason}")
        return None

    if _reranker is not None:
        return _reranker

    if _reranker_unavailable_reason is not None:
        return None

    if CrossEncoder is None:
        _reranker_unavailable_reason = "sentence-transformers CrossEncoder 不可用"
        print(f"[reranker] disabled: {_reranker_unavailable_reason}")
        return None

    try:
        print(f"[reranker] loading model: {RERANKER_MODEL_NAME} (device={RERANKER_DEVICE})")
        _reranker = CrossEncoder(RERANKER_MODEL_NAME, device=RERANKER_DEVICE)
        return _reranker
    except Exception as exc:
        _reranker_unavailable_reason = str(exc)
        print(f"[reranker] fallback to vector ranking: {_reranker_unavailable_reason}")
        return None


def _attach_retrieval_metadata(documents: Iterable[Document]) -> list[Document]:
    enriched_documents: list[Document] = []

    for index, doc in enumerate(documents, start=1):
        doc.metadata = {
            **doc.metadata,
            "retrieval_rank": index,
        }
        enriched_documents.append(doc)

    return enriched_documents


def rerank_documents(query: str, documents: list[Document]) -> list[Document]:
    if not documents:
        return []

    enriched_documents = _attach_retrieval_metadata(documents)
    reranker = get_reranker()
    if reranker is None:
        return enriched_documents

    pairs = [(query, doc.page_content) for doc in enriched_documents]
    try:
        scores = reranker.predict(pairs)
    except Exception as exc:
        print(f"[reranker] predict failed, use vector ranking only: {exc}")
        return enriched_documents

    reranked_documents: list[Document] = []
    for doc, score in zip(enriched_documents, scores):
        doc.metadata = {
            **doc.metadata,
            "reranker_score": round(_coerce_score(score), 6),
        }
        reranked_documents.append(doc)

    reranked_documents.sort(
        key=lambda doc: _coerce_score(doc.metadata.get("reranker_score")),
        reverse=True,
    )

    if reranked_documents:
        preview = [
            f"{doc.metadata.get('source_file', 'unknown')}={_coerce_score(doc.metadata.get('reranker_score')):.4f}"
            for doc in reranked_documents[:3]
        ]
        print(f"[reranker] top scores: {preview}")

    return reranked_documents