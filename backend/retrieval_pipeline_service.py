from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from access_control import build_source_file_filter, dedupe_values, load_document_access_manifest, normalize_username, resolve_accessible_sources
from context_compression_service import compress_context
from es_service import search_bm25_documents
from faq_support import rank_results_for_generation
from hybrid_retrieval_service import reciprocal_rank_fusion
from rag_config import BM25_RECALL_K, CONTEXT_COMPRESSION_ENABLED, CONTEXT_COMPRESSION_MIN_CHARS, CONTEXT_COMPRESSION_SENTENCE_K, DOCS_DIR, ENABLE_ACL, ENABLE_PARENT_CHILD_RETRIEVAL, FINAL_CONTEXT_K, FUSION_TOP_K, PARENT_RECALL_K, RERANKER_ENABLED, RRF_K, ES_INDEX_NAME, ES_PARENT_INDEX_NAME, USERNAME_GROUP_MAPPING_FILE, VECTOR_RECALL_K
from rag_store import parent_vectorstore, vectorstore
from retrieval_strategy_config import get_retrieval_strategy_plan, normalize_query_type
from reranker_service import rerank_documents
from retrieval_response_formatter import build_context_entry, build_retrieval_response


def _extract_denied_domains(source_files: list[str]) -> list[str]:
    return sorted({Path(source_file).parts[0] for source_file in source_files if Path(source_file).parts})


def _coerce_metadata_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return []


def _resolve_domains_for_source_files(source_files: list[str]) -> list[str]:
    if not source_files:
        return []

    source_file_set = set(source_files)
    manifest = load_document_access_manifest(DOCS_DIR)
    resolved_domains = [
        str(entry["domain"]).strip()
        for entry in manifest
        if entry["source_file"] in source_file_set and str(entry["domain"]).strip()
    ]
    return dedupe_values(resolved_domains)


def _extract_routed_domains(documents: list[Document], fallback_source_files: list[str]) -> list[str]:
    routed_domains: list[str] = []
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        if domain:
            routed_domains.append(domain)
        routed_domains.extend(_coerce_metadata_list(doc.metadata.get("related_domains")))

    deduped = dedupe_values(routed_domains)
    if deduped:
        return deduped
    return _resolve_domains_for_source_files(fallback_source_files)


def _merge_domain_lists(*domain_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for domain_list in domain_lists:
        merged.extend(domain_list)
    return dedupe_values(merged)


def _extract_primary_domains(documents: list[Document], fallback_source_files: list[str]) -> list[str]:
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        if domain:
            return [domain]

    return _resolve_domains_for_source_files(fallback_source_files)


def _should_expand_related_domains(documents: list[Document]) -> bool:
    if not documents:
        return False
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        related_domains = _coerce_metadata_list(doc.metadata.get("related_domains"))
        if domain == "global" and len(related_domains) > 1:
            return True
    return False


def _expand_source_files_by_domains(source_files: list[str], routed_domains: list[str]) -> list[str]:
    if not source_files or not routed_domains:
        return []

    source_file_set = set(source_files)
    routed_domain_set = set(routed_domains)
    manifest = load_document_access_manifest(DOCS_DIR)
    expanded_source_files = [
        entry["source_file"]
        for entry in manifest
        if entry["source_file"] in source_file_set and str(entry["domain"]).strip() in routed_domain_set
    ]
    return dedupe_values(expanded_source_files)


def _resolve_source_files_for_scope(source_files: list[str], domains: list[str]) -> list[str]:
    normalized_source_files = dedupe_values(source_files)
    normalized_domains = dedupe_values(domains)
    if not normalized_source_files and not normalized_domains:
        return []

    source_file_set = set(normalized_source_files) if normalized_source_files else None
    domain_set = set(normalized_domains) if normalized_domains else None
    manifest = load_document_access_manifest(DOCS_DIR)
    resolved_source_files = [
        entry["source_file"]
        for entry in manifest
        if (source_file_set is None or entry["source_file"] in source_file_set)
        and (domain_set is None or str(entry["domain"]).strip() in domain_set)
    ]
    return dedupe_values(resolved_source_files)


def _list_all_source_files() -> list[str]:
    manifest = load_document_access_manifest(DOCS_DIR)
    return dedupe_values([str(entry["source_file"]).strip() for entry in manifest if str(entry["source_file"]).strip()])


def _build_trace(
    query: str,
    username: str,
    requested_domains: list[str],
    authorized_domains: list[str],
    authorized_source_files: list[str],
    restricted_source_files: list[str],
    filter_value: dict | None,
) -> dict[str, Any]:
    return {
        "query": query,
        "username": username,
        "requested_domains": requested_domains,
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
            "two_stage_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
            "two_stage_applied": False,
            "two_stage_fallback_reason": "disabled",
            "compression": {
                "enabled": CONTEXT_COMPRESSION_ENABLED,
                "min_char_count": CONTEXT_COMPRESSION_MIN_CHARS,
                "sentence_limit": CONTEXT_COMPRESSION_SENTENCE_K,
                "compressed_doc_count": 0,
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
    saved_ratio = round(saved_char_count / original_char_count, 4) if original_char_count else 0.0

    return {
        "enabled": CONTEXT_COMPRESSION_ENABLED,
        "min_char_count": CONTEXT_COMPRESSION_MIN_CHARS,
        "sentence_limit": CONTEXT_COMPRESSION_SENTENCE_K,
        "compressed_doc_count": compressed_doc_count,
        "original_char_count": original_char_count,
        "compressed_char_count": compressed_char_count,
        "saved_char_count": saved_char_count,
        "saved_ratio": saved_ratio,
    }


def _extract_source_files(documents: list[Document]) -> list[str]:
    source_files: list[str] = []
    for doc in documents:
        source_file = str(doc.metadata.get("source_file", "")).strip()
        if source_file:
            source_files.append(source_file)
    return dedupe_values(source_files)


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


def _resolve_sql_source_files(source_files: list[str], domains: list[str], query: str) -> list[str]:
    if not source_files and not domains:
        return []

    source_file_set = set(dedupe_values(source_files)) if source_files else None
    domain_set = set(dedupe_values(domains)) if domains else None

    try:
        collection = vectorstore._collection
        records = collection.get(include=["metadatas"])
        metadatas = records.get("metadatas", [])
    except Exception:
        return dedupe_values(source_files)

    structured_source_files: list[str] = []
    for metadata in metadatas:
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

    if structured_source_files:
        return dedupe_values(structured_source_files)

    return dedupe_values(source_files)


def _collection_count(resolved_vectorstore) -> int:
    try:
        return int(resolved_vectorstore._collection.count())
    except Exception:
        return 0


def _run_child_recall(
    query: str,
    retrieval_plan: dict[str, Any],
    source_files: list[str],
    domains: list[str],
    filter_value: dict | None,
) -> tuple[list[Document], list[Document], list[Document]]:
    if filter_value is not None:
        vector_results = vectorstore.similarity_search(query, k=int(retrieval_plan["vector_k"]), filter=filter_value)
    else:
        vector_results = vectorstore.similarity_search(query, k=int(retrieval_plan["vector_k"]))

    bm25_results = search_bm25_documents(
        query=query,
        source_files=source_files or None,
        domains=domains or None,
        index_name=ES_INDEX_NAME,
        limit=int(retrieval_plan["bm25_k"]),
    )

    fused_results = reciprocal_rank_fusion(
        vector_results,
        bm25_results,
        k=RRF_K,
        vector_weight=float(retrieval_plan["vector_weight"]),
        bm25_weight=float(retrieval_plan["bm25_weight"]),
    )[: int(retrieval_plan["fusion_top_k"])]

    return vector_results, bm25_results, fused_results


def _build_observability_payload(
    query_type: str,
    retrieval_plan: dict[str, Any],
    requested_domains: list[str],
    target_domains: list[str],
    routed_domains: list[str],
    expanded_routed_domains: list[str],
    target_source_file_count: int,
    two_stage_applied: bool,
    two_stage_fallback_reason: str,
    requested_execution_mode: str,
    resolved_execution_mode: str,
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
            "final_context_k": FINAL_CONTEXT_K,
        },
        "requested_domains": requested_domains,
        "target_domains": target_domains,
        "routed_domains": routed_domains,
        "expanded_routed_domains": expanded_routed_domains,
        "target_source_file_count": target_source_file_count,
        "two_stage_applied": two_stage_applied,
        "two_stage_fallback_reason": two_stage_fallback_reason,
    }


def run_retrieval_pipeline(query: str, username: str, domains: list[str] | None = None, query_type: str | None = None) -> dict[str, Any]:
    normalized_query = query.strip()
    normalized_username = normalize_username(username)
    normalized_requested_domains = dedupe_values(domains or [])
    normalized_query_type = normalize_query_type(query_type)
    retrieval_plan = get_retrieval_strategy_plan(normalized_query_type)
    requested_strategy = str(retrieval_plan.get("strategy", "unknown"))
    requested_execution_mode = str(retrieval_plan.get("execution_mode", "hybrid")).strip() or "hybrid"
    supported_execution_modes = {"hybrid", "sql_query"}
    resolved_execution_mode = requested_execution_mode if requested_execution_mode in supported_execution_modes else "hybrid"
    
    if resolved_execution_mode == "hybrid" and _is_sql_query_candidate(normalized_query):
        resolved_execution_mode = "sql_query"

    requested_scope_source_files = _resolve_source_files_for_scope([], normalized_requested_domains) if normalized_requested_domains else _list_all_source_files()
    has_candidate_documents = bool(requested_scope_source_files)
    filter_value = build_source_file_filter(requested_scope_source_files) if requested_scope_source_files else None
    trace = _build_trace(
        query=normalized_query,
        username=normalized_username,
        requested_domains=normalized_requested_domains,
        authorized_domains=normalized_requested_domains,
        authorized_source_files=requested_scope_source_files,
        restricted_source_files=[],
        filter_value=filter_value,
    )
    trace["query_type"] = normalized_query_type
    trace["retrieval_plan"] = retrieval_plan
    trace["requested_execution_mode"] = requested_execution_mode
    trace["resolved_execution_mode"] = resolved_execution_mode

    if requested_scope_source_files:
        print(f"🌐 全局检索启用，当前候选文档数={len(requested_scope_source_files)}，user={normalized_username}")
    else:
        print(f"🌐 接收到检索请求，但当前没有可检索文档，user={normalized_username}")

    if not has_candidate_documents:
        diagnostics = _build_observability_payload(
            query_type=normalized_query_type,
            retrieval_plan=retrieval_plan,
            requested_domains=normalized_requested_domains,
            target_domains=normalized_requested_domains,
            routed_domains=[],
            expanded_routed_domains=[],
            target_source_file_count=0,
            two_stage_applied=False,
            two_stage_fallback_reason="no-candidate-documents",
            requested_execution_mode=requested_execution_mode,
            resolved_execution_mode=resolved_execution_mode,
        )
        response = build_retrieval_response(
            context=[],
            routed_domains=[],
            expanded_routed_domains=[],
            routed_source_files=[],
            authorized_domains=[],
            authorized_source_files=[],
            username=normalized_username,
            query_type=normalized_query_type,
            diagnostics=diagnostics,
        )
        return {
            "status": "success",
            "response": response,
            "trace": trace,
        }

    parent_results: list[Document] = []
    parent_vector_results: list[Document] = []
    parent_bm25_results: list[Document] = []
    narrowed_source_files = list(requested_scope_source_files)
    narrowed_domains = list(normalized_requested_domains)
    effective_authorized_source_files = []
    effective_authorized_domains = []
    routed_domains = _resolve_domains_for_source_files(narrowed_source_files)
    expanded_routed_domains = list(routed_domains)
    two_stage_applied = False
    two_stage_fallback_reason = "disabled"

    if ENABLE_PARENT_CHILD_RETRIEVAL and requested_scope_source_files:
        parent_scope_source_files = _resolve_source_files_for_scope(requested_scope_source_files, normalized_requested_domains) if normalized_requested_domains else requested_scope_source_files
        parent_filter_value = build_source_file_filter(parent_scope_source_files) if parent_scope_source_files else None
        parent_collection_count = _collection_count(parent_vectorstore)
        print(
            f"INFO: 阶段一父文档双路召回，parent_k={PARENT_RECALL_K}, "
            f"authorized_docs={len(requested_scope_source_files)}, authorized_domains={normalized_requested_domains}, filter={parent_filter_value}"
        )
        if parent_collection_count > 0:
            if parent_filter_value is not None:
                parent_vector_results = parent_vectorstore.similarity_search(normalized_query, k=PARENT_RECALL_K, filter=parent_filter_value)
            else:
                parent_vector_results = parent_vectorstore.similarity_search(normalized_query, k=PARENT_RECALL_K)
        else:
            print("INFO: 父向量索引为空，阶段一跳过向量召回")

        parent_bm25_results = search_bm25_documents(
            query=normalized_query,
            source_files=requested_scope_source_files or None,
            domains=normalized_requested_domains or None,
            index_name=ES_PARENT_INDEX_NAME,
            limit=PARENT_RECALL_K,
        )

        raw_parent_vector_hit_count = len(parent_vector_results)
        raw_parent_bm25_hit_count = len(parent_bm25_results)
        parent_vector_results = _dedupe_documents_by_source_file(parent_vector_results)
        parent_bm25_results = _dedupe_documents_by_source_file(parent_bm25_results)
        if len(parent_vector_results) != raw_parent_vector_hit_count or len(parent_bm25_results) != raw_parent_bm25_hit_count:
            print(
                f"INFO: 阶段一按 source_file 去重，"
                f"parent_vector_hits={raw_parent_vector_hit_count}->{len(parent_vector_results)}, "
                f"parent_bm25_hits={raw_parent_bm25_hit_count}->{len(parent_bm25_results)}"
            )

        parent_results = reciprocal_rank_fusion(parent_vector_results, parent_bm25_results, k=RRF_K)[:PARENT_RECALL_K]

        if parent_results:
            parent_target_source_files = _extract_source_files(parent_results)
            if parent_target_source_files:
                primary_domains = _extract_primary_domains(parent_results, parent_target_source_files)
                expanded_routed_domains = _extract_routed_domains(parent_results, parent_target_source_files)
                routed_domains = list(primary_domains)
                narrowed_domains = list(primary_domains)
                if _should_expand_related_domains(parent_results):
                    narrowed_domains = list(expanded_routed_domains)

                combined_domains = _merge_domain_lists(normalized_requested_domains, narrowed_domains)
                combined_source_files, restricted_source_files, combined_authorized_domains = resolve_accessible_sources(
                    normalized_username,
                    combined_domains,
                    DOCS_DIR,
                    USERNAME_GROUP_MAPPING_FILE,
                )
                if ENABLE_ACL and not combined_source_files:
                    denied_domains = combined_domains or _extract_denied_domains(restricted_source_files)
                    print(
                        f"⛔ ACL deny user={normalized_username}, denied_files={restricted_source_files}, "
                        f"effective_domains={combined_domains or ['inferred-global']}"
                    )
                    response = {
                        "status": "forbidden",
                        "message": f"用户 {normalized_username} 无权访问当前命中的知识文档。",
                        "username": normalized_username,
                        "denied_domains": denied_domains,
                        "denied_source_files": restricted_source_files,
                    }
                    trace["authorized_domains"] = []
                    trace["authorized_source_files"] = []
                    return {
                        "status": "forbidden",
                        "response": response,
                        "trace": trace,
                    }
                if combined_source_files:
                    effective_authorized_source_files = combined_source_files
                    effective_authorized_domains = combined_authorized_domains or combined_domains
                    narrowed_source_files = combined_source_files
                    narrowed_domains = combined_authorized_domains or combined_domains
                else:
                    narrowed_domains = combined_domains
                    narrowed_source_files = _resolve_source_files_for_scope(requested_scope_source_files, narrowed_domains)
                    if not narrowed_source_files:
                        narrowed_source_files = parent_target_source_files
                    effective_authorized_domains = narrowed_domains
                two_stage_applied = True
                two_stage_fallback_reason = "applied"
                print(
                    f"INFO: 阶段一完成，parent_vector_hits={len(parent_vector_results)}, parent_bm25_hits={len(parent_bm25_results)}, parent_hits={len(parent_results)}, "
                    f"routed_domains={routed_domains}, expanded_routed_domains={expanded_routed_domains}, narrowed_domains={narrowed_domains}, target_files={narrowed_source_files}"
                )
            else:
                two_stage_fallback_reason = "no-parent-hits"
                print("INFO: 阶段一未命中父文档，回退到授权范围内直接检索")
        else:
            two_stage_fallback_reason = "no-parent-hits"
            print("INFO: 阶段一双路召回未命中父文档，回退到当前单层检索")

    child_filter_value = build_source_file_filter(narrowed_source_files) if narrowed_source_files else filter_value

    if resolved_execution_mode == "sql_query":
        sql_source_files = _resolve_sql_source_files(
            narrowed_source_files or requested_scope_source_files,
            narrowed_domains or effective_authorized_domains or normalized_requested_domains,
            normalized_query,
        )
        sql_filter_value = build_source_file_filter(sql_source_files) if sql_source_files else child_filter_value
        sql_target_source_files = sql_source_files or narrowed_source_files
        print(
            f"INFO: 执行结构化查询，parent_enabled={ENABLE_PARENT_CHILD_RETRIEVAL}, parent_k={PARENT_RECALL_K}, "
            f"two_stage_applied={two_stage_applied}, query_type={normalized_query_type}, strategy={retrieval_plan['strategy']}, "
            f"requested_execution_mode={requested_execution_mode}, execution_mode={resolved_execution_mode}, "
            f"vector_k={retrieval_plan['vector_k']}, bm25_k={retrieval_plan['bm25_k']}, "
            f"vector_weight={retrieval_plan['vector_weight']}, bm25_weight={retrieval_plan['bm25_weight']}, "
            f"fusion_top_k={retrieval_plan['fusion_top_k']}, final_top_k={FINAL_CONTEXT_K}, "
            f"target_files={len(sql_target_source_files)}, target_domains={narrowed_domains}, filter={sql_filter_value}"
        )

        vector_results, bm25_results, fused_results = _run_child_recall(
            query=normalized_query,
            retrieval_plan=retrieval_plan,
            source_files=sql_target_source_files or narrowed_source_files or requested_scope_source_files,
            domains=narrowed_domains or effective_authorized_domains or normalized_requested_domains,
            filter_value=sql_filter_value,
        )

        print(f"INFO: 结构化查询召回完成，vector_hits={len(vector_results)}, bm25_hits={len(bm25_results)}")
        print(f"INFO: 结构化查询 RRF 融合完成，fused_hits={len(fused_results)}")
    else:
        print(
            f"INFO: 执行混合召回，parent_enabled={ENABLE_PARENT_CHILD_RETRIEVAL}, parent_k={PARENT_RECALL_K}, "
            f"two_stage_applied={two_stage_applied}, query_type={normalized_query_type}, strategy={retrieval_plan['strategy']}, "
            f"requested_execution_mode={requested_execution_mode}, execution_mode={resolved_execution_mode}, "
            f"vector_k={retrieval_plan['vector_k']}, bm25_k={retrieval_plan['bm25_k']}, "
            f"vector_weight={retrieval_plan['vector_weight']}, bm25_weight={retrieval_plan['bm25_weight']}, "
            f"fusion_top_k={retrieval_plan['fusion_top_k']}, final_top_k={FINAL_CONTEXT_K}, "
            f"reranker_enabled={RERANKER_ENABLED}, target_files={len(narrowed_source_files)}, target_domains={narrowed_domains}, filter={child_filter_value}"
        )

        vector_results, bm25_results, fused_results = _run_child_recall(
            query=normalized_query,
            retrieval_plan=retrieval_plan,
            source_files=narrowed_source_files or requested_scope_source_files,
            domains=narrowed_domains or effective_authorized_domains or normalized_requested_domains,
            filter_value=child_filter_value,
        )

        print(f"INFO: 混合召回完成，vector_hits={len(vector_results)}, bm25_hits={len(bm25_results)}")
        print(f"INFO: RRF 融合完成，fused_hits={len(fused_results)}")

    reranked_results = rank_results_for_generation(normalized_query, rerank_documents(normalized_query, fused_results))
    selected_results = reranked_results[:FINAL_CONTEXT_K]
    final_results = selected_results

    if CONTEXT_COMPRESSION_ENABLED and selected_results:
        final_results = compress_context(
            query=normalized_query,
            documents=selected_results,
            sentence_limit=CONTEXT_COMPRESSION_SENTENCE_K,
        )
        compression_metrics = _compute_compression_metrics(selected_results, final_results)
        print(
            "INFO: 上下文压缩完成，"
            f"doc_hits={len(final_results)}, compressed_docs={compression_metrics['compressed_doc_count']}, "
            f"saved_chars={compression_metrics['saved_char_count']}, saved_ratio={compression_metrics['saved_ratio']:.2%}, "
            f"min_chars={CONTEXT_COMPRESSION_MIN_CHARS}, sentence_k={CONTEXT_COMPRESSION_SENTENCE_K}"
        )
    else:
        compression_metrics = _compute_compression_metrics(selected_results, final_results)

    trace["parent_results"] = parent_results
    trace["parent_vector_results"] = parent_vector_results
    trace["parent_bm25_results"] = parent_bm25_results
    trace["parent_target_source_files"] = _extract_source_files(parent_results) if two_stage_applied else []
    trace["requested_domains"] = normalized_requested_domains
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
        "query_type": normalized_query_type,
        "strategy": retrieval_plan["strategy"],
        "requested_strategy": requested_strategy,
        "requested_execution_mode": requested_execution_mode,
        "execution_mode": resolved_execution_mode,
        "vector_weight": retrieval_plan["vector_weight"],
        "bm25_weight": retrieval_plan["bm25_weight"],
        "two_stage_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
        "two_stage_applied": two_stage_applied,
        "two_stage_fallback_reason": two_stage_fallback_reason,
        "compression": compression_metrics,
    }

    diagnostics = _build_observability_payload(
        query_type=normalized_query_type,
        retrieval_plan=retrieval_plan,
        requested_domains=normalized_requested_domains,
        target_domains=narrowed_domains or effective_authorized_domains or normalized_requested_domains,
        routed_domains=routed_domains,
        expanded_routed_domains=expanded_routed_domains,
        target_source_file_count=len(narrowed_source_files),
        two_stage_applied=two_stage_applied,
        two_stage_fallback_reason=two_stage_fallback_reason,
        requested_execution_mode=requested_execution_mode,
        resolved_execution_mode=resolved_execution_mode,
    )
    diagnostics["requested_strategy"] = requested_strategy

    context = [
        build_context_entry(index, doc.metadata, doc.page_content)
        for index, doc in enumerate(final_results)
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