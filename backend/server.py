import os
import sys
import platform
import time

if platform.system() == "Linux":
    try:
        __import__("pysqlite3")
        sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
    except ImportError:
        pass

from pathlib import Path

import yaml
import frontmatter
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from access_control import ANONYMOUS_USERNAME
from access_control import is_knowledge_base_accessible
from access_control import resolve_accessible_knowledge_bases
from access_control import resolve_accessible_query_scope
from api_models import QueryRequest, SourceFileRequest
from documents_service import assert_document_access, build_index_documents, list_docs_markdown_files, normalize_docs_relative_path
from knowledge_base_service import get_child_collection_name, get_child_es_index_name, get_knowledge_base_dir, get_module_directory_file, get_parent_collection_name, get_parent_es_index_name, list_knowledge_bases, normalize_knowledge_base_name
from rag_config import DASHBOARD_DIR, DASHBOARD_INDEX, DEFAULT_KNOWLEDGE_BASE, ENABLE_ACL, ENABLE_HTTPS, ENABLE_PARENT_CHILD_RETRIEVAL, ENABLE_SUB_CHUNKING, KNOWLEDGE_BASE_GROUP_MAPPING_FILE, REMOTE_DB_CERT, REMOTE_DB_TARGET, USERNAME_GROUP_MAPPING_FILE, get_runtime_config
from rag_store import get_parent_vectorstore, get_vectorstore
from remote_retrieval_service import get_remote_retrieval_service
from retrieval_strategy_config import get_routing_runtime_config, normalize_query_type
from vectorstore_service import clear_vectorstore, delete_by_source_file, get_chunk_statistics, get_grouped_source_files_from_vectorstore, upsert_chunks

app = FastAPI()

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


def _resolve_knowledge_base_runtime(knowledge_base: str | None) -> dict[str, object]:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    docs_dir = get_knowledge_base_dir(normalized_name)
    return {
        "knowledge_base": normalized_name,
        "docs_dir": docs_dir,
        "module_directory_file": get_module_directory_file(normalized_name),
        "vectorstore": get_vectorstore(normalized_name),
        "parent_vectorstore": get_parent_vectorstore(normalized_name),
        "es_index_name": get_child_es_index_name(normalized_name),
        "es_parent_index_name": get_parent_es_index_name(normalized_name),
    }


def _dependency_check(status: str, message: str, **details: object) -> dict[str, object]:
    return {
        "status": status,
        "message": message,
        **details,
    }


def _ok_or_error(ok: bool, ok_message: str, error_message: str, **details: object) -> dict[str, object]:
    return _dependency_check("ok" if ok else "error", ok_message if ok else error_message, **details)


def _combine_ready_status(checks: dict[str, dict[str, object]]) -> str:
    statuses = [str(check.get("status", "error")) for check in checks.values()]
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def _add_rpc_subcheck(
    checks: dict[str, dict[str, object]],
    name: str,
    rpc_checks: dict[str, object],
    rpc_name: str,
) -> None:
    value = rpc_checks.get(rpc_name)
    if isinstance(value, dict):
        checks[name] = dict(value)


def get_module_directory_response(knowledge_base: str | None = None) -> dict:
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    module_directory_file = runtime["module_directory_file"]
    if not isinstance(module_directory_file, Path) or not module_directory_file.is_file():
        raise FileNotFoundError(f"module directory file not found: {module_directory_file}")

    with open(module_directory_file, "r", encoding="utf-8") as file_handle:
        payload = yaml.safe_load(file_handle)

    response_payload = payload if isinstance(payload, dict) else {"modules": []}
    response_payload["knowledge_base"] = runtime["knowledge_base"]
    return response_payload

# ================= 3. 核心业务字典：意图路由配置 =================


@app.get("/knowledge-bases")
def list_available_knowledge_bases(username: str = ANONYMOUS_USERNAME):
    knowledge_bases = list_knowledge_bases()
    if ENABLE_ACL:
        accessible_knowledge_bases, _ = resolve_accessible_knowledge_bases(
            username,
            knowledge_bases,
            USERNAME_GROUP_MAPPING_FILE,
            KNOWLEDGE_BASE_GROUP_MAPPING_FILE,
        )
    else:
        accessible_knowledge_bases = knowledge_bases
    return {
        "default_knowledge_base": DEFAULT_KNOWLEDGE_BASE,
        "knowledge_bases": knowledge_bases,
        "accessible_knowledge_bases": accessible_knowledge_bases,
        "username": username,
    }

# ================= 4. [入库 API] 带元数据解析的自定义切片 =================
@app.post("/ingest")
def ingest_docs(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    normalized_name = str(runtime["knowledge_base"])
    docs_dir = runtime["docs_dir"]
    parent_vectorstore = runtime["parent_vectorstore"]
    vectorstore = runtime["vectorstore"]
    es_parent_index_name = str(runtime["es_parent_index_name"])
    es_index_name = str(runtime["es_index_name"])
    md_files = list_docs_markdown_files(docs_dir)

    parent_docs = []
    final_chunks = []
    total_files = len(md_files)
    remote_service = get_remote_retrieval_service()
    
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
        return {
            "knowledge_base": normalized_name,
            "message": f"未找到可入库的 Markdown 内容，共扫描 {len(md_files)} 个文件。",
        }
    parent_upsert_result = upsert_chunks(parent_docs, parent_vectorstore)
    upsert_result = upsert_chunks(final_chunks, vectorstore)
    parent_es_upsert_result = remote_service.upsert_chunks_to_es(parent_docs, index_name=es_parent_index_name)
    child_es_upsert_result = remote_service.upsert_chunks_to_es(final_chunks, index_name=es_index_name)
    es_sync_message = "并已同步检索索引。" if parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"] else "，但未启用 ES BM25，同步已跳过。"
    return {
        "knowledge_base": normalized_name,
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
def clear_index(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    normalized_name = str(runtime["knowledge_base"])
    parent_vectorstore = runtime["parent_vectorstore"]
    vectorstore = runtime["vectorstore"]
    es_parent_index_name = str(runtime["es_parent_index_name"])
    es_index_name = str(runtime["es_index_name"])
    deleted_parent_count = clear_vectorstore(parent_vectorstore)
    deleted_vector_count = clear_vectorstore(vectorstore)
    remote_service = get_remote_retrieval_service()
    deleted_parent_es_count = remote_service.clear_es_index(index_name=es_parent_index_name, recreate=True)
    deleted_es_count = remote_service.clear_es_index(index_name=es_index_name, recreate=True)
    return {
        "knowledge_base": normalized_name,
        "message": f"已清空检索索引，父文档索引删除 {deleted_parent_count} 条，子切片向量库删除 {deleted_vector_count} 条，父 BM25 索引删除 {deleted_parent_es_count} 条，子 BM25 索引删除 {deleted_es_count} 条。",
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
    }


@app.post("/rebuild")
def rebuild_index(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    normalized_name = str(runtime["knowledge_base"])
    parent_vectorstore = runtime["parent_vectorstore"]
    vectorstore = runtime["vectorstore"]
    es_parent_index_name = str(runtime["es_parent_index_name"])
    es_index_name = str(runtime["es_index_name"])
    deleted_parent_count = clear_vectorstore(parent_vectorstore)
    deleted_vector_count = clear_vectorstore(vectorstore)
    remote_service = get_remote_retrieval_service()
    deleted_parent_es_count = remote_service.clear_es_index(index_name=es_parent_index_name, recreate=True)
    deleted_es_count = remote_service.clear_es_index(index_name=es_index_name, recreate=True)
    ingest_result = ingest_docs(knowledge_base=normalized_name)
    return {
        "knowledge_base": normalized_name,
        "message": "索引已重建完成。",
        "deleted_count": deleted_vector_count,
        "deleted_parent_count": deleted_parent_count,
        "deleted_vector_count": deleted_vector_count,
        "deleted_parent_es_count": deleted_parent_es_count,
        "deleted_es_count": deleted_es_count,
        "ingest_result": ingest_result,
    }


@app.get("/chunks/stats")
def get_chunk_stats(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    payload = get_chunk_statistics(runtime["vectorstore"])
    payload["knowledge_base"] = runtime["knowledge_base"]
    return payload


@app.get("/config")
def get_config(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    runtime_config = get_runtime_config()
    runtime_config["routing"] = get_routing_runtime_config()
    runtime_config["knowledge_base"] = {
        "selected": runtime["knowledge_base"],
        "default": DEFAULT_KNOWLEDGE_BASE,
        "available": list_knowledge_bases(),
        "docs_dir": str(runtime["docs_dir"]),
        "module_directory_file": str(runtime["module_directory_file"]),
    }
    runtime_config["indexing"]["child_es_index_name"] = runtime["es_index_name"]
    runtime_config["indexing"]["parent_es_index_name"] = runtime["es_parent_index_name"]
    runtime_config["indexing"]["es_runtime"] = get_remote_retrieval_service().get_es_runtime_config([
        str(runtime["es_index_name"]),
        str(runtime["es_parent_index_name"]),
    ])
    return runtime_config


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "rag-backend",
        "message": "backend process is alive",
    }


@app.get("/ready")
def ready_check(response: Response, knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    started = time.perf_counter()
    checks: dict[str, dict[str, object]] = {}

    try:
        runtime = _resolve_knowledge_base_runtime(knowledge_base)
        normalized_name = str(runtime["knowledge_base"])
        docs_dir = runtime["docs_dir"]
        module_directory_file = runtime["module_directory_file"]
        child_collection_name = get_child_collection_name(normalized_name)
        parent_collection_name = get_parent_collection_name(normalized_name)
        child_es_index_name = str(runtime["es_index_name"])
        parent_es_index_name = str(runtime["es_parent_index_name"])
        checks["knowledge_base"] = _dependency_check(
            "ok",
            "knowledge base resolved",
            selected=normalized_name,
            default=DEFAULT_KNOWLEDGE_BASE,
        )
        checks["docs_dir"] = _ok_or_error(
            isinstance(docs_dir, Path) and docs_dir.is_dir(),
            "docs directory exists",
            "docs directory is missing",
            path=str(docs_dir),
        )
        checks["module_directory"] = _ok_or_error(
            isinstance(module_directory_file, Path) and module_directory_file.is_file(),
            "module directory exists",
            "module directory is missing",
            path=str(module_directory_file),
        )
    except Exception as exc:
        normalized_name = str(knowledge_base or DEFAULT_KNOWLEDGE_BASE)
        child_collection_name = ""
        parent_collection_name = ""
        child_es_index_name = ""
        parent_es_index_name = ""
        checks["knowledge_base"] = _dependency_check(
            "error",
            "failed to resolve knowledge base",
            selected=normalized_name,
            error=str(exc),
        )

    remote_cert_path = Path(str(REMOTE_DB_CERT)).expanduser()
    checks["remote_rpc_certificate"] = _ok_or_error(
        remote_cert_path.is_file(),
        "remote RPC client certificate exists",
        "remote RPC client certificate is missing",
        path=str(remote_cert_path),
        target=REMOTE_DB_TARGET,
    )

    if child_collection_name and parent_collection_name:
        try:
            rpc_health = get_remote_retrieval_service().health_check(
                collection_names=[child_collection_name, parent_collection_name],
                index_names=[child_es_index_name, parent_es_index_name],
                include_es=True,
            )
            rpc_ready = bool(rpc_health.get("ready"))
            rpc_status = str(rpc_health.get("status") or ("ok" if rpc_ready else "error"))
            checks["grpc"] = _dependency_check(
                rpc_status if rpc_ready else "error",
                "remote RPC health check completed" if rpc_ready else "remote RPC is not ready",
                target=REMOTE_DB_TARGET,
                rpc_status=rpc_status,
            )
            rpc_checks = rpc_health.get("checks", {})
            if isinstance(rpc_checks, dict):
                _add_rpc_subcheck(checks, "rpc_server_certificates", rpc_checks, "certificates")
                _add_rpc_subcheck(checks, "embedding_model", rpc_checks, "embedding_model")
                _add_rpc_subcheck(checks, "reranker", rpc_checks, "reranker")
                _add_rpc_subcheck(checks, "collections", rpc_checks, "collections")
                _add_rpc_subcheck(checks, "elasticsearch", rpc_checks, "elasticsearch")
        except Exception as exc:
            checks["grpc"] = _dependency_check(
                "error",
                "remote RPC health check failed",
                target=REMOTE_DB_TARGET,
                error=str(exc),
            )

    status = _combine_ready_status(checks)
    ready = status != "error"
    if not ready:
        response.status_code = 503

    return {
        "ready": ready,
        "status": status,
        "service": "rag-backend",
        "knowledge_base": normalized_name,
        "checks": checks,
        "checked_in_seconds": round(time.perf_counter() - started, 4),
    }


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
def get_document_content(
    path: str,
    knowledge_base: str = DEFAULT_KNOWLEDGE_BASE,
    username: str = Header(default=ANONYMOUS_USERNAME, alias="X-Username")
):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    docs_dir = runtime["docs_dir"]
    try:
        file_path = normalize_docs_relative_path(path, docs_dir)
    except ValueError as exc:
        return {"error": str(exc)}
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    if ENABLE_ACL:
        assert_document_access(file_path, username, docs_dir)

    with open(file_path, "r", encoding="utf-8") as f:
        post = frontmatter.load(f)

    return {
        "knowledge_base": runtime["knowledge_base"],
        "path": str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/"),
        "content": post.content,
    }


@app.get("/docs/list")
def list_documents(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    docs_dir = runtime["docs_dir"]
    md_files = list_docs_markdown_files(docs_dir)
    return {
        "knowledge_base": runtime["knowledge_base"],
        "documents": [
            str(file_path.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
            for file_path in md_files
        ]
    }


@app.get("/docs/module-directory")
def get_module_directory(
    knowledge_base: str = DEFAULT_KNOWLEDGE_BASE,
    username: str | None = Header(default=None, alias="X-Username")
):
    normalized_username = username.strip() if isinstance(username, str) else ''
    if ENABLE_ACL and not normalized_username:
        raise HTTPException(status_code=401, detail={
            "status": "unauthorized",
            "message": "缺少 X-Username 请求头，无法获取模块目录。"
        })

    if ENABLE_ACL and not is_knowledge_base_accessible(
        normalized_username,
        knowledge_base,
        USERNAME_GROUP_MAPPING_FILE,
        KNOWLEDGE_BASE_GROUP_MAPPING_FILE,
    ):
        raise HTTPException(status_code=403, detail={
            "status": "forbidden",
            "message": f"用户 {normalized_username} 无权访问知识库 {normalize_knowledge_base_name(knowledge_base)} 的模块目录。",
            "username": normalized_username,
            "knowledge_base": normalize_knowledge_base_name(knowledge_base),
        })

    try:
        return get_module_directory_response(knowledge_base)
    except FileNotFoundError as exc:
        return {"error": str(exc)}


@app.get("/docs/grouped")
def list_grouped_documents_from_vectorstore(knowledge_base: str = DEFAULT_KNOWLEDGE_BASE):
    runtime = _resolve_knowledge_base_runtime(knowledge_base)
    return {
        "knowledge_base": runtime["knowledge_base"],
        "modules": get_grouped_source_files_from_vectorstore(runtime["vectorstore"])
    }


@app.post("/docs/delete")
def delete_document_chunks(req: SourceFileRequest):
    runtime = _resolve_knowledge_base_runtime(req.knowledge_base)
    remote_service = get_remote_retrieval_service()
    deleted_parent_count = delete_by_source_file(req.source_file, runtime["parent_vectorstore"])
    deleted_vector_count = delete_by_source_file(req.source_file, runtime["vectorstore"])
    deleted_parent_es_count = remote_service.delete_by_source_file_in_es(req.source_file, index_name=str(runtime["es_parent_index_name"]))
    deleted_es_count = remote_service.delete_by_source_file_in_es(req.source_file, index_name=str(runtime["es_index_name"]))
    return {
        "knowledge_base": runtime["knowledge_base"],
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
    runtime = _resolve_knowledge_base_runtime(req.knowledge_base)
    docs_dir = runtime["docs_dir"]
    file_path = normalize_docs_relative_path(req.source_file, docs_dir)
    parent_vectorstore = runtime["parent_vectorstore"]
    vectorstore = runtime["vectorstore"]
    es_parent_index_name = str(runtime["es_parent_index_name"])
    es_index_name = str(runtime["es_index_name"])
    remote_service = get_remote_retrieval_service()

    deleted_parent_count = delete_by_source_file(req.source_file, parent_vectorstore)
    deleted_vector_count = delete_by_source_file(req.source_file, vectorstore)
    deleted_parent_es_count = remote_service.delete_by_source_file_in_es(req.source_file, index_name=es_parent_index_name)
    deleted_es_count = remote_service.delete_by_source_file_in_es(req.source_file, index_name=es_index_name)
    parent_doc, chunks = build_index_documents(file_path, docs_dir)
    parent_docs = [parent_doc] if parent_doc is not None else []
    parent_upsert_result = upsert_chunks(parent_docs, parent_vectorstore)
    upsert_result = upsert_chunks(chunks, vectorstore)
    parent_es_upsert_result = remote_service.upsert_chunks_to_es(parent_docs, index_name=es_parent_index_name)
    child_es_upsert_result = remote_service.upsert_chunks_to_es(chunks, index_name=es_index_name)
    es_sync_message = "" if parent_es_upsert_result["enabled"] and child_es_upsert_result["enabled"] else "，但未启用 ES BM25，同步已跳过"

    return {
        "knowledge_base": runtime["knowledge_base"],
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
    try:
        normalize_query_type(req.query_type)
        runtime = _resolve_knowledge_base_runtime(req.knowledge_base)
        normalized_knowledge_base = str(runtime["knowledge_base"])
        if ENABLE_ACL and not is_knowledge_base_accessible(
            req.username,
            normalized_knowledge_base,
            USERNAME_GROUP_MAPPING_FILE,
            KNOWLEDGE_BASE_GROUP_MAPPING_FILE,
        ):
            return {
                "status": "forbidden",
                "message": f"当前账号没有访问知识库 {normalized_knowledge_base} 的权限。",
                "knowledge_base": normalized_knowledge_base,
            }
        if ENABLE_ACL:
            authorized_domains, authorized_source_files = resolve_accessible_query_scope(
                req.username,
                req.domains,
                req.source_files,
                runtime["docs_dir"],
                USERNAME_GROUP_MAPPING_FILE,
            )
        else:
            authorized_domains = list(req.domains or [])
            authorized_source_files = list(req.source_files or [])
        pipeline_result = get_remote_retrieval_service().run_retrieval_pipeline(
            query=req.query,
            username=req.username,
            domains=authorized_domains,
            source_files=authorized_source_files,
            query_type=req.query_type,
            top_k=req.top_k,
            knowledge_base=req.knowledge_base,
            debug=req.debug,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return pipeline_result["response"]

if __name__ == "__main__":
    import uvicorn
    
    server_kwargs = {
        "app": app,
        "host": "0.0.0.0",
        "port": 8000,
    }

    if ENABLE_HTTPS:
        from rag_config import SERVER_CRT, SERVER_KEY
        server_kwargs["ssl_certfile"] = str(SERVER_CRT)
        server_kwargs["ssl_keyfile"] = str(SERVER_KEY)
        print("Starting server with HTTPS enabled.")
    else:
        print("Starting server with HTTPS disabled (HTTP).")

    uvicorn.run(**server_kwargs)