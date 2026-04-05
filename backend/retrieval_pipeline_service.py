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


def _extract_primary_domains(documents: list[Document], fallback_source_files: list[str]) -> list[str]:
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        if domain:
            return [domain]

    return _resolve_domains_for_source_files(fallback_source_files)


def _should_expand_related_domains(documents: list[Document]) -> bool:
    for doc in documents:
        domain = str(doc.metadata.get("domain", "")).strip()
        doc_type = str(doc.metadata.get("type", "")).strip().lower()
        related_domains = _coerce_metadata_list(doc.metadata.get("related_domains"))
        if domain == "global" and doc_type != "catalog" and len(related_domains) > 1:
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


def _collection_count(resolved_vectorstore) -> int:
    try:
        return int(resolved_vectorstore._collection.count())
    except Exception:
        return 0


def run_retrieval_pipeline(query: str, username: str) -> dict[str, Any]:
    normalized_query = query.strip()
    normalized_username = normalize_username(username)
    authorized_source_files, restricted_source_files, authorized_domains = resolve_accessible_sources(
        normalized_username,
        [],
        DOCS_DIR,
        USERNAME_GROUP_MAPPING_FILE,
    )
    has_candidate_documents = bool(authorized_source_files or restricted_source_files)
    filter_value = build_source_file_filter(authorized_source_files) if authorized_source_files else None
    trace = _build_trace(
        query=normalized_query,
        username=normalized_username,
        requested_domains=[],
        authorized_domains=authorized_domains,
        authorized_source_files=authorized_source_files,
        restricted_source_files=restricted_source_files,
        filter_value=filter_value,
    )

    if ENABLE_ACL and restricted_source_files and not authorized_source_files:
        denied_domains = _extract_denied_domains(restricted_source_files)
        print(f"⛔ ACL deny user={normalized_username}, denied_files={restricted_source_files}, requested_domains=[inferred-global]")
        response = {
            "status": "forbidden",
            "message": f"用户 {normalized_username} 无权访问当前命中的知识文档。",
            "username": normalized_username,
            "denied_domains": denied_domains,
            "denied_source_files": restricted_source_files,
        }
        return {
            "status": "forbidden",
            "response": response,
            "trace": trace,
        }

    if authorized_source_files:
        if ENABLE_ACL:
            print(f"🔐 全局检索已按文档 ACL 收敛，允许文档数={len(authorized_source_files)}，user={normalized_username}")
        else:
            print(f"🌐 全局检索启用，当前可检索文档数={len(authorized_source_files)}，user={normalized_username}")
    else:
        print(f"🌐 接收到检索请求，但当前没有可检索文档，user={normalized_username}")

    if not has_candidate_documents:
        response = build_retrieval_response(
            context=[],
            routed_domains=[],
            expanded_routed_domains=[],
            routed_source_files=[],
            authorized_domains=[],
            authorized_source_files=[],
            username=normalized_username,
        )
        return {
            "status": "success",
            "response": response,
            "trace": trace,
        }

    parent_results: list[Document] = []
    parent_vector_results: list[Document] = []
    parent_bm25_results: list[Document] = []
    narrowed_source_files = list(authorized_source_files)
    narrowed_domains = list(authorized_domains)
    routed_domains = _resolve_domains_for_source_files(narrowed_source_files)
    expanded_routed_domains = list(routed_domains)
    two_stage_applied = False
    two_stage_fallback_reason = "disabled"

    if ENABLE_PARENT_CHILD_RETRIEVAL and authorized_source_files:
        parent_scope_source_files = _resolve_source_files_for_scope(authorized_source_files, authorized_domains)
        parent_filter_value = build_source_file_filter(parent_scope_source_files) if parent_scope_source_files else None
        parent_collection_count = _collection_count(parent_vectorstore)
        print(
            f"INFO: 阶段一父文档双路召回，parent_k={PARENT_RECALL_K}, "
            f"authorized_docs={len(authorized_source_files)}, authorized_domains={authorized_domains}, filter={parent_filter_value}"
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
            source_files=authorized_source_files or None,
            domains=authorized_domains or None,
            index_name=ES_PARENT_INDEX_NAME,
            limit=PARENT_RECALL_K,
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

                narrowed_source_files = _resolve_source_files_for_scope(authorized_source_files, narrowed_domains)
                if not narrowed_source_files:
                    narrowed_source_files = parent_target_source_files
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

    print(
        f"INFO: 执行混合召回，parent_enabled={ENABLE_PARENT_CHILD_RETRIEVAL}, parent_k={PARENT_RECALL_K}, "
        f"two_stage_applied={two_stage_applied}, vector_k={VECTOR_RECALL_K}, bm25_k={BM25_RECALL_K}, "
        f"fusion_top_k={FUSION_TOP_K}, final_top_k={FINAL_CONTEXT_K}, "
        f"reranker_enabled={RERANKER_ENABLED}, target_files={len(narrowed_source_files)}, target_domains={narrowed_domains}, filter={child_filter_value}"
    )

    if child_filter_value is not None:
        vector_results = vectorstore.similarity_search(normalized_query, k=VECTOR_RECALL_K, filter=child_filter_value)
    else:
        vector_results = vectorstore.similarity_search(normalized_query, k=VECTOR_RECALL_K)

    bm25_results = search_bm25_documents(
        query=normalized_query,
        source_files=authorized_source_files or None,
        domains=narrowed_domains or None,
        index_name=ES_INDEX_NAME,
        limit=BM25_RECALL_K,
    )

    print(f"INFO: 混合召回完成，vector_hits={len(vector_results)}, bm25_hits={len(bm25_results)}")

    fused_results = reciprocal_rank_fusion(vector_results, bm25_results, k=RRF_K)[:FUSION_TOP_K]
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
    trace["requested_domains"] = []
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
        "two_stage_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
        "two_stage_applied": two_stage_applied,
        "two_stage_fallback_reason": two_stage_fallback_reason,
        "compression": compression_metrics,
    }

    context = [
        build_context_entry(index, doc.metadata, doc.page_content)
        for index, doc in enumerate(final_results)
    ]

    response = build_retrieval_response(
        context=context,
        routed_domains=routed_domains,
        expanded_routed_domains=expanded_routed_domains,
        routed_source_files=narrowed_source_files,
        authorized_domains=authorized_domains,
        authorized_source_files=authorized_source_files,
        username=normalized_username,
    )
    return {
        "status": "success",
        "response": response,
        "trace": trace,
    }