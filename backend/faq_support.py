import re
from typing import Any

from langchain_core.documents import Document


FAQ_TEXT_NORMALIZE_PATTERN = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
FAQ_HEADER_KEYS = ("Header 1", "Header 2", "Header 3")
FAQ_QUESTION_BLOCK_PATTERN = re.compile(
    r"(?ms)^###\s*Q[:：]\s*(?P<question>.+?)\n(?P<answer>.*?)(?=^###\s*Q[:：]\s*|\Z)"
)


def metadata_flag_to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    return False


def is_faq_document(metadata: dict[str, Any]) -> bool:
    doc_type = str(metadata.get("type", "")).strip().lower()
    return doc_type == "faq" or metadata_flag_to_bool(metadata.get("faq"))


def extract_faq_question(metadata: dict[str, Any]) -> str:
    explicit_question = metadata.get("faq_question")
    if isinstance(explicit_question, str) and explicit_question.strip():
        return explicit_question.strip()

    for key in reversed(FAQ_HEADER_KEYS):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def extract_markdown_title(text: str) -> str:
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("# "):
            return stripped_line[2:].strip()

    return ""


def normalize_faq_answer(answer_block: str) -> str:
    normalized = answer_block.strip()
    normalized = re.sub(r"^\*\*A[:：]\*\*\s*", "", normalized)
    normalized = re.sub(r"^A[:：]\s*", "", normalized)
    return normalized.strip()


def format_faq_chunk_content(question: str, answer: str) -> str:
    normalized_answer = answer.strip()
    if not normalized_answer:
        return ""

    lines = ["[FAQ: true]", "[类型: faq]"]
    if question:
        lines.append(f"【用户常问】：{question}")
    lines.extend([
        "【标准解答】：",
        normalized_answer,
    ])
    return "\n".join(lines)


def split_structured_faq_chunks(text_content: str, metadata: dict[str, Any], source_file: str) -> list[Document]:
    documents: list[Document] = []
    document_title = extract_markdown_title(text_content)

    for chunk_index, match in enumerate(FAQ_QUESTION_BLOCK_PATTERN.finditer(text_content), start=1):
        question = match.group("question").strip()
        answer = normalize_faq_answer(match.group("answer"))
        faq_content = format_faq_chunk_content(question, answer)
        if not faq_content:
            continue

        faq_metadata = metadata.copy()
        faq_metadata["source_file"] = source_file
        faq_metadata["faq"] = True
        faq_metadata["is_faq"] = True
        faq_metadata["faq_question"] = question
        if document_title:
            faq_metadata["Header 1"] = document_title
        faq_metadata["Header 2"] = question
        faq_metadata["chunk_index"] = chunk_index
        documents.append(Document(page_content=faq_content, metadata=faq_metadata))

    return documents


def normalize_text_for_match(value: str) -> str:
    return FAQ_TEXT_NORMALIZE_PATTERN.sub(" ", value.lower()).strip()


def tokenize_for_match(value: str) -> set[str]:
    normalized = normalize_text_for_match(value)
    if not normalized:
        return set()

    return {token for token in normalized.split() if token}


def score_faq_match(query: str, metadata: dict[str, Any]) -> int:
    if not is_faq_document(metadata):
        return 0

    score = 1
    question = extract_faq_question(metadata)
    normalized_query = normalize_text_for_match(query)
    normalized_question = normalize_text_for_match(question)

    if normalized_query and normalized_question:
        if normalized_query in normalized_question or normalized_question in normalized_query:
            score += 8

        query_tokens = tokenize_for_match(query)
        question_tokens = tokenize_for_match(question)
        if query_tokens and question_tokens:
            overlap_ratio = len(query_tokens & question_tokens) / len(query_tokens)
            if overlap_ratio >= 1:
                score += 6
            elif overlap_ratio >= 0.6:
                score += 4
            elif overlap_ratio >= 0.3:
                score += 2

    return score


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def rank_results_for_generation(query: str, results: list[Document]) -> list[Document]:
    return [doc for _, doc in sorted(
        enumerate(results),
        key=lambda item: (
            _coerce_float(item[1].metadata.get("reranker_score")),
            score_faq_match(query, item[1].metadata),
            -item[0],
        ),
        reverse=True,
    )]
