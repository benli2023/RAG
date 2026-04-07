import glob
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, List

import frontmatter
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
import yaml

from access_control import (
    is_document_accessible,
    load_username_group_mapping,
    normalize_username,
    parse_acl_subjects,
    resolve_user_subjects,
)
from faq_support import (
    extract_faq_question,
    extract_markdown_title,
    format_faq_chunk_content,
    is_faq_document,
    split_structured_faq_chunks,
)
from rag_config import DOCS_DIR, ENABLE_SUB_CHUNKING, USERNAME_GROUP_MAPPING_FILE
from retrieval_response_formatter import extract_context_labels

FENCED_BLOCK_PATTERN = re.compile(r"```[\w-]*\n.*?\n```", re.DOTALL)
MODULE_DIRECTORY_FILE = DOCS_DIR / "module-directory.yaml"


def list_docs_markdown_files() -> List[Path]:
    md_files = [Path(file_path) for file_path in glob.glob(f"{DOCS_DIR}/**/*.md", recursive=True)]
    return sorted(md_files)


def normalize_docs_relative_path(relative_path: str) -> Path:
    cleaned = relative_path.strip().lstrip("/\\")
    if not cleaned:
        raise ValueError("relative path is empty")

    candidate = (DOCS_DIR / cleaned).resolve()
    if DOCS_DIR.resolve() not in candidate.parents and candidate != DOCS_DIR.resolve():
        raise ValueError("path escapes docs directory")

    if not candidate.is_file():
        raise FileNotFoundError(f"document not found: {relative_path}")

    return candidate


def assert_document_access(path: Path, username: str) -> None:
    normalized_username = normalize_username(username)

    with open(path, "r", encoding="utf-8") as file_handle:
        post = frontmatter.load(file_handle)

    metadata = dict(post.metadata) if isinstance(post.metadata, dict) else {}
    acl_subjects = parse_acl_subjects(metadata)
    group_mapping = load_username_group_mapping(USERNAME_GROUP_MAPPING_FILE)
    user_subjects = resolve_user_subjects(normalized_username, group_mapping)

    if is_document_accessible(user_subjects, acl_subjects):
        return

    source_file = str(path.resolve().relative_to(DOCS_DIR.resolve())).replace("\\", "/")
    raise HTTPException(
        status_code=403,
        detail={
            "status": "forbidden",
            "message": f"用户 {normalized_username} 无权访问文档 {source_file}。",
            "username": normalized_username,
            "source_file": source_file,
        },
    )


def build_chunk_id(metadata: dict, chunk_index: int, content: str | None = None) -> str:
    domain, doc_type, headers = extract_context_labels(metadata)
    source_file = str(metadata.get("source_file", ""))
    normalized_content = " ".join(str(content or "").split())
    content_digest = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    raw_key = f"{domain}|{doc_type}|{headers}|{source_file}|{chunk_index}|{content_digest}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def sanitize_metadata_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    if isinstance(value, list):
        sanitized_list = []
        for item in value:
            sanitized_item = sanitize_metadata_value(item)
            if isinstance(sanitized_item, (str, int, float, bool)) or sanitized_item is None:
                sanitized_list.append(sanitized_item)
            else:
                sanitized_list.append(str(sanitized_item))
        return sanitized_list

    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    return str(value)


def sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_metadata_value(value) for key, value in metadata.items()}


def _normalize_metadata_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_search_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _normalize_metadata_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_keywords = value
    elif isinstance(value, str):
        raw_keywords = re.split(r"[,，;；\n]+", value)
    else:
        raw_keywords = []

    normalized_keywords: list[str] = []
    for keyword in raw_keywords:
        cleaned_keyword = _normalize_metadata_text(keyword)
        if cleaned_keyword and cleaned_keyword not in normalized_keywords:
            normalized_keywords.append(cleaned_keyword)
    return normalized_keywords


def _normalize_related_domains(value: Any, current_domain: str) -> list[str]:
    if isinstance(value, list):
        raw_domains = value
    elif isinstance(value, str):
        raw_domains = re.split(r"[,，;；\n]+", value)
    else:
        raw_domains = []

    normalized_domains: list[str] = []
    for raw_domain in raw_domains:
        cleaned_domain = _normalize_metadata_text(raw_domain)
        if cleaned_domain and cleaned_domain not in normalized_domains:
            normalized_domains.append(cleaned_domain)

    if current_domain and current_domain not in normalized_domains:
        normalized_domains.insert(0, current_domain)

    return normalized_domains


@lru_cache(maxsize=1)
def _load_module_directory_payload() -> dict[str, Any]:
    if not MODULE_DIRECTORY_FILE.is_file():
        return {}

    with open(MODULE_DIRECTORY_FILE, "r", encoding="utf-8") as file_handle:
        payload = yaml.safe_load(file_handle)

    return payload if isinstance(payload, dict) else {}


def _build_domain_alias_map() -> dict[str, list[str]]:
    payload = _load_module_directory_payload()
    modules = payload.get("modules", [])
    if not isinstance(modules, list):
        return {}

    alias_map: dict[str, list[str]] = {}
    for module in modules:
        if not isinstance(module, dict):
            continue

        domain = _normalize_metadata_text(module.get("domain"))
        if not domain:
            continue

        aliases: list[str] = [domain]
        aliases.extend(_normalize_metadata_keywords(module.get("keywords")))

        files = module.get("files", [])
        if isinstance(files, list):
            for file_item in files:
                if not isinstance(file_item, dict):
                    continue
                aliases.extend(_normalize_metadata_keywords(file_item.get("keywords")))

        unique_aliases: list[str] = []
        for alias in aliases:
            cleaned_alias = _normalize_metadata_text(alias)
            if cleaned_alias and cleaned_alias not in unique_aliases:
                unique_aliases.append(cleaned_alias)

        alias_map[domain] = unique_aliases

    return alias_map


def _protect_fenced_blocks(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        placeholder = f"__FENCED_BLOCK_{len(placeholders)}__"
        placeholders[placeholder] = match.group(0)
        return placeholder

    return FENCED_BLOCK_PATTERN.sub(_replace, text), placeholders


def _restore_fenced_blocks(text: str, placeholders: dict[str, str]) -> str:
    for placeholder, block in placeholders.items():
        text = text.replace(placeholder, block)
    return text


def _load_markdown_payload(file_path: Path, docs_dir: Path) -> tuple[str, dict[str, Any], str]:
    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)

    text_content = post.content
    yaml_metadata = dict(post.metadata) if isinstance(post.metadata, dict) else {}

    if "domain" not in yaml_metadata:
        relative_parts = file_path.resolve().relative_to(docs_dir.resolve()).parts
        yaml_metadata["domain"] = relative_parts[0] if len(relative_parts) > 1 else "global"

    current_domain = _normalize_metadata_text(yaml_metadata.get("domain") or "global") or "global"
    yaml_metadata["domain"] = current_domain
    yaml_metadata["description"] = _normalize_metadata_text(yaml_metadata.get("description"))
    yaml_metadata["keywords"] = _normalize_metadata_keywords(yaml_metadata.get("keywords"))
    yaml_metadata["related_domains"] = _normalize_related_domains(yaml_metadata.get("related_domains"), current_domain)

    source_file = str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
    return text_content, yaml_metadata, source_file


def _extract_related_domains(text_content: str, yaml_metadata: dict[str, Any], source_file: str) -> list[str]:
    current_domain = str(yaml_metadata.get("domain", "global")).strip()
    explicit_related_domains = _normalize_related_domains(yaml_metadata.get("related_domains"), "")
    if explicit_related_domains:
        return explicit_related_domains

    normalized_text = f"{source_file}\n{text_content}".lower()
    compact_text = _normalize_search_text(f"{source_file}\n{text_content}")
    domain_scores: dict[str, int] = {}
    for domain_name, aliases in _build_domain_alias_map().items():
        score = 0
        for alias in aliases:
            alias_text = alias.lower()
            compact_alias = _normalize_search_text(alias)
            if alias_text and alias_text in normalized_text:
                score += normalized_text.count(alias_text)
            elif compact_alias and compact_alias in compact_text:
                score += compact_text.count(compact_alias)
        if score > 0:
            domain_scores[domain_name] = score

    if current_domain:
        domain_scores[current_domain] = max(domain_scores.get(current_domain, 0), 1)

    ranked_domains = [
        domain_name
        for domain_name, _ in sorted(domain_scores.items(), key=lambda item: (item[1], item[0] == current_domain), reverse=True)
    ]

    if current_domain and current_domain in ranked_domains:
        ranked_domains.remove(current_domain)
        ranked_domains.insert(0, current_domain)

    if current_domain == "global":
        return ranked_domains[:3]
    return ranked_domains[:2]


def _build_parent_document(text_content: str, yaml_metadata: dict[str, Any], source_file: str) -> Document | None:
    title = str(yaml_metadata.get("title") or extract_markdown_title(text_content) or Path(source_file).stem).strip()
    description = _normalize_metadata_text(yaml_metadata.get("description"))
    if not description:
        return None

    related_domains = _extract_related_domains(text_content, yaml_metadata, source_file)
    parent_metadata = yaml_metadata.copy()
    parent_metadata["source_file"] = source_file
    parent_metadata["chunk_index"] = 0
    if description:
        parent_metadata["parent_description"] = description
    if related_domains:
        parent_metadata["related_domains"] = related_domains
    if title:
        parent_metadata["parent_title"] = title

    return Document(page_content=description, metadata=parent_metadata)


def _build_child_documents(text_content: str, yaml_metadata: dict[str, Any], source_file: str) -> List[Document]:
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[
        ("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")
    ])
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)
    if is_faq_document(yaml_metadata):
        structured_faq_chunks = split_structured_faq_chunks(text_content, yaml_metadata, source_file)
        if structured_faq_chunks:
            for doc in structured_faq_chunks:
                doc.metadata["parent_source_file"] = source_file
            return structured_faq_chunks

    md_chunks = markdown_splitter.split_text(text_content)
    final_chunks: List[Document] = []
    next_chunk_index = 1

    for chunk in md_chunks:
        chunk.metadata.update(yaml_metadata)
        chunk.metadata["source_file"] = source_file
        chunk.metadata["parent_source_file"] = source_file

        if is_faq_document(chunk.metadata):
            faq_question = extract_faq_question(chunk.metadata)
            faq_content = format_faq_chunk_content(faq_question, chunk.page_content)
            if not faq_content:
                continue

            faq_metadata = chunk.metadata.copy()
            faq_metadata["faq"] = True
            faq_metadata["is_faq"] = True
            if faq_question:
                faq_metadata["faq_question"] = faq_question

            final_chunks.append(Document(
                page_content=faq_content,
                metadata={
                    **faq_metadata,
                    "chunk_index": next_chunk_index,
                },
            ))
            next_chunk_index += 1
            continue

        protected_content, placeholders = _protect_fenced_blocks(chunk.page_content)
        
        description = _normalize_metadata_text(yaml_metadata.get("description"))
        keywords = yaml_metadata.get("keywords", [])
        keywords_str = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
        
        injection_text = ""
        if description:
            injection_text += f"[文档描述: {description}]\n"
        if keywords_str:
            injection_text += f"[文档关键词: {keywords_str}]\n"
            
        if injection_text and chunk.page_content.strip():
            protected_content = f"{injection_text}{protected_content}"

        protected_chunk = Document(page_content=protected_content, metadata=chunk.metadata.copy())
        sub_chunks = text_splitter.split_documents([protected_chunk]) if ENABLE_SUB_CHUNKING else [protected_chunk]
        for doc in sub_chunks:
            doc.page_content = _restore_fenced_blocks(doc.page_content, placeholders)
            if not doc.page_content.strip():
                continue
            doc.metadata["chunk_index"] = next_chunk_index
            final_chunks.append(doc)
            next_chunk_index += 1

    return final_chunks


def build_index_documents(file_path: Path, docs_dir: Path) -> tuple[Document | None, List[Document]]:
    text_content, yaml_metadata, source_file = _load_markdown_payload(file_path, docs_dir)
    parent_document = _build_parent_document(text_content, yaml_metadata, source_file)
    child_documents = _build_child_documents(text_content, yaml_metadata, source_file)
    return parent_document, child_documents


def split_single_markdown_file(file_path: Path, docs_dir: Path) -> List[Document]:
    _, child_documents = build_index_documents(file_path, docs_dir)
    return child_documents
