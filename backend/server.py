import os
import sys
from pathlib import Path

import frontmatter
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from access_control import ANONYMOUS_USERNAME
from api_models import QueryRequest, SourceFileRequest
from documents_service import assert_document_access, get_module_directory_response, list_docs_markdown_files, normalize_docs_relative_path, split_single_markdown_file
from es_service import clear_es_index, delete_by_source_file_in_es, upsert_chunks_to_es
from rag_config import DASHBOARD_DIR, DASHBOARD_INDEX, DOCS_DIR, ENABLE_ACL, ENABLE_SUB_CHUNKING, get_runtime_config
from rag_store import embedding_function, vectorstore
from retrieval_pipeline_service import run_retrieval_pipeline
from vectorstore_service import clear_vectorstore, delete_by_source_file, get_chunk_statistics, get_grouped_source_files_from_vectorstore, upsert_chunks

app = FastAPI()

if DASHBOARD_DIR.exists():
    app.mount("/static-dashboard", StaticFiles(directory=str(DASHBOARD_DIR)), name="static-dashboard")


def _print_progress(current: int, total: int, prefix: str = "Progress", width: int = 30) -> None:
    if total <= 0:
        return

    filled = int(width * current / total)
    bar = "█" * filled + "-" * (width - filled)
    sys.stdout.write(f"{prefix} |{bar}| {current}/{total}\n")
    sys.stdout.flush()


def _finish_progress() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()

# ================= 3. 核心业务字典：意图路由配置 =================

# ================= 4. [入库 API] 带元数据解析的自定义切片 =================
@app.post("/ingest")
def ingest_docs():
    docs_dir = DOCS_DIR
    md_files = list_docs_markdown_files(include_module_directory=False)

    final_chunks = []
    total_files = len(md_files)
    
    for file_index, file_path in enumerate(md_files, start=1):
        print(f"[{file_index}/{total_files}] Processing file: {os.path.basename(file_path)}")
        file_chunks = split_single_markdown_file(file_path, docs_dir)
        final_chunks.extend(file_chunks)
        _print_progress(file_index, total_files, prefix="Ingest files")
        print(f"  -> {len(file_chunks)} chunks generated, {len(final_chunks)} total stored chunks so far")

    _finish_progress()

    # 存入数据库
    if not final_chunks:
        return {"message": f"未找到可入库的 Markdown 内容，共扫描 {len(md_files)} 个文件。"}
    upsert_result = upsert_chunks(final_chunks, embedding_function, vectorstore)
    es_upsert_result = upsert_chunks_to_es(final_chunks)
    return {
        "message": f"成功处理 {len(md_files)} 个文件，生成 {len(final_chunks)} 个带域标签的 Chunk，并已同步检索索引。",
        "sub_chunking_enabled": ENABLE_SUB_CHUNKING,
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
        "es_enabled": es_upsert_result["enabled"],
        "es_index_name": es_upsert_result["index_name"],
        "es_indexed_count": es_upsert_result["indexed_count"],
        "es_sync_seconds": es_upsert_result["sync_seconds"],
    }


@app.post("/clear")
def clear_index():
    deleted_vector_count = clear_vectorstore(vectorstore)
    deleted_es_count = clear_es_index()
    return {
        "message": f"已清空检索索引，向量库删除 {deleted_vector_count} 条，BM25 索引删除 {deleted_es_count} 条。",
        "deleted_count": deleted_vector_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_es_count": deleted_es_count,
    }


@app.post("/rebuild")
def rebuild_index():
    deleted_vector_count = clear_vectorstore(vectorstore)
    deleted_es_count = clear_es_index()
    ingest_result = ingest_docs()
    return {
        "message": "索引已重建完成。",
        "deleted_count": deleted_vector_count,
        "deleted_vector_count": deleted_vector_count,
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


@app.get("/docs/module-directory")
def get_module_directory():
    try:
        return get_module_directory_response()
    except FileNotFoundError as exc:
        return {"error": str(exc)}


@app.get("/docs/list")
def list_documents():
    docs_dir = DOCS_DIR
    md_files = list_docs_markdown_files(include_module_directory=False)
    return {
        "documents": [
            str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
            for file_path in md_files
        ]
    }


@app.get("/docs/grouped")
def list_grouped_documents_from_vectorstore():
    return {
        "modules": get_grouped_source_files_from_vectorstore(vectorstore)
    }


@app.post("/docs/delete")
def delete_document_chunks(req: SourceFileRequest):
    deleted_vector_count = delete_by_source_file(req.source_file, vectorstore)
    deleted_es_count = delete_by_source_file_in_es(req.source_file)
    return {
        "message": f"已删除 source_file={req.source_file} 的向量记录。",
        "source_file": req.source_file,
        "deleted_count": deleted_vector_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_es_count": deleted_es_count,
    }


@app.post("/docs/update")
def update_document_chunks(req: SourceFileRequest):
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    file_path = normalize_docs_relative_path(req.source_file)

    deleted_vector_count = delete_by_source_file(req.source_file, vectorstore)
    deleted_es_count = delete_by_source_file_in_es(req.source_file)
    chunks = split_single_markdown_file(file_path, docs_dir)
    upsert_result = upsert_chunks(chunks, embedding_function, vectorstore)
    es_upsert_result = upsert_chunks_to_es(chunks)

    return {
        "message": f"已完成 source_file={req.source_file} 的重建更新。",
        "source_file": req.source_file,
        "deleted_count": deleted_vector_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_es_count": deleted_es_count,
        "upserted_count": upsert_result["chunk_count"],
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
        "es_enabled": es_upsert_result["enabled"],
        "es_index_name": es_upsert_result["index_name"],
        "es_indexed_count": es_upsert_result["indexed_count"],
        "es_sync_seconds": es_upsert_result["sync_seconds"],
    }

@app.post("/retrieve")
def retrieve_context(req: QueryRequest):
    pipeline_result = run_retrieval_pipeline(
        query=req.query,
        username=req.username,
        domains=req.domains,
    )
    return pipeline_result["response"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)