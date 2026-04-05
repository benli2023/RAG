import glob
import hashlib
import json
import re
from pathlib import Path
from typing import Any, List

import yaml

import frontmatter
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from access_control import (
    is_document_accessible,
    load_username_group_mapping,
    normalize_username,
    parse_acl_subjects,
    resolve_user_subjects,
)
from faq_support import (
    extract_faq_question,
    format_faq_chunk_content,
    is_faq_document,
    split_structured_faq_chunks,
)
from rag_config import DOCS_DIR, ENABLE_SUB_CHUNKING, MODULE_DIRECTORY_FILE, USERNAME_GROUP_MAPPING_FILE
from retrieval_response_formatter import extract_context_labels

FENCED_BLOCK_PATTERN = re.compile(r"```[\w-]*\n.*?\n```", re.DOTALL)


def list_docs_markdown_files(include_module_directory: bool = False) -> List[Path]:
    md_files = [Path(file_path) for file_path in glob.glob(f"{DOCS_DIR}/**/*.md", recursive=True)]
    if not include_module_directory:
        md_files = [file_path for file_path in md_files if file_path.resolve() != MODULE_DIRECTORY_FILE.resolve()]
    return sorted(md_files)


def get_module_directory_response() -> dict[str, Any]:
    if not MODULE_DIRECTORY_FILE.is_file():
        raise FileNotFoundError("module directory not found: module-directory.yaml")

    with open(MODULE_DIRECTORY_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw_modules = data.get("modules", []) if isinstance(data, dict) else []

    modules = []
    for mod in raw_modules:
        if not isinstance(mod, dict):
            continue
        domain = str(mod.get("domain", "")).strip()
        if not domain:
            continue
        files = [
            {
                "name": str(file_entry.get("name", "")).strip(),
                "description": str(file_entry.get("description", "")).strip(),
                "keywords": [str(kw) for kw in file_entry.get("keywords", []) if kw],
            }
            for file_entry in mod.get("files", [])
            if isinstance(file_entry, dict) and file_entry.get("name")
        ]
        modules.append({
            "domain": domain,
            "description": str(mod.get("description", "")).strip(),
            "keywords": [str(kw) for kw in mod.get("keywords", []) if kw],
            "files": files,
        })

    domains = sorted(mod["domain"] for mod in modules)

    return {
        "source_file": str(MODULE_DIRECTORY_FILE.resolve().relative_to(DOCS_DIR.resolve())).replace("\\", "/"),
        "domains": domains,
        "modules": modules,
    }


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


def split_single_markdown_file(file_path: Path, docs_dir: Path) -> List[Document]:
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[
        ("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")
    ])
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)

    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)

    text_content = post.content
    yaml_metadata = dict(post.metadata) if isinstance(post.metadata, dict) else {}

    if "domain" not in yaml_metadata:
        parts = str(file_path).replace("\\", "/").split("/")
        yaml_metadata["domain"] = parts[-2] if len(parts) >= 2 else "global"

    source_file = str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
    if is_faq_document(yaml_metadata):
        structured_faq_chunks = split_structured_faq_chunks(text_content, yaml_metadata, source_file)
        if structured_faq_chunks:
            return structured_faq_chunks

    md_chunks = markdown_splitter.split_text(text_content)
    final_chunks: List[Document] = []
    next_chunk_index = 1

    for chunk in md_chunks:
        chunk.metadata.update(yaml_metadata)
        chunk.metadata["source_file"] = source_file

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
