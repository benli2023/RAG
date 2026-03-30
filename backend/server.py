import os
import sys
from pathlib import Path

import frontmatter
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from access_control import ANONYMOUS_USERNAME, build_source_file_filter, dedupe_values, normalize_username, resolve_accessible_sources
from api_models import QueryRequest, SourceFileRequest
from documents_service import assert_document_access, get_module_directory_response, list_docs_markdown_files, normalize_docs_relative_path, split_single_markdown_file
from faq_support import rank_results_for_generation
from rag_config import DASHBOARD_DIR, DASHBOARD_INDEX, DOCS_DIR, ENABLE_ACL, ENABLE_SUB_CHUNKING, FINAL_CONTEXT_K, RERANK_CANDIDATE_K, RERANKER_ENABLED, USERNAME_GROUP_MAPPING_FILE, get_runtime_config
from rag_store import embedding_function, vectorstore
from reranker_service import rerank_documents
from retrieval_response_formatter import build_context_entry, build_retrieval_response
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
    return {
        "message": f"成功处理 {len(md_files)} 个文件，生成 {len(final_chunks)} 个带域标签的 Chunk！",
        "sub_chunking_enabled": ENABLE_SUB_CHUNKING,
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
    }


@app.post("/clear")
def clear_index():
    deleted_count = clear_vectorstore(vectorstore)
    return {"message": f"已清空向量库，共删除 {deleted_count} 条记录。", "deleted_count": deleted_count}


@app.post("/rebuild")
def rebuild_index():
    deleted_count = clear_vectorstore(vectorstore)
    ingest_result = ingest_docs()
    return {
        "message": "索引已重建完成。",
        "deleted_count": deleted_count,
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
    deleted_count = delete_by_source_file(req.source_file, vectorstore)
    return {
        "message": f"已删除 source_file={req.source_file} 的向量记录。",
        "source_file": req.source_file,
        "deleted_count": deleted_count,
    }


@app.post("/docs/update")
def update_document_chunks(req: SourceFileRequest):
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    file_path = normalize_docs_relative_path(req.source_file)

    deleted_count = delete_by_source_file(req.source_file, vectorstore)
    chunks = split_single_markdown_file(file_path, docs_dir)
    upsert_result = upsert_chunks(chunks, embedding_function, vectorstore)

    return {
        "message": f"已完成 source_file={req.source_file} 的重建更新。",
        "source_file": req.source_file,
        "deleted_count": deleted_count,
        "upserted_count": upsert_result["chunk_count"],
        "embed_seconds": upsert_result["embed_seconds"],
        "add_documents_seconds": upsert_result["add_documents_seconds"],
    }

@app.post("/retrieve")
def retrieve_context(req: QueryRequest):
    query = req.query
    username = normalize_username(req.username)
    requested_domains = dedupe_values(req.domains)
    authorized_source_files, restricted_source_files, authorized_domains = resolve_accessible_sources(
        username,
        requested_domains,
        DOCS_DIR,
        USERNAME_GROUP_MAPPING_FILE,
    )
    has_candidate_documents = bool(authorized_source_files or restricted_source_files)
    filter_value = None

    if ENABLE_ACL and restricted_source_files and not authorized_source_files:
        denied_domains = sorted({Path(source_file).parts[0] for source_file in restricted_source_files if Path(source_file).parts})
        print(f"⛔ ACL deny user={username}, denied_files={restricted_source_files}, requested_domains={requested_domains or '[global]'}")
        return {
            "status": "forbidden",
            "message": f"用户 {username} 无权访问当前命中的知识文档。",
            "username": username,
            "denied_domains": denied_domains,
            "denied_source_files": restricted_source_files,
        }
    
    # 根据前端传入的 domains 和文档 ACL 生成 source_file 硬过滤
    if authorized_source_files:
        filter_value = build_source_file_filter(authorized_source_files)
        if requested_domains:
            if ENABLE_ACL:
                print(f"🎯 接收到前端的大模型路由，锁定模块: {requested_domains}，ACL 收敛后文档数={len(authorized_source_files)}，user={username}")
            else:
                print(f"🎯 接收到前端的大模型路由，锁定模块: {requested_domains}，可检索文档数={len(authorized_source_files)}，user={username}")
        else:
            if ENABLE_ACL:
                print(f"🔐 全局检索已按文档 ACL 收敛，允许文档数={len(authorized_source_files)}，user={username}")
            else:
                print(f"🌐 全局检索启用，当前可检索文档数={len(authorized_source_files)}，user={username}")
    elif requested_domains:
        print(f"🎯 接收到前端的大模型路由，但模块 {requested_domains} 下没有可检索文档，user={username}")
    else:
        print("🌐 接收到空路由，执行全局检索")

    if requested_domains and not has_candidate_documents:
        return {
            "context": [],
            "routed_domains": requested_domains,
            "authorized_domains": [],
            "authorized_source_files": [],
            "username": username,
        }

    if ENABLE_ACL and requested_domains and not authorized_source_files and restricted_source_files:
        denied_domains = sorted({Path(source_file).parts[0] for source_file in restricted_source_files if Path(source_file).parts})
        return {
            "status": "forbidden",
            "message": f"用户 {username} 无权访问当前命中的知识文档。",
            "username": username,
            "denied_domains": denied_domains,
            "denied_source_files": restricted_source_files,
        }

    if ENABLE_ACL and not requested_domains and not authorized_source_files and restricted_source_files:
        print(f"⛔ ACL deny user={username}, no accessible documents for global retrieval")
        if filter_value is None:
            return {
                "status": "forbidden",
                "message": f"用户 {username} 当前没有任何可检索的知识文档权限。",
                "username": username,
                "denied_domains": sorted({Path(source_file).parts[0] for source_file in restricted_source_files if Path(source_file).parts}),
                "denied_source_files": restricted_source_files,
            }

    # 第一阶段：放大召回，给第二阶段 Reranker 留出候选空间
    print(
        f"INFO: 执行 BGE-M3 向量检索，candidate_k={RERANK_CANDIDATE_K}, "
        f"reranker_enabled={RERANKER_ENABLED}, filter={filter_value}"
    )
    if filter_value is not None:
        results = vectorstore.similarity_search(query, k=RERANK_CANDIDATE_K, filter=filter_value)
    else:
        results = vectorstore.similarity_search(query, k=RERANK_CANDIDATE_K)

    # 第二阶段：使用 CrossEncoder/BGE-Reranker 精排
    results = rerank_documents(query, results)
    results = rank_results_for_generation(query, results)
    
    # 拼装带有严密上下文的 Prompt
    context = []
    for res in results:
        # stop once we've collected the final number of context pieces
        if len(context) >= FINAL_CONTEXT_K:
            break

        context.append(build_context_entry(len(context), res.metadata, res.page_content))

    return build_retrieval_response(
        context=context,
        requested_domains=requested_domains,
        authorized_domains=authorized_domains,
        authorized_source_files=authorized_source_files,
        username=username,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)