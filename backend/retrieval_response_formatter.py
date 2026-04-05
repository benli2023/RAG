from typing import Any

from faq_support import extract_faq_question, is_faq_document


def extract_context_labels(metadata: dict[str, Any]) -> tuple[str, str, str]:
    domain = str(metadata.get("domain", "未知域"))
    doc_type = str(metadata.get("type", "未知类型"))
    headers = " > ".join(
        value
        for _, value in sorted(
            (
                (key, value)
                for key, value in metadata.items()
                if key.startswith("Header") and isinstance(value, str) and len(value) > 0
            ),
            key=lambda item: item[0],
        )
    )
    return domain, doc_type, headers


def build_context_entry(index: int, metadata: dict[str, Any], page_content: str) -> dict[str, Any]:
    domain, doc_type, headers = extract_context_labels(metadata)
    source_file = str(metadata.get("source_file", "unknown"))
    is_faq = is_faq_document(metadata)
    faq_question = extract_faq_question(metadata)
    reranker_score = metadata.get("reranker_score")
    ctx_id = f"{index + 1:02d}"
    formatted_page_content = (
        f"--- [ ID: {ctx_id} | 来源: {source_file} | 模块: {domain} | 类型: {doc_type} | 章节: {headers} | FAQ: {'true' if is_faq else 'false'}{f' | FAQ问题: {faq_question}' if faq_question else ''}] ---\n"
        f"{page_content}"
    )
    return {
        "metadata": metadata,
        "reranker_score": reranker_score,
        "page_content": formatted_page_content,
    }


def build_retrieval_response(
    context: list[dict[str, Any]],
    routed_domains: list[str],
    expanded_routed_domains: list[str],
    routed_source_files: list[str],
    authorized_domains: list[str],
    authorized_source_files: list[str],
    username: str,
) -> dict[str, Any]:
    return {
        "context": context,
        "routed_domains": routed_domains,
        "expanded_routed_domains": expanded_routed_domains,
        "routed_source_files": routed_source_files,
        "authorized_domains": authorized_domains,
        "authorized_source_files": authorized_source_files,
        "username": username,
    }
