import os
import sys
from pathlib import Path

import frontmatter
import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from access_control import ANONYMOUS_USERNAME
from api_models import QueryRequest, SourceFileRequest
from documents_service import assert_document_access, build_index_documents, list_docs_markdown_files, normalize_docs_relative_path
from es_service import clear_es_index, delete_by_source_file_in_es, upsert_chunks_to_es
from rag_config import DASHBOARD_DIR, DASHBOARD_INDEX, DOCS_DIR, ENABLE_ACL, ENABLE_PARENT_CHILD_RETRIEVAL, ENABLE_SUB_CHUNKING, ES_INDEX_NAME, ES_PARENT_INDEX_NAME, get_runtime_config
from rag_store import embedding_function, parent_vectorstore, vectorstore
from retrieval_pipeline_service import run_retrieval_pipeline
from vectorstore_service import clear_vectorstore, delete_by_source_file, get_chunk_statistics, get_grouped_source_files_from_vectorstore, upsert_chunks

app = FastAPI()

MODULE_DIRECTORY_FILE = DOCS_DIR / "module-directory.yaml"

if DASHBOARD_DIR.exists():
    app.mount("/static-dashboard", StaticFiles(directory=str(DASHBOARD_DIR)), name="static-dashboard")


_progress_line_length = 0


def _print_progress(current: int, total: int, prefix: str = "Progress", width: int = 30) -> None:
    global _progress_line_length

    if total <= 0:
        return

    filled = int(width * current / total)
    bar = "█" * filled + "-" * (width - filled)
    message = f"{prefix} |{bar}| {current}/{total}"
    padding = max(_progress_line_length - len(message), 0)
    sys.stdout.write(f"\r{message}{' ' * padding}")
    _progress_line_length = len(message)
    sys.stdout.flush()


def _finish_progress() -> None:
    global _progress_line_length

    sys.stdout.write("\r")
    if _progress_line_length:
        sys.stdout.write(" " * _progress_line_length)
        sys.stdout.write("\r")
    sys.stdout.write("\n")
    _progress_line_length = 0
    sys.stdout.flush()


def get_module_directory_response() -> dict:
    if not MODULE_DIRECTORY_FILE.is_file():
        raise FileNotFoundError(f"module directory file not found: {MODULE_DIRECTORY_FILE}")

    with open(MODULE_DIRECTORY_FILE, "r", encoding="utf-8") as file_handle:
        payload = yaml.safe_load(file_handle)

    return payload if isinstance(payload, dict) else {"modules": []}

# ================= 3. 核心业务字典：意图路由配置 =================

# ================= 4. [入库 API] 带元数据解析的自定义切片 =================
@app.post("/ingest")
def ingest_docs():
    docs_dir = DOCS_DIR
    md_files = list_docs_markdown_files()

    parent_docs = []
    final_chunks = []
    total_files = len(md_files)
    
    for file_index, file_path in enumerate(md_files, start=1):
        parent_doc, file_chunks = build_index_documents(file_path, docs_dir)
        if parent_doc is not None:
            parent_docs.append(parent_doc)
        final_chunks.extend(file_chunks)
        _print_progress(
            file_index,
            total_files,
            prefix=f"Ingest {os.path.basename(file_path)}",
        )

    _finish_progress()

    # 存入数据库
    if not final_chunks and not parent_docs:
        return {"message": f"未找到可入库的 Markdown 内容，共扫描 {len(md_files)} 个文件。"}
    parent_upsert_result = upsert_chunks(parent_docs, embedding_function, parent_vectorstore)
    upsert_result = upsert_chunks(final_chunks, embedding_function, vectorstore)
    parent_es_upsert_result = upsert_chunks_to_es(parent_docs, index_name=ES_PARENT_INDEX_NAME)
    child_es_upsert_result = upsert_chunks_to_es(final_chunks, index_name=ES_INDEX_NAME)
    es_sync_message = "并已同步检索索引。" if parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"] else "，但未启用 ES BM25，同步已跳过。"
    return {
        "message": f"成功处理 {len(md_files)} 个文件，生成 {len(parent_docs)} 个父文档摘要和 {len(final_chunks)} 个子切片{es_sync_message}",
        "parent_child_retrieval_enabled": ENABLE_PARENT_CHILD_RETRIEVAL,
        "sub_chunking_enabled": ENABLE_SUB_CHUNKING,
        "parent_chunk_count": parent_upsert_result["chunk_count"],
        "parent_embed_seconds": parent_upsert_result["embed_seconds"],
        "parent_add_documents_seconds": parent_upsert_result["add_documents_seconds"],
        "child_chunk_count": upsert_result["chunk_count"],
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
        "es_enabled": parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"],
        "es_parent_index_name": parent_es_upsert_result["index_name"],
        "es_parent_indexed_count": parent_es_upsert_result["indexed_count"],
        "es_parent_sync_seconds": parent_es_upsert_result["sync_seconds"],
        "es_index_name": child_es_upsert_result["index_name"],
        "es_indexed_count": child_es_upsert_result["indexed_count"],
        "es_sync_seconds": child_es_upsert_result["sync_seconds"],
    }


@app.post("/clear")
def clear_index():
    deleted_parent_count = clear_vectorstore(parent_vectorstore)
    deleted_vector_count = clear_vectorstore(vectorstore)
    deleted_parent_es_count = clear_es_index(index_name=ES_PARENT_INDEX_NAME)
    deleted_es_count = clear_es_index(index_name=ES_INDEX_NAME)
    return {
        "message": f"已清空检索索引，父文档索引删除 {deleted_parent_count} 条，子切片向量库删除 {deleted_vector_count} 条，父 BM25 索引删除 {deleted_parent_es_count} 条，子 BM25 索引删除 {deleted_es_count} 条。",
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
    }


@app.post("/rebuild")
def rebuild_index():
    deleted_parent_count = clear_vectorstore(parent_vectorstore)
    deleted_vector_count = clear_vectorstore(vectorstore)
    deleted_parent_es_count = clear_es_index(index_name=ES_PARENT_INDEX_NAME)
    deleted_es_count = clear_es_index(index_name=ES_INDEX_NAME)
    ingest_result = ingest_docs()
    return {
        "message": "索引已重建完成。",
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
        "ingest_result": ingest_result,
    }


@app.get("/chunks/stats")
def get_chunk_stats():
    return get_chunk_statistics(vectorstore)


@app.get("/config")
def get_config():
    return get_runtime_config()


@app.get("/", include_in_schema=False)
def root_redirect():
    if DASHBOARD_INDEX.is_file():
        return RedirectResponse(url="/dashboard")
    return {"message": "RAG backend is running."}


@app.get("/dashboard", include_in_schema=False)
def get_dashboard_page():
    if not DASHBOARD_INDEX.is_file():
        return {"error": "dashboard not found: static/index.html"}
    return FileResponse(DASHBOARD_INDEX)


@app.get("/docs/content/{path:path}")
def get_document_content(path: str, username: str = ANONYMOUS_USERNAME):
    try:
        file_path = normalize_docs_relative_path(path)
    except ValueError as exc:
        return {"error": str(exc)}
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    if ENABLE_ACL:
        assert_document_access(file_path, username)

    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)

    return {
        "path": str(file_path.resolve().relative_to(DOCS_DIR.resolve())).replace("\\", "/"),
        "content": post.content,
    }


@app.get("/docs/list")
def list_documents():
    docs_dir = DOCS_DIR
    md_files = list_docs_markdown_files()
    return {
        "documents": [
            str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
            for file_path in md_files
        ]
    }


@app.get("/docs/module-directory")
def get_module_directory():
    try:
        return get_module_directory_response()
    except FileNotFoundError as exc:
        return {"error": str(exc)}


@app.get("/docs/grouped")
def list_grouped_documents_from_vectorstore():
    return {
        "modules": get_grouped_source_files_from_vectorstore(vectorstore)
    }


@app.post("/docs/delete")
def delete_document_chunks(req: SourceFileRequest):
    deleted_parent_count = delete_by_source_file(req.source_file, parent_vectorstore)
    deleted_vector_count = delete_by_source_file(req.source_file, vectorstore)
    deleted_parent_es_count = delete_by_source_file_in_es(req.source_file, index_name=ES_PARENT_INDEX_NAME)
    deleted_es_count = delete_by_source_file_in_es(req.source_file, index_name=ES_INDEX_NAME)
    return {
        "message": f"已删除 source_file={req.source_file} 的父子索引记录。",
        "source_file": req.source_file,
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
    }


@app.post("/docs/update")
def update_document_chunks(req: SourceFileRequest):
    docs_dir = DOCS_DIR
    file_path = normalize_docs_relative_path(req.source_file)

    deleted_parent_count = delete_by_source_file(req.source_file, parent_vectorstore)
    deleted_vector_count = delete_by_source_file(req.source_file, vectorstore)
    deleted_parent_es_count = delete_by_source_file_in_es(req.source_file, index_name=ES_PARENT_INDEX_NAME)
    deleted_es_count = delete_by_source_file_in_es(req.source_file, index_name=ES_INDEX_NAME)
    parent_doc, chunks = build_index_documents(file_path, docs_dir)
    parent_docs = [parent_doc] if parent_doc is not None else []
    parent_upsert_result = upsert_chunks(parent_docs, embedding_function, parent_vectorstore)
    upsert_result = upsert_chunks(chunks, embedding_function, vectorstore)
    parent_es_upsert_result = upsert_chunks_to_es(parent_docs, index_name=ES_PARENT_INDEX_NAME)
    child_es_upsert_result = upsert_chunks_to_es(chunks, index_name=ES_INDEX_NAME)
    es_sync_message = "" if parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"] else "，但未启用 ES BM25，同步已跳过"

    return {
        "message": f"已完成 source_file={req.source_file} 的重建更新{es_sync_message}。",
        "source_file": req.source_file,
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
        "parent_upserted_count": parent_upsert_result["chunk_count"],
        "parent_embed_seconds": parent_upsert_result["embed_seconds"],
        "parent_add_documents_seconds": parent_upsert_result["add_documents_seconds"],
        "upserted_count": upsert_result["chunk_count"],
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
        "es_enabled": parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"],
        "es_parent_index_name": parent_es_upsert_result["index_name"],
        "es_parent_indexed_count": parent_es_upsert_result["indexed_count"],
        "es_parent_sync_seconds": parent_es_upsert_result["sync_seconds"],
        "es_index_name": child_es_upsert_result["index_name"],
        "es_indexed_count": child_es_upsert_result["indexed_count"],
        "es_sync_seconds": child_es_upsert_result["sync_seconds"],
    }

@app.post("/retrieve")
def retrieve_context(req: QueryRequest):
    pipeline_result = run_retrieval_pipeline(
        query=req.query,
        username=req.username,
    )
    return pipeline_result["response"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)