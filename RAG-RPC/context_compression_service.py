from __future__ import annotations

import re

from langchain_core.documents import Document

from faq_support import is_faq_document
from rag_config import CONTEXT_COMPRESSION_ENABLED, CONTEXT_COMPRESSION_MIN_CHARS, CONTEXT_COMPRESSION_SENTENCE_K
from reranker_service import predict_relevance_scores


SENTENCE_EXTRACT_PATTERN = re.compile(r"[^。！？.!?\n]+[。！？.!?]?", re.UNICODE)
STRUCTURED_LINE_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\|)")


def _split_sentences(text: str) -> list[str]:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    if not normalized_text:
        return []

    sentences = [sentence.strip() for sentence in SENTENCE_EXTRACT_PATTERN.findall(normalized_text) if sentence.strip()]
    return sentences if sentences else [normalized_text]


def _split_structured_lines(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    structured_lines = [line for line in lines if STRUCTURED_LINE_PATTERN.match(line)]
    if len(structured_lines) >= 2 and len(structured_lines) >= max(2, len(lines) // 2):
        return structured_lines
    return []


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compress_context(query: str, documents: list[Document], sentence_limit: int | None = None) -> list[Document]:
    if not documents or not CONTEXT_COMPRESSION_ENABLED:
        return documents

    resolved_sentence_limit = max(1, sentence_limit or CONTEXT_COMPRESSION_SENTENCE_K)
    resolved_min_chars = max(1, CONTEXT_COMPRESSION_MIN_CHARS)
    compressed_documents: list[Document] = []

    for doc in documents:
        if is_faq_document(doc.metadata):
            compressed_documents.append(doc)
            continue

        normalized_content = _normalize_text(doc.page_content)
        if len(normalized_content) < resolved_min_chars:
            compressed_documents.append(doc)
            continue

        structured_lines = _split_structured_lines(doc.page_content)
        units = structured_lines if structured_lines else _split_sentences(doc.page_content)
        joiner = "\n" if structured_lines else " ... "

        if len(units) <= resolved_sentence_limit:
            compressed_documents.append(doc)
            continue

        scores = predict_relevance_scores(query, units)
        if len(scores) != len(units):
            compressed_documents.append(doc)
            continue

        ranked_sentences = sorted(
            enumerate(zip(units, scores)),
            key=lambda item: item[1][1],
            reverse=True,
        )[:resolved_sentence_limit]
        selected_indexes = sorted(index for index, _ in ranked_sentences)
        compressed_text = joiner.join(units[index] for index in selected_indexes).strip()

        if not compressed_text:
            compressed_documents.append(doc)
            continue

        compressed_documents.append(Document(
            page_content=compressed_text,
            metadata={
                **doc.metadata,
                "context_compressed": True,
                "context_compression_sentence_k": resolved_sentence_limit,
                "original_sentence_count": len(units),
                "context_compression_mode": "structured" if structured_lines else "sentence",
            },
        ))

    return compressed_documents