from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from access_control import build_source_file_filter, dedupe_values, normalize_username, resolve_accessible_sources
from context_compression_service import compress_context
from es_service import search_bm25_documents
from faq_support import rank_results_for_generation
from hybrid_retrieval_service import reciprocal_rank_fusion
from rag_config import BM25_RECALL_K, CONTEXT_COMPRESSION_ENABLED, CONTEXT_COMPRESSION_SENTENCE_K, DOCS_DIR, ENABLE_ACL, FINAL_CONTEXT_K, FUSION_TOP_K, RERANKER_ENABLED, RRF_K, USERNAME_GROUP_MAPPING_FILE, VECTOR_RECALL_K
from rag_store import vectorstore
from reranker_service import rerank_documents
from retrieval_response_formatter import build_context_entry, build_retrieval_response


def _extract_denied_domains(source_files: list[str]) -> list[str]:
    return sorted({Path(source_file).parts[0] for source_file in source_files if Path(source_file).parts})


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
        "vector_results": [],
        "bm25_results": [],
        "fused_results": [],
        "reranked_results": [],
        "selected_results": [],
        "final_results": [],
        "metrics": {
            "vector_hit_count": 0,
            "bm25_hit_count": 0,
            "fused_hit_count": 0,
            "reranked_hit_count": 0,
            "selected_hit_count": 0,
            "final_hit_count": 0,
            "shared_fused_hit_count": 0,
            "compression": {
                "enabled": CONTEXT_COMPRESSION_ENABLED,
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
        "sentence_limit": CONTEXT_COMPRESSION_SENTENCE_K,
        "compressed_doc_count": compressed_doc_count,
        "original_char_count": original_char_count,
        "compressed_char_count": compressed_char_count,
        "saved_char_count": saved_char_count,
        "saved_ratio": saved_ratio,
    }


def run_retrieval_pipeline(query: str, username: str, domains: list[str] | None = None) -> dict[str, Any]:
    normalized_query = query.strip()
    normalized_username = normalize_username(username)
    requested_domains = dedupe_values(domains or [])
    authorized_source_files, restricted_source_files, authorized_domains = resolve_accessible_sources(
        normalized_username,
        requested_domains,
        DOCS_DIR,
        USERNAME_GROUP_MAPPING_FILE,
    )
    has_candidate_documents = bool(authorized_source_files or restricted_source_files)
    filter_value = build_source_file_filter(authorized_source_files) if authorized_source_files else None
    trace = _build_trace(
        query=normalized_query,
        username=normalized_username,
        requested_domains=requested_domains,
        authorized_domains=authorized_domains,
        authorized_source_files=authorized_source_files,
        restricted_source_files=restricted_source_files,
        filter_value=filter_value,
    )

    if ENABLE_ACL and restricted_source_files and not authorized_source_files:
        denied_domains = _extract_denied_domains(restricted_source_files)
        print(f"⛔ ACL deny user={normalized_username}, denied_files={restricted_source_files}, requested_domains={requested_domains or '[global]'}")
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
        if requested_domains:
            if ENABLE_ACL:
                print(f"🎯 接收到前端的大模型路由，锁定模块: {requested_domains}，ACL 收敛后文档数={len(authorized_source_files)}，user={normalized_username}")
            else:
                print(f"🎯 接收到前端的大模型路由，锁定模块: {requested_domains}，可检索文档数={len(authorized_source_files)}，user={normalized_username}")
        else:
            if ENABLE_ACL:
                print(f"🔐 全局检索已按文档 ACL 收敛，允许文档数={len(authorized_source_files)}，user={normalized_username}")
            else:
                print(f"🌐 全局检索启用，当前可检索文档数={len(authorized_source_files)}，user={normalized_username}")
    elif requested_domains:
        print(f"🎯 接收到前端的大模型路由，但模块 {requested_domains} 下没有可检索文档，user={normalized_username}")
    else:
        print("🌐 接收到空路由，执行全局检索")

    if requested_domains and not has_candidate_documents:
        response = build_retrieval_response(
            context=[],
            requested_domains=requested_domains,
            authorized_domains=[],
            authorized_source_files=[],
            username=normalized_username,
        )
        return {
            "status": "success",
            "response": response,
            "trace": trace,
        }

    if ENABLE_ACL and requested_domains and not authorized_source_files and restricted_source_files:
        denied_domains = _extract_denied_domains(restricted_source_files)
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

    if ENABLE_ACL and not requested_domains and not authorized_source_files and restricted_source_files:
        print(f"⛔ ACL deny user={normalized_username}, no accessible documents for global retrieval")
        if filter_value is None:
            response = {
                "status": "forbidden",
                "message": f"用户 {normalized_username} 当前没有任何可检索的知识文档权限。",
                "username": normalized_username,
                "denied_domains": _extract_denied_domains(restricted_source_files),
                "denied_source_files": restricted_source_files,
            }
            return {
                "status": "forbidden",
                "response": response,
                "trace": trace,
            }

    print(
        f"INFO: 执行混合召回，vector_k={VECTOR_RECALL_K}, bm25_k={BM25_RECALL_K}, "
        f"fusion_top_k={FUSION_TOP_K}, final_top_k={FINAL_CONTEXT_K}, "
        f"reranker_enabled={RERANKER_ENABLED}, filter={filter_value}"
    )

    if filter_value is not None:
        vector_results = vectorstore.similarity_search(normalized_query, k=VECTOR_RECALL_K, filter=filter_value)
    else:
        vector_results = vectorstore.similarity_search(normalized_query, k=VECTOR_RECALL_K)

    bm25_results = search_bm25_documents(
        query=normalized_query,
        source_files=authorized_source_files or None,
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
            f"sentence_k={CONTEXT_COMPRESSION_SENTENCE_K}"
        )
    else:
        compression_metrics = _compute_compression_metrics(selected_results, final_results)

    trace["vector_results"] = vector_results
    trace["bm25_results"] = bm25_results
    trace["fused_results"] = fused_results
    trace["reranked_results"] = reranked_results
    trace["selected_results"] = selected_results
    trace["final_results"] = final_results
    trace["metrics"] = {
        "vector_hit_count": len(vector_results),
        "bm25_hit_count": len(bm25_results),
        "fused_hit_count": len(fused_results),
        "reranked_hit_count": len(reranked_results),
        "selected_hit_count": len(selected_results),
        "final_hit_count": len(final_results),
        "shared_fused_hit_count": sum(1 for doc in fused_results if len(doc.metadata.get("retrieval_sources", [])) > 1),
        "compression": compression_metrics,
    }

    context = [
        build_context_entry(index, doc.metadata, doc.page_content)
        for index, doc in enumerate(final_results)
    ]

    response = build_retrieval_response(
        context=context,
        requested_domains=requested_domains,
        authorized_domains=authorized_domains,
        authorized_source_files=authorized_source_files,
        username=normalized_username,
    )
    return {
        "status": "success",
        "response": response,
        "trace": trace,
    }