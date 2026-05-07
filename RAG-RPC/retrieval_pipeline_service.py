from __future__ import annotations

import hashlib
import time
from typing import Any

from langchain_core.documents import Document

from access_control import build_source_file_filter, dedupe_values, normalize_username
from context_compression_service import compress_context
from documents_service import build_chunk_id
from es_service import search_bm25_documents
from faq_support import rank_results_for_generation
from hybrid_retrieval_service import reciprocal_rank_fusion
from knowledge_base_service import get_child_es_index_name, get_parent_es_index_name, normalize_knowledge_base_name
from rag_config import BM25_RECALL_K, CONTEXT_COMPRESSION_ENABLED, CONTEXT_COMPRESSION_MIN_CHARS, CONTEXT_COMPRESSION_SENTENCE_K, ENABLE_PARENT_CHILD_RETRIEVAL, FINAL_CONTEXT_K, FUSION_TOP_K, PARENT_RECALL_K, PRINT_LOGGING_ENABLED, RERANKER_ENABLED, RERANK_CANDIDATE_K, RRF_K, VECTOR_RECALL_K
from rag_store import get_parent_vectorstore, get_vectorstore
from retrieval_strategy_config import get_retrieval_strategy_plan, normalize_query_type
from reranker_service import rerank_documents_with_metrics
from retrieval_response_formatter import build_retrieval_response


GET_RECORDS_PAGE_SIZE = 1000


def _log_print(*args, **kwargs):
    if PRINT_LOGGING_ENABLED:
        print(*args, **kwargs)


def _elapsed_seconds(start_time: float) -> float:
    return round(time.perf_counter() - start_time, 6)


def _normalize_top_k(top_k: int | None) -> int:
    if top_k is None:
        return FINAL_CONTEXT_K

    try:
        normalized = int(top_k)
    except (TypeError, ValueError):
        return FINAL_CONTEXT_K

    return max(1, normalized)


def _resolve_final_context_k(top_k: int | None, retrieval_plan: dict[str, Any]) -> int:
    if top_k is not None:
        return _normalize_top_k(top_k)

    try:
        planned_final_context_k = int(retrieval_plan.get("final_context_k", FINAL_CONTEXT_K))
    except (TypeError, ValueError):
        planned_final_context_k = FINAL_CONTEXT_K
    return max(1, planned_final_context_k)


def _coerce_metadata_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return []


def _extract_routed_domains(documents: list[Document], fallback_domains: list[str]) -> list[str]:
    routed_domains: list[str] = []
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        if domain:
            routed_domains.append(domain)
        routed_domains.extend(_coerce_metadata_list(doc.metadata.get("related_domains")))

    deduped = dedupe_values(routed_domains)
    if deduped:
        return deduped
    return dedupe_values(fallback_domains)


def _merge_domain_lists(*domain_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for domain_list in domain_lists:
        merged.extend(domain_list)
    return dedupe_values(merged)


def _merge_source_file_lists(*source_file_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for source_file_list in source_file_lists:
        merged.extend(source_file_list)
    return dedupe_values(merged)


def _extract_primary_domains(documents: list[Document], fallback_domains: list[str]) -> list[str]:
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        if domain:
            return [domain]

    return dedupe_values(fallback_domains)


def _should_expand_related_domains(documents: list[Document]) -> bool:
    if not documents:
        return False
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        related_domains = _coerce_metadata_list(doc.metadata.get("related_domains"))
        if domain == "global" and len(related_domains) > 1:
            return True
    return False


def _build_trace(
    query: str,
    username: str,
    requested_domains: list[str],
    requested_source_files: list[str],
    authorized_domains: list[str],
    authorized_source_files: list[str],
    restricted_source_files: list[str],
    filter_value: dict | None,
) -> dict[str, Any]:
    return {
        "query": query,
        "username": username,
        "requested_domains": requested_domains,
        "requested_source_files": requested_source_files,
        "authorized_domains": authorized_domains,
        "authorized_source_files": authorized_source_files,
        "restricted_source_files": restricted_source_files,
        "filter_value": filter_value,
        "expanded_routed_domains": [],
        "parent_results": [],
        "parent_vector_results": [],
        "parent_bm25_results": [],
        "parent_target_source_files": [],
        "vector_results": [],
        "bm25_results": [],
        "fused_results": [],
        "reranked_results": [],
        "selected_results": [],
        "final_results": [],
        "metrics": {
            "parent_hit_count": 0,
            "parent_target_file_count": 0,
            "vector_hit_count": 0,
            "bm25_hit_count": 0,
            "fused_hit_count": 0,
            "reranked_hit_count": 0,
            "selected_hit_count": 0,
            "final_hit_count": 0,
            "shared_fused_hit_count": 0,
            "parent_vector_score_metrics": {},
            "vector_score_metrics": {},
            "reranker": {},
            "timings": {},
            "two_stage_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
            "two_stage_applied": False,
            "two_stage_fallback_reason": "disabled",
            "compression": {
                "enabled": CONTEXT_COMPRESSION_ENABLED,
                "min_char_count": CONTEXT_COMPRESSION_MIN_CHARS,
                "sentence_limit": CONTEXT_COMPRESSION_SENTENCE_K,
                "compressed_doc_count": 0,
                "high_relevance_preserved_doc_count": 0,
                "high_relevance_preserved_docs": [],
                "original_char_count": 0,
                "compressed_char_count": 0,
                "saved_char_count": 0,
                "saved_ratio": 0.0,
            },
        },
    }


def _compute_compression_metrics(selected_results: list[Document], final_results: list[Document]) -> dict[str, Any]:
    original_char_count = sum(len(doc.page_content) for doc in selected_results)
    compressed_char_count = sum(len(doc.page_content) for doc in final_results)
    saved_char_count = max(original_char_count - compressed_char_count, 0)
    compressed_doc_count = sum(1 for doc in final_results if doc.metadata.get("context_compressed") is True)
    high_relevance_preserved_docs = [
        {
            "final_rank": index + 1,
            "source_file": str(doc.metadata.get("source_file", "")).strip(),
            "chunk_index": doc.metadata.get("chunk_index"),
            "reranker_score": doc.metadata.get("reranker_score"),
            "headers": " > ".join(
                str(value)
                for _, value in sorted(
                    (
                        (key, value)
                        for key, value in doc.metadata.items()
                        if key.startswith("Header") and isinstance(value, str) and value.strip()
                    ),
                    key=lambda item: item[0],
                )
            ),
            "preserved_original": True,
            "reason": "high_relevance",
        }
        for index, doc in enumerate(final_results)
        if doc.metadata.get("context_compression_skip_reason") == "high_relevance"
    ]
    saved_ratio = round(saved_char_count / original_char_count, 4) if original_char_count else 0.0

    return {
        "enabled": CONTEXT_COMPRESSION_ENABLED,
        "min_char_count": CONTEXT_COMPRESSION_MIN_CHARS,
        "sentence_limit": CONTEXT_COMPRESSION_SENTENCE_K,
        "compressed_doc_count": compressed_doc_count,
        "high_relevance_preserved_doc_count": len(high_relevance_preserved_docs),
        "high_relevance_preserved_docs": high_relevance_preserved_docs,
        "original_char_count": original_char_count,
        "compressed_char_count": compressed_char_count,
        "saved_char_count": saved_char_count,
        "saved_ratio": saved_ratio,
    }


def _coerce_metric_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarize_numeric_values(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "min": None,
            "max": None,
            "avg": None,
        }

    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "avg": round(sum(values) / len(values), 6),
    }


def _compute_vector_score_metrics(documents: list[Document]) -> dict[str, Any]:
    scores: list[float] = []
    distances: list[float] = []

    for doc in documents:
        score = _coerce_metric_float(doc.metadata.get("vector_score"))
        distance = _coerce_metric_float(doc.metadata.get("vector_distance"))
        if score is not None:
            scores.append(score)
        if distance is not None:
            distances.append(distance)

    score_summary = _summarize_numeric_values(scores)
    distance_summary = _summarize_numeric_values(distances)
    return {
        "hit_count": len(documents),
        "scored_hit_count": len(scores),
        "distance_hit_count": len(distances),
        "score_min": score_summary["min"],
        "score_max": score_summary["max"],
        "score_avg": score_summary["avg"],
        "distance_min": distance_summary["min"],
        "distance_max": distance_summary["max"],
        "distance_avg": distance_summary["avg"],
    }


def _format_vector_score_metrics(metrics: dict[str, Any]) -> str:
    scored_hit_count = int(metrics.get("scored_hit_count") or 0)
    if scored_hit_count <= 0:
        return "unavailable"

    return (
        f"scored={scored_hit_count}, "
        f"score_avg={metrics.get('score_avg')}, score_max={metrics.get('score_max')}, "
        f"distance_min={metrics.get('distance_min')}, distance_avg={metrics.get('distance_avg')}"
    )


def _extract_source_files(documents: list[Document]) -> list[str]:
    source_files: list[str] = []
    for doc in documents:
        source_file = str(doc.metadata.get("source_file", "")).strip()
        if source_file:
            source_files.append(source_file)
    return dedupe_values(source_files)


def _normalize_document_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _coerce_optional_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _coerce_metric_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _document_title_fallback_parts(metadata: dict[str, Any]) -> list[str]:
    fallback_parts: list[str] = []
    for field_name in ("parent_title", "title", "Header 1", "Header 2", "Header 3"):
        cleaned_value = _coerce_optional_str(metadata.get(field_name))
        if cleaned_value and cleaned_value not in fallback_parts:
            fallback_parts.append(cleaned_value)
    return fallback_parts


def _document_dedupe_key(doc: Document) -> str:
    if not isinstance(doc.metadata, dict):
        doc.metadata = dict(doc.metadata or {})

    metadata = doc.metadata
    existing_key = _coerce_optional_str(metadata.get("retrieval_dedupe_key"))
    if existing_key:
        return existing_key

    source_file = _coerce_optional_str(metadata.get("source_file"))
    normalized_content = _normalize_document_text(doc.page_content)
    if source_file and normalized_content:
        content_digest = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
        key = f"source_content:{source_file}|{content_digest}"
    else:
        chunk_id = _coerce_optional_str(metadata.get("chunk_id"))
        if chunk_id:
            key = f"chunk_id:{chunk_id}"
        else:
            chunk_index = metadata.get("chunk_index")
            if source_file and chunk_index not in (None, ""):
                try:
                    key = f"chunk_index:{build_chunk_id(metadata, int(chunk_index), doc.page_content)}"
                except (TypeError, ValueError):
                    key = f"chunk_index:{source_file}|{chunk_index}"
            else:
                title_fallback_parts = _document_title_fallback_parts(metadata)
                if source_file and title_fallback_parts:
                    key = f"title:{source_file}|{'|'.join(title_fallback_parts)}"
                elif normalized_content:
                    content_digest = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
                    key = f"content:{content_digest}"
                elif source_file:
                    key = f"source:{source_file}"
                else:
                    fallback_digest = hashlib.sha256(str(metadata).encode("utf-8")).hexdigest()
                    key = f"fallback:{fallback_digest}"

    metadata["retrieval_dedupe_key"] = key
    return key


def _merge_fused_duplicate(existing_doc: Document, duplicate_doc: Document) -> None:
    merged_sources: list[str] = []
    for source in list(existing_doc.metadata.get("retrieval_sources", [])) + list(duplicate_doc.metadata.get("retrieval_sources", [])):
        cleaned_source = _coerce_optional_str(source)
        if cleaned_source and cleaned_source not in merged_sources:
            merged_sources.append(cleaned_source)
    if merged_sources:
        existing_doc.metadata["retrieval_sources"] = merged_sources

    existing_rrf_score = _coerce_metric_float(existing_doc.metadata.get("rrf_score")) or 0.0
    duplicate_rrf_score = _coerce_metric_float(duplicate_doc.metadata.get("rrf_score")) or 0.0
    existing_doc.metadata["rrf_score"] = round(existing_rrf_score + duplicate_rrf_score, 6)

    for rank_field in ("vector_rank", "bm25_rank"):
        available_ranks = [
            rank
            for rank in (
                _coerce_metric_int(existing_doc.metadata.get(rank_field)),
                _coerce_metric_int(duplicate_doc.metadata.get(rank_field)),
            )
            if rank is not None
        ]
        if available_ranks:
            existing_doc.metadata[rank_field] = min(available_ranks)

    for weight_field in ("vector_weight", "bm25_weight"):
        if existing_doc.metadata.get(weight_field) in (None, "") and duplicate_doc.metadata.get(weight_field) not in (None, ""):
            existing_doc.metadata[weight_field] = duplicate_doc.metadata.get(weight_field)


def _sort_fused_documents(documents: list[Document]) -> list[Document]:
    return sorted(
        documents,
        key=lambda doc: (
            float(doc.metadata.get("rrf_score", 0.0)),
            -int(doc.metadata.get("vector_rank", 10**6)),
            -int(doc.metadata.get("bm25_rank", 10**6)),
        ),
        reverse=True,
    )


def _dedupe_documents(documents: list[Document], *, stage_name: str) -> tuple[list[Document], int]:
    deduped_documents: list[Document] = []
    documents_by_key: dict[str, Document] = {}
    duplicate_count = 0

    for doc in documents:
        dedupe_key = _document_dedupe_key(doc)
        existing_doc = documents_by_key.get(dedupe_key)
        if existing_doc is None:
            documents_by_key[dedupe_key] = doc
            deduped_documents.append(doc)
            continue

        duplicate_count += 1
        if stage_name == "fused":
            _merge_fused_duplicate(existing_doc, doc)

    if stage_name == "fused":
        deduped_documents = _sort_fused_documents(deduped_documents)

    return deduped_documents, duplicate_count


def _dedupe_documents_by_source_file(documents: list[Document]) -> list[Document]:
    deduped_documents: list[Document] = []
    seen_source_files: set[str] = set()

    for doc in documents:
        source_file = str(doc.metadata.get("source_file", "")).strip()
        if not source_file:
            deduped_documents.append(doc)
            continue

        if source_file in seen_source_files:
            continue

        seen_source_files.add(source_file)
        deduped_documents.append(doc)

    return deduped_documents


def _is_sql_query_candidate(query: str) -> bool:
    normalized_query = query.lower()
    strong_markers = (
        "sql",
        "select",
        "from",
        "where",
        "group by",
        "order by",
    )
    if any(marker in normalized_query for marker in strong_markers):
        return True

    weak_markers = (
        "数据库",
        "表",
        "字段",
        "列",
        "主键",
        "索引",
        "枚举",
        "状态字典",
        "接口",
        "参数",
        "请求参数",
        "返回字段",
        "格式",
        "明文",
    )
    matched_weak = sum(1 for marker in weak_markers if marker in normalized_query)
    return matched_weak >= 3


def _resolve_sql_source_files(source_files: list[str], domains: list[str], query: str, vectorstore) -> list[str]:
    if not source_files and not domains:
        return []

    source_file_set = set(dedupe_values(source_files)) if source_files else None
    domain_set = set(dedupe_values(domains)) if domains else None

    structured_source_files: list[str] = []
    try:
        offset = 0
        while True:
            records = vectorstore.get_records(include=["metadatas"], limit=GET_RECORDS_PAGE_SIZE, offset=offset)
            for metadata in records.get("metadatas", []):
                if not isinstance(metadata, dict):
                    continue
                
                doc_type = str(metadata.get("type", "")).strip().lower()
                if doc_type != "reference":
                    continue

                source_file = str(metadata.get("source_file", "")).strip()
                domain = str(metadata.get("domain", "")).strip()
                if not source_file:
                    continue
                if source_file_set is not None and source_file not in source_file_set:
                    continue
                if domain_set is not None and domain not in domain_set:
                    continue
                
                structured_source_files.append(source_file)

            next_offset = int(records.get("next_offset", offset + len(records.get("ids", []))))
            if not records.get("has_more") or next_offset <= offset:
                break
            offset = next_offset
    except Exception:
        return dedupe_values(source_files)

    if structured_source_files:
        return dedupe_values(structured_source_files)

    return dedupe_values(source_files)


def _collection_count(resolved_vectorstore) -> int:
    try:
        return int(resolved_vectorstore.count())
    except Exception:
        return 0


def _run_child_recall(
    query: str,
    retrieval_plan: dict[str, Any],
    source_files: list[str] | None,
    domains: list[str] | None,
    filter_value: dict | None,
    vectorstore,
    es_index_name: str,
) -> tuple[list[Document], list[Document], list[Document], dict[str, float]]:
    recall_start = time.perf_counter()
    vector_start = time.perf_counter()
    if filter_value is not None:
        vector_results = vectorstore.similarity_search_with_score(query, k=int(retrieval_plan["vector_k"]), filter=filter_value)
    else:
        vector_results = vectorstore.similarity_search_with_score(query, k=int(retrieval_plan["vector_k"]))
    vector_search_seconds = _elapsed_seconds(vector_start)
    raw_vector_hit_count = len(vector_results)
    vector_results, vector_duplicate_count = _dedupe_documents(vector_results, stage_name="vector")

    bm25_start = time.perf_counter()
    bm25_results = search_bm25_documents(
        query=query,
        source_files=source_files or None,
        domains=domains or None,
        index_name=es_index_name,
        limit=int(retrieval_plan["bm25_k"]),
    )
    bm25_search_seconds = _elapsed_seconds(bm25_start)
    raw_bm25_hit_count = len(bm25_results)
    bm25_results, bm25_duplicate_count = _dedupe_documents(bm25_results, stage_name="bm25")

    fusion_start = time.perf_counter()
    fused_results = reciprocal_rank_fusion(
        vector_results,
        bm25_results,
        k=RRF_K,
        vector_weight=float(retrieval_plan["vector_weight"]),
        bm25_weight=float(retrieval_plan["bm25_weight"]),
    )
    raw_fused_hit_count = len(fused_results)
    fused_results, fused_duplicate_count = _dedupe_documents(fused_results, stage_name="fused")
    fused_results = fused_results[: int(retrieval_plan.get("rerank_candidate_k", retrieval_plan["fusion_top_k"]))]
    fusion_seconds = _elapsed_seconds(fusion_start)

    if vector_duplicate_count or bm25_duplicate_count or fused_duplicate_count:
        _log_print(
            "INFO: 子召回统一去重，"
            f"vector_hits={raw_vector_hit_count}->{len(vector_results)}, "
            f"bm25_hits={raw_bm25_hit_count}->{len(bm25_results)}, "
            f"fused_hits={raw_fused_hit_count}->{len(fused_results)}"
        )

    timings = {
        "child_vector_search_seconds": vector_search_seconds,
        "child_bm25_search_seconds": bm25_search_seconds,
        "child_fusion_seconds": fusion_seconds,
        "child_recall_seconds": _elapsed_seconds(recall_start),
    }

    return vector_results, bm25_results, fused_results, timings


def _build_observability_payload(
    query_type: str,
    retrieval_plan: dict[str, Any],
    final_context_k: int,
    requested_domains: list[str],
    target_domains: list[str],
    routed_domains: list[str],
    expanded_routed_domains: list[str],
    target_source_file_count: int,
    two_stage_applied: bool,
    two_stage_fallback_reason: str,
    requested_execution_mode: str,
    resolved_execution_mode: str,
    parent_vector_score_metrics: dict[str, Any],
    vector_score_metrics: dict[str, Any],
    reranker_metrics: dict[str, Any],
    compression_metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "query_type": query_type,
        "strategy": str(retrieval_plan.get("strategy", "unknown")),
        "requested_execution_mode": requested_execution_mode,
        "execution_mode": resolved_execution_mode,
        "mode_fallback_applied": requested_execution_mode != resolved_execution_mode,
        "weights": {
            "vector": float(retrieval_plan.get("vector_weight", 1.0)),
            "bm25": float(retrieval_plan.get("bm25_weight", 1.0)),
        },
        "recall": {
            "vector_k": int(retrieval_plan.get("vector_k", VECTOR_RECALL_K)),
            "bm25_k": int(retrieval_plan.get("bm25_k", BM25_RECALL_K)),
            "fusion_top_k": int(retrieval_plan.get("fusion_top_k", FUSION_TOP_K)),
            "rerank_candidate_k": int(retrieval_plan.get("rerank_candidate_k", RERANK_CANDIDATE_K)),
            "final_context_k": final_context_k,
        },
        "reranker": reranker_metrics,
        "requested_domains": requested_domains,
        "target_domains": target_domains,
        "routed_domains": routed_domains,
        "expanded_routed_domains": expanded_routed_domains,
        "target_source_file_count": target_source_file_count,
        "two_stage_applied": two_stage_applied,
        "two_stage_fallback_reason": two_stage_fallback_reason,
        "score_metrics": {
            "parent_vector": parent_vector_score_metrics,
            "vector": vector_score_metrics,
        },
        "compression": compression_metrics,
    }


def run_retrieval_pipeline(
    query: str,
    username: str,
    domains: list[str] | None = None,
    source_files: list[str] | None = None,
    query_type: str | None = None,
    top_k: int | None = None,
    knowledge_base: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    pipeline_start = time.perf_counter()
    normalized_query = query.strip()
    normalized_username = normalize_username(username)
    normalized_query_type = normalize_query_type(query_type)
    normalized_knowledge_base = normalize_knowledge_base_name(knowledge_base)
    normalized_scope_domains = dedupe_values(domains or [])
    normalized_scope_source_files = dedupe_values(source_files or [])
    vectorstore = get_vectorstore(normalized_knowledge_base)
    parent_vectorstore = get_parent_vectorstore(normalized_knowledge_base)
    es_index_name = get_child_es_index_name(normalized_knowledge_base)
    es_parent_index_name = get_parent_es_index_name(normalized_knowledge_base)
    retrieval_plan = dict(get_retrieval_strategy_plan(normalized_query_type))
    requested_top_k = _normalize_top_k(top_k) if top_k is not None else None
    effective_final_context_k = _resolve_final_context_k(top_k, retrieval_plan)
    retrieval_plan["final_context_k"] = effective_final_context_k
    retrieval_plan["fusion_top_k"] = max(int(retrieval_plan.get("fusion_top_k", FUSION_TOP_K)), effective_final_context_k)
    retrieval_plan["rerank_candidate_k"] = max(
        int(retrieval_plan.get("rerank_candidate_k", RERANK_CANDIDATE_K)),
        int(retrieval_plan["fusion_top_k"]),
        effective_final_context_k,
    )
    requested_strategy = str(retrieval_plan.get("strategy", "unknown"))
    requested_execution_mode = str(retrieval_plan.get("execution_mode", "hybrid")).strip() or "hybrid"
    supported_execution_modes = {"hybrid", "sql_query"}
    resolved_execution_mode = requested_execution_mode if requested_execution_mode in supported_execution_modes else "hybrid"
    filter_value = build_source_file_filter(normalized_scope_source_files) if normalized_scope_source_files else None
    
    if resolved_execution_mode == "hybrid" and _is_sql_query_candidate(normalized_query):
        resolved_execution_mode = "sql_query"
    trace = _build_trace(
        query=normalized_query,
        username=normalized_username,
        requested_domains=normalized_scope_domains,
        requested_source_files=normalized_scope_source_files,
        authorized_domains=normalized_scope_domains,
        authorized_source_files=normalized_scope_source_files,
        restricted_source_files=[],
        filter_value=filter_value,
    )
    trace["query_type"] = normalized_query_type
    trace["knowledge_base"] = normalized_knowledge_base
    trace["retrieval_plan"] = retrieval_plan
    trace["requested_top_k"] = requested_top_k
    trace["effective_final_context_k"] = effective_final_context_k
    trace["requested_execution_mode"] = requested_execution_mode
    trace["resolved_execution_mode"] = resolved_execution_mode

    if normalized_scope_source_files or normalized_scope_domains:
        candidate_scope_count = len(normalized_scope_source_files) if normalized_scope_source_files else len(normalized_scope_domains)
        _log_print(f"🌐 全局检索启用，当前候选文档数={candidate_scope_count}，user={normalized_username}")
    else:
        _log_print(f"🌐 检索未提供范围，按全量知识库检索处理，user={normalized_username}")

    parent_results: list[Document] = []
    parent_vector_results: list[Document] = []
    parent_bm25_results: list[Document] = []
    parent_stage_timings = {
        "parent_collection_count_seconds": 0.0,
        "parent_vector_search_seconds": 0.0,
        "parent_bm25_search_seconds": 0.0,
        "parent_dedupe_seconds": 0.0,
        "parent_fusion_seconds": 0.0,
        "parent_stage_seconds": 0.0,
    }
    child_recall_timings = {
        "child_vector_search_seconds": 0.0,
        "child_bm25_search_seconds": 0.0,
        "child_fusion_seconds": 0.0,
        "child_recall_seconds": 0.0,
    }
    narrowed_source_files = list(normalized_scope_source_files)
    narrowed_domains = list(normalized_scope_domains)
    effective_authorized_source_files = list(normalized_scope_source_files)
    effective_authorized_domains = list(normalized_scope_domains)
    routed_domains = list(normalized_scope_domains)
    expanded_routed_domains = list(normalized_scope_domains)
    two_stage_applied = False
    two_stage_fallback_reason = "disabled"

    if ENABLE_PARENT_CHILD_RETRIEVAL:
        parent_stage_start = time.perf_counter()
        parent_scope_source_files = normalized_scope_source_files
        parent_filter_value = build_source_file_filter(parent_scope_source_files) if parent_scope_source_files else None
        parent_count_start = time.perf_counter()
        parent_collection_count = _collection_count(parent_vectorstore)
        parent_stage_timings["parent_collection_count_seconds"] = _elapsed_seconds(parent_count_start)
        _log_print(
            f"INFO: 阶段一父文档双路召回，parent_k={PARENT_RECALL_K}, "
            f"authorized_docs={len(normalized_scope_source_files)}, authorized_domains={normalized_scope_domains}, filter={parent_filter_value}"
        )
        if parent_collection_count > 0:
            parent_vector_start = time.perf_counter()
            if parent_filter_value is not None:
                parent_vector_results = parent_vectorstore.similarity_search_with_score(normalized_query, k=PARENT_RECALL_K, filter=parent_filter_value)
            else:
                parent_vector_results = parent_vectorstore.similarity_search_with_score(normalized_query, k=PARENT_RECALL_K)
            parent_stage_timings["parent_vector_search_seconds"] = _elapsed_seconds(parent_vector_start)
        else:
            _log_print("INFO: 父向量索引为空，阶段一跳过向量召回")

        parent_bm25_start = time.perf_counter()
        parent_bm25_results = search_bm25_documents(
            query=normalized_query,
            source_files=normalized_scope_source_files or None,
            domains=normalized_scope_domains or None,
            index_name=es_parent_index_name,
            limit=PARENT_RECALL_K,
        )
        parent_stage_timings["parent_bm25_search_seconds"] = _elapsed_seconds(parent_bm25_start)

        raw_parent_vector_hit_count = len(parent_vector_results)
        raw_parent_bm25_hit_count = len(parent_bm25_results)
        parent_dedupe_start = time.perf_counter()
        parent_vector_results = _dedupe_documents_by_source_file(parent_vector_results)
        parent_bm25_results = _dedupe_documents_by_source_file(parent_bm25_results)
        parent_stage_timings["parent_dedupe_seconds"] = _elapsed_seconds(parent_dedupe_start)
        if len(parent_vector_results) != raw_parent_vector_hit_count or len(parent_bm25_results) != raw_parent_bm25_hit_count:
            _log_print(
                f"INFO: 阶段一按 source_file 去重，"
                f"parent_vector_hits={raw_parent_vector_hit_count}->{len(parent_vector_results)}, "
                f"parent_bm25_hits={raw_parent_bm25_hit_count}->{len(parent_bm25_results)}"
            )

        parent_fusion_start = time.perf_counter()
        parent_results = reciprocal_rank_fusion(parent_vector_results, parent_bm25_results, k=RRF_K)[:PARENT_RECALL_K]
        parent_stage_timings["parent_fusion_seconds"] = _elapsed_seconds(parent_fusion_start)

        if parent_results:
            parent_target_source_files = _extract_source_files(parent_results)
            if parent_target_source_files:
                merged_requested_source_files = _merge_source_file_lists(normalized_scope_source_files, parent_target_source_files)
                primary_domains = _extract_primary_domains(parent_results, normalized_scope_domains)
                expanded_routed_domains = _extract_routed_domains(parent_results, normalized_scope_domains)
                if not expanded_routed_domains:
                    expanded_routed_domains = list(primary_domains)
                routed_domains = list(primary_domains)
                narrowed_domains = list(primary_domains)
                if _should_expand_related_domains(parent_results):
                    narrowed_domains = list(expanded_routed_domains)
                narrowed_domains = _merge_domain_lists(normalized_scope_domains, narrowed_domains)
                narrowed_source_files = merged_requested_source_files or parent_target_source_files or normalized_scope_source_files
                two_stage_applied = True
                two_stage_fallback_reason = "applied"
                _log_print(
                    f"INFO: 阶段一完成，parent_vector_hits={len(parent_vector_results)}, parent_bm25_hits={len(parent_bm25_results)}, parent_hits={len(parent_results)}, "
                    f"routed_domains={routed_domains}, expanded_routed_domains={expanded_routed_domains}, narrowed_domains={narrowed_domains}, target_files={narrowed_source_files}"
                )
            else:
                two_stage_fallback_reason = "no-parent-hits"
                _log_print("INFO: 阶段一未命中父文档，回退到授权范围内直接检索")
        else:
            two_stage_fallback_reason = "no-parent-hits"
            _log_print("INFO: 阶段一双路召回未命中父文档，回退到当前单层检索")
        parent_stage_timings["parent_stage_seconds"] = _elapsed_seconds(parent_stage_start)

    child_filter_value = build_source_file_filter(narrowed_source_files) if narrowed_source_files else filter_value

    if resolved_execution_mode == "sql_query":
        sql_source_files = _resolve_sql_source_files(
            narrowed_source_files or normalized_scope_source_files,
            narrowed_domains or effective_authorized_domains or normalized_scope_domains,
            normalized_query,
            vectorstore,
        )
        sql_filter_value = build_source_file_filter(sql_source_files) if sql_source_files else child_filter_value
        sql_target_source_files = sql_source_files or narrowed_source_files
        _log_print(
            f"INFO: 执行结构化查询，parent_enabled={ENABLE_PARENT_CHILD_RETRIEVAL}, parent_k={PARENT_RECALL_K}, "
            f"two_stage_applied={two_stage_applied}, query_type={normalized_query_type}, strategy={retrieval_plan['strategy']}, "
            f"requested_execution_mode={requested_execution_mode}, execution_mode={resolved_execution_mode}, "
            f"vector_k={retrieval_plan['vector_k']}, bm25_k={retrieval_plan['bm25_k']}, "
            f"vector_weight={retrieval_plan['vector_weight']}, bm25_weight={retrieval_plan['bm25_weight']}, "
            f"fusion_top_k={retrieval_plan['fusion_top_k']}, final_top_k={effective_final_context_k}, "
            f"rerank_candidate_k={retrieval_plan['rerank_candidate_k']}, "
            f"target_files={len(sql_target_source_files)}, target_domains={narrowed_domains}, filter={sql_filter_value}"
        )

        vector_results, bm25_results, fused_results, child_recall_timings = _run_child_recall(
            query=normalized_query,
            retrieval_plan=retrieval_plan,
            source_files=sql_target_source_files or narrowed_source_files or normalized_scope_source_files,
            domains=narrowed_domains or effective_authorized_domains or normalized_scope_domains,
            filter_value=sql_filter_value,
            vectorstore=vectorstore,
            es_index_name=es_index_name,
        )

        _log_print(f"INFO: 结构化查询召回完成，vector_hits={len(vector_results)}, bm25_hits={len(bm25_results)}")
        _log_print(f"INFO: 结构化查询 RRF 融合完成，fused_hits={len(fused_results)}")
    else:
        _log_print(
            f"INFO: 执行混合召回，parent_enabled={ENABLE_PARENT_CHILD_RETRIEVAL}, parent_k={PARENT_RECALL_K}, "
            f"two_stage_applied={two_stage_applied}, query_type={normalized_query_type}, strategy={retrieval_plan['strategy']}, "
            f"requested_execution_mode={requested_execution_mode}, execution_mode={resolved_execution_mode}, "
            f"vector_k={retrieval_plan['vector_k']}, bm25_k={retrieval_plan['bm25_k']}, "
            f"vector_weight={retrieval_plan['vector_weight']}, bm25_weight={retrieval_plan['bm25_weight']}, "
            f"fusion_top_k={retrieval_plan['fusion_top_k']}, final_top_k={effective_final_context_k}, "
            f"rerank_candidate_k={retrieval_plan['rerank_candidate_k']}, "
            f"reranker_enabled={RERANKER_ENABLED}, target_files={len(narrowed_source_files)}, target_domains={narrowed_domains}, filter={child_filter_value}"
        )

        vector_results, bm25_results, fused_results, child_recall_timings = _run_child_recall(
            query=normalized_query,
            retrieval_plan=retrieval_plan,
            source_files=narrowed_source_files or normalized_scope_source_files,
            domains=narrowed_domains or effective_authorized_domains or normalized_scope_domains,
            filter_value=child_filter_value,
            vectorstore=vectorstore,
            es_index_name=es_index_name,
        )

        _log_print(f"INFO: 混合召回完成，vector_hits={len(vector_results)}, bm25_hits={len(bm25_results)}")
        _log_print(f"INFO: RRF 融合完成，fused_hits={len(fused_results)}")

    parent_vector_score_metrics = _compute_vector_score_metrics(parent_vector_results)
    vector_score_metrics = _compute_vector_score_metrics(vector_results)
    _log_print(
        "INFO: 向量召回分数，"
        f"parent={_format_vector_score_metrics(parent_vector_score_metrics)}, "
        f"child={_format_vector_score_metrics(vector_score_metrics)}"
    )

    rerank_start = time.perf_counter()
    reranked_by_model, reranker_metrics = rerank_documents_with_metrics(normalized_query, fused_results)
    generation_rank_start = time.perf_counter()
    reranked_results = rank_results_for_generation(normalized_query, reranked_by_model)
    generation_rank_seconds = _elapsed_seconds(generation_rank_start)
    rerank_seconds = _elapsed_seconds(rerank_start)
    _log_print(
        "INFO: Reranker 完成，"
        f"status={reranker_metrics.get('status')}, candidates={reranker_metrics.get('candidate_count')}, "
        f"scored={reranker_metrics.get('scored_count')}, batch_size={reranker_metrics.get('batch_size')}, "
        f"batches={reranker_metrics.get('batch_count')}, predict_seconds={reranker_metrics.get('predict_seconds')}, "
        f"fallback={reranker_metrics.get('fallback_used')}, failure={reranker_metrics.get('failure_reason')}"
    )
    raw_reranked_hit_count = len(reranked_results)
    reranked_results, reranked_duplicate_count = _dedupe_documents(reranked_results, stage_name="reranked")
    if reranked_duplicate_count:
        _log_print(
            f"INFO: 重排结果统一去重，reranked_hits={raw_reranked_hit_count}->{len(reranked_results)}"
        )
    selected_results = reranked_results[:effective_final_context_k]
    final_results = selected_results

    compression_start = time.perf_counter()
    if CONTEXT_COMPRESSION_ENABLED and selected_results:
        final_results = compress_context(
            query=normalized_query,
            documents=selected_results,
            sentence_limit=CONTEXT_COMPRESSION_SENTENCE_K,
        )
    raw_final_hit_count = len(final_results)
    final_results, final_duplicate_count = _dedupe_documents(final_results, stage_name="final")
    compression_metrics = _compute_compression_metrics(selected_results, final_results)
    if CONTEXT_COMPRESSION_ENABLED and selected_results:
        _log_print(
            "INFO: 上下文压缩完成，"
            f"doc_hits={len(final_results)}, compressed_docs={compression_metrics['compressed_doc_count']}, "
            f"saved_chars={compression_metrics['saved_char_count']}, saved_ratio={compression_metrics['saved_ratio']:.2%}, "
            f"min_chars={CONTEXT_COMPRESSION_MIN_CHARS}, sentence_k={CONTEXT_COMPRESSION_SENTENCE_K}"
        )
    if final_duplicate_count:
        _log_print(
            f"INFO: 最终结果统一去重，final_hits={raw_final_hit_count}->{len(final_results)}"
        )
    compression_seconds = _elapsed_seconds(compression_start)

    timing_metrics = {
        **parent_stage_timings,
        **child_recall_timings,
        "reranker_model_resolve_seconds": float(reranker_metrics.get("model_resolve_seconds") or 0.0),
        "reranker_predict_seconds": float(reranker_metrics.get("predict_seconds") or 0.0),
        "generation_rank_seconds": generation_rank_seconds,
        "rerank_seconds": rerank_seconds,
        "compression_seconds": compression_seconds,
        "total_retrieval_seconds": _elapsed_seconds(pipeline_start),
    }

    trace["parent_results"] = parent_results
    trace["parent_vector_results"] = parent_vector_results
    trace["parent_bm25_results"] = parent_bm25_results
    trace["parent_target_source_files"] = _extract_source_files(parent_results) if two_stage_applied else []
    trace["requested_domains"] = normalized_scope_domains
    trace["authorized_domains"] = effective_authorized_domains
    trace["authorized_source_files"] = effective_authorized_source_files
    trace["routed_domains"] = routed_domains
    trace["expanded_routed_domains"] = expanded_routed_domains
    trace["vector_results"] = vector_results
    trace["bm25_results"] = bm25_results
    trace["fused_results"] = fused_results
    trace["reranked_results"] = reranked_results
    trace["selected_results"] = selected_results
    trace["final_results"] = final_results
    trace["metrics"] = {
        "parent_hit_count": len(parent_results),
        "parent_vector_hit_count": len(parent_vector_results),
        "parent_bm25_hit_count": len(parent_bm25_results),
        "parent_target_file_count": len(narrowed_source_files) if two_stage_applied else 0,
        "vector_hit_count": len(vector_results),
        "bm25_hit_count": len(bm25_results),
        "fused_hit_count": len(fused_results),
        "reranked_hit_count": len(reranked_results),
        "selected_hit_count": len(selected_results),
        "final_hit_count": len(final_results),
        "shared_fused_hit_count": sum(1 for doc in fused_results if len(doc.metadata.get("retrieval_sources", [])) > 1),
        "parent_vector_score_metrics": parent_vector_score_metrics,
        "vector_score_metrics": vector_score_metrics,
        "reranker": reranker_metrics,
        "timings": timing_metrics,
        "query_type": normalized_query_type,
        "strategy": retrieval_plan["strategy"],
        "requested_strategy": requested_strategy,
        "requested_execution_mode": requested_execution_mode,
        "execution_mode": resolved_execution_mode,
        "vector_weight": retrieval_plan["vector_weight"],
        "bm25_weight": retrieval_plan["bm25_weight"],
        "requested_top_k": requested_top_k,
        "final_context_k": effective_final_context_k,
        "rerank_candidate_k": retrieval_plan["rerank_candidate_k"],
        "two_stage_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
        "two_stage_applied": two_stage_applied,
        "two_stage_fallback_reason": two_stage_fallback_reason,
        "compression": compression_metrics,
    }

    diagnostics = _build_observability_payload(
        query_type=normalized_query_type,
        retrieval_plan=retrieval_plan,
        final_context_k=effective_final_context_k,
        requested_domains=normalized_scope_domains,
        target_domains=narrowed_domains or effective_authorized_domains or normalized_scope_domains,
        routed_domains=routed_domains,
        expanded_routed_domains=expanded_routed_domains,
        target_source_file_count=len(narrowed_source_files),
        two_stage_applied=two_stage_applied,
        two_stage_fallback_reason=two_stage_fallback_reason,
        requested_execution_mode=requested_execution_mode,
        resolved_execution_mode=resolved_execution_mode,
        parent_vector_score_metrics=parent_vector_score_metrics,
        vector_score_metrics=vector_score_metrics,
        reranker_metrics=reranker_metrics,
        compression_metrics=compression_metrics,
    )
    if debug:
        diagnostics["requested_strategy"] = requested_strategy
        diagnostics["knowledge_base"] = normalized_knowledge_base
    else:
        diagnostics = None

    context = [
        {
            "metadata": doc.metadata,
            "reranker_score": doc.metadata.get("reranker_score"),
            "page_content": doc.page_content,
        }
        for doc in final_results
    ]

    response = build_retrieval_response(
        context=context,
        routed_domains=routed_domains,
        expanded_routed_domains=expanded_routed_domains,
        routed_source_files=narrowed_source_files,
        authorized_domains=effective_authorized_domains,
        authorized_source_files=effective_authorized_source_files,
        username=normalized_username,
        query_type=normalized_query_type,
        diagnostics=diagnostics,
    )
    return {
        "status": "success",
        "response": response,
        "trace": trace,
    }