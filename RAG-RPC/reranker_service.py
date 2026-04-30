from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from langchain_core.documents import Document

from rag_config import RERANKER_BATCH_SIZE, RERANKER_DEVICE, RERANKER_ENABLED, RERANKER_MODEL_NAME

try:
    from sentence_transformers.cross_encoder import CrossEncoder
except ImportError:
    CrossEncoder = None


_reranker = None
_reranker_unavailable_reason: str | None = None


def _path_state(model_name: str) -> dict[str, object]:
    path = Path(model_name).expanduser()
    looks_like_path = path.is_absolute() or model_name.startswith(("./", "../", "~")) or "\\" in model_name
    return {
        "path": str(path) if looks_like_path else None,
        "path_like": looks_like_path,
        "path_exists": path.exists() if looks_like_path else None,
    }


def get_reranker_runtime_status() -> dict[str, object]:
    path_status = _path_state(RERANKER_MODEL_NAME)

    if not RERANKER_ENABLED:
        return {
            "status": "ok",
            "state": "disabled",
            "enabled": False,
            "loaded": False,
            "model_name": RERANKER_MODEL_NAME,
            "device": RERANKER_DEVICE,
            **path_status,
        }

    if _reranker is not None:
        return {
            "status": "ok",
            "state": "loaded",
            "enabled": True,
            "loaded": True,
            "model_name": RERANKER_MODEL_NAME,
            "device": RERANKER_DEVICE,
            **path_status,
        }

    if _reranker_unavailable_reason is not None:
        return {
            "status": "error",
            "state": "unavailable",
            "enabled": True,
            "loaded": False,
            "model_name": RERANKER_MODEL_NAME,
            "device": RERANKER_DEVICE,
            "unavailable_reason": _reranker_unavailable_reason,
            **path_status,
        }

    missing_local_path = path_status["path_like"] and not path_status["path_exists"]
    return {
        "status": "error" if missing_local_path else "warning",
        "state": "local_model_missing" if missing_local_path else "configured_not_loaded",
        "enabled": True,
        "loaded": False,
        "model_name": RERANKER_MODEL_NAME,
        "device": RERANKER_DEVICE,
        **path_status,
    }


def _coerce_score(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fallback_relevance_score(doc: Document) -> float:
    for metadata_key in ("rrf_score", "bm25_score", "vector_score"):
        if metadata_key in doc.metadata:
            return _coerce_score(doc.metadata.get(metadata_key))

    retrieval_rank = _coerce_score(doc.metadata.get("retrieval_rank"))
    if retrieval_rank > 0:
        return 1.0 / retrieval_rank
    return 0.0


def _elapsed_seconds(start_time: float) -> float:
    return round(time.perf_counter() - start_time, 6)


def _coerce_batch_size(batch_size: int | None) -> int:
    try:
        normalized = int(batch_size or RERANKER_BATCH_SIZE)
    except (TypeError, ValueError):
        normalized = RERANKER_BATCH_SIZE
    return max(1, normalized)


def _summarize_seconds(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "avg": None}
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "avg": round(sum(values) / len(values), 6),
    }


def _build_base_metrics(candidate_count: int, batch_size: int) -> dict[str, object]:
    return {
        "enabled": RERANKER_ENABLED,
        "model_name": RERANKER_MODEL_NAME,
        "device": RERANKER_DEVICE,
        "candidate_count": candidate_count,
        "batch_size": batch_size,
        "batch_count": 0,
        "scored_count": 0,
        "failed_batch_count": 0,
        "fallback_used": False,
        "failure_reason": None,
        "model_resolve_seconds": 0.0,
        "predict_seconds": 0.0,
        "total_seconds": 0.0,
        "batch_timings": [],
        "batch_seconds": {"min": None, "max": None, "avg": None},
        "status": "not_started",
    }


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


def predict_relevance_scores_with_metrics(
    query: str,
    texts: list[str],
    batch_size: int | None = None,
) -> tuple[list[float], dict[str, object]]:
    effective_batch_size = _coerce_batch_size(batch_size)
    metrics = _build_base_metrics(len(texts), effective_batch_size)
    total_start = time.perf_counter()
    if not texts:
        metrics["status"] = "no_candidates"
        metrics["total_seconds"] = _elapsed_seconds(total_start)
        return [], metrics

    model_start = time.perf_counter()
    reranker = get_reranker()
    metrics["model_resolve_seconds"] = _elapsed_seconds(model_start)
    if reranker is None:
        metrics["status"] = "unavailable"
        metrics["fallback_used"] = True
        metrics["failure_reason"] = _reranker_unavailable_reason or "reranker unavailable"
        metrics["total_seconds"] = _elapsed_seconds(total_start)
        return [], metrics

    scores: list[float] = []
    batch_timings: list[dict[str, object]] = []
    predict_start = time.perf_counter()
    for batch_index, start_index in enumerate(range(0, len(texts), effective_batch_size), start=1):
        batch_texts = texts[start_index:start_index + effective_batch_size]
        pairs = [(query, text) for text in batch_texts]
        batch_start = time.perf_counter()
        try:
            batch_scores = [_coerce_score(score) for score in reranker.predict(pairs, batch_size=effective_batch_size)]
        except Exception as exc:
            batch_seconds = _elapsed_seconds(batch_start)
            metrics["failed_batch_count"] = int(metrics["failed_batch_count"]) + 1
            metrics["fallback_used"] = True
            metrics["failure_reason"] = str(exc)
            metrics["status"] = "predict_failed"
            batch_timings.append({
                "batch_index": batch_index,
                "candidate_count": len(batch_texts),
                "seconds": batch_seconds,
                "status": "error",
                "failure_reason": str(exc),
            })
            print(f"[reranker] predict batch failed, skip scoring: batch={batch_index}, size={len(batch_texts)}, error={exc}")
            break

        batch_seconds = _elapsed_seconds(batch_start)
        scores.extend(batch_scores)
        batch_timings.append({
            "batch_index": batch_index,
            "candidate_count": len(batch_texts),
            "seconds": batch_seconds,
            "status": "ok",
        })

    metrics["predict_seconds"] = _elapsed_seconds(predict_start)
    metrics["total_seconds"] = _elapsed_seconds(total_start)
    metrics["batch_count"] = len(batch_timings)
    metrics["scored_count"] = len(scores)
    metrics["batch_timings"] = batch_timings
    metrics["batch_seconds"] = _summarize_seconds([
        float(item["seconds"])
        for item in batch_timings
        if item.get("status") == "ok"
    ])

    if metrics["failed_batch_count"]:
        return [], metrics

    if len(scores) != len(texts):
        metrics["status"] = "score_count_mismatch"
        metrics["fallback_used"] = True
        metrics["failure_reason"] = f"expected {len(texts)} scores, got {len(scores)}"
        return [], metrics

    metrics["status"] = "scored"
    return scores, metrics


def predict_relevance_scores(query: str, texts: list[str]) -> list[float]:
    scores, _ = predict_relevance_scores_with_metrics(query, texts)
    return scores


def rerank_documents_with_metrics(query: str, documents: list[Document]) -> tuple[list[Document], dict[str, object]]:
    total_start = time.perf_counter()
    if not documents:
        metrics = _build_base_metrics(0, _coerce_batch_size(None))
        metrics["status"] = "no_candidates"
        metrics["total_seconds"] = _elapsed_seconds(total_start)
        return [], metrics

    enriched_documents = _attach_retrieval_metadata(documents)
    scores, metrics = predict_relevance_scores_with_metrics(query, [doc.page_content for doc in enriched_documents])
    if not scores:
        for doc in enriched_documents:
            doc.metadata = {
                **doc.metadata,
                "reranker_score": round(_fallback_relevance_score(doc), 6),
                "reranker_fallback": True,
                "reranker_fallback_reason": metrics.get("failure_reason") or metrics.get("status"),
            }
        metrics["fallback_used"] = True
        metrics["fallback_count"] = len(enriched_documents)
        metrics["total_seconds"] = _elapsed_seconds(total_start)
        return enriched_documents, metrics

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
        print(
            f"[reranker] top scores: {preview}; "
            f"candidates={metrics.get('candidate_count')}, batch_size={metrics.get('batch_size')}, "
            f"batches={metrics.get('batch_count')}, predict_seconds={metrics.get('predict_seconds')}"
        )

    metrics["fallback_count"] = 0
    metrics["total_seconds"] = _elapsed_seconds(total_start)
    return reranked_documents, metrics


def rerank_documents(query: str, documents: list[Document]) -> list[Document]:
    reranked_documents, _ = rerank_documents_with_metrics(query, documents)
    return reranked_documents