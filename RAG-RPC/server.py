import sys
import os
import signal
import platform
import builtins

if platform.system() == "Linux":
    try:
        __import__("pysqlite3")
        sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
    except ImportError:
        pass

from pathlib import Path
import json
from concurrent import futures
import time
import threading

import grpc
from langchain_chroma import Chroma
from langchain_core.documents import Document

import es_service
import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc
from local_vector_database import LocalVectorDatabase
from rag_config import DB_DIR, EMBEDDING_DEVICE, EMBEDDING_MODEL_NAME, LOCAL_MODEL_PATH, PRINT_LOGGING_ENABLED, RPC_CRT, RPC_HTTP2_MAX_PINGS_WITHOUT_DATA, RPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS, RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS, RPC_KEEPALIVE_TIME_MS, RPC_KEEPALIVE_TIMEOUT_MS, RPC_KEY
from rag_store import get_embedding_function
from reranker_service import get_reranker_runtime_status
from retrieval_pipeline_service import run_retrieval_pipeline

RAG_RPC_DIR = Path(__file__).resolve().parent

# Maximum recursion depth for _json_safe_value to guard against deeply nested structures
_JSON_MAX_DEPTH = 20


if not PRINT_LOGGING_ENABLED:
    def _disabled_print(*args, **kwargs):
        return None

    builtins.print = _disabled_print


def _log_print(*args, **kwargs):
    if PRINT_LOGGING_ENABLED:
        print(*args, **kwargs)


def _json_safe_value(value, _depth: int = 0):
    if _depth > _JSON_MAX_DEPTH:
        return str(value)

    if isinstance(value, Document):
        return {
            "page_content": value.page_content,
            "metadata": value.metadata,
        }

    if isinstance(value, dict):
        return {key: _json_safe_value(item, _depth + 1) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item, _depth + 1) for item in value]

    return value


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default


def _looks_like_path(value: str) -> bool:
    return Path(value).is_absolute() or value.startswith(("./", "../", "~")) or "\\" in value


def _model_path_status(model_name: str, configured_path: Path | None = None) -> dict[str, object]:
    path = configured_path if configured_path is not None else Path(model_name).expanduser()
    path_like = _looks_like_path(model_name) or configured_path is not None
    path_exists = path.exists() if path_like else None
    missing_required_path = path_like and not path_exists
    return {
        "status": "error" if missing_required_path else "ok",
        "state": "missing" if missing_required_path else "available" if path_like else "external_model_name",
        "model_name": model_name,
        "path": str(path) if path_like else None,
        "path_like": path_like,
        "path_exists": path_exists,
    }


def _file_status(path: Path, label: str) -> dict[str, object]:
    exists = path.is_file()
    return {
        "status": "ok" if exists else "error",
        "label": label,
        "path": str(path),
        "exists": exists,
    }


def _directory_status(path: Path, label: str) -> dict[str, object]:
    exists = path.is_dir()
    writable = os.access(path, os.W_OK) if exists else False
    return {
        "status": "ok" if exists and writable else "error",
        "label": label,
        "path": str(path),
        "exists": exists,
        "writable": writable,
    }


def _certificate_status() -> dict[str, object]:
    key_status = _file_status(RPC_KEY, "rpc_private_key")
    crt_status = _file_status(RPC_CRT, "rpc_certificate")
    status = "ok" if key_status["status"] == "ok" and crt_status["status"] == "ok" else "error"
    return {
        "status": status,
        "rpc_key": key_status,
        "rpc_crt": crt_status,
    }


def _embedding_status(multi_db=None) -> dict[str, object]:
    model_status = _model_path_status(EMBEDDING_MODEL_NAME, LOCAL_MODEL_PATH if _looks_like_path(EMBEDDING_MODEL_NAME) else None)
    loaded = bool(getattr(multi_db, "embedding_function", None)) if multi_db is not None else False
    status = model_status["status"]
    if multi_db is not None and not loaded:
        status = "error"
    return {
        **model_status,
        "status": status,
        "loaded": loaded,
        "device": EMBEDDING_DEVICE,
    }


def _collection_status(multi_db, collection_names: list[str]) -> dict[str, object]:
    names = collection_names or [COLLECTION_NAME]
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    for collection_name in names:
        try:
            counts[collection_name] = int(multi_db.get_db(collection_name).count())
        except Exception as exc:
            errors[collection_name] = str(exc)

    if errors:
        status = "error"
    elif any(count == 0 for count in counts.values()):
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "counts": counts,
        "errors": errors,
    }


def _es_status(index_names: list[str]) -> dict[str, object]:
    try:
        runtime = es_service.get_es_runtime_config(index_names or None)
    except Exception as exc:
        return {
            "status": "error",
            "available": False,
            "error": str(exc),
        }

    document_counts = runtime.get("document_counts_by_name", {}) if isinstance(runtime, dict) else {}
    analyzer_blocking_reason = runtime.get("analyzer_blocking_reason") if isinstance(runtime, dict) else None
    if es_service.ES_ENABLED and (not runtime.get("available") or analyzer_blocking_reason):
        status = "error"
    elif es_service.ES_ENABLED and isinstance(document_counts, dict) and any(count == 0 for count in document_counts.values()):
        status = "warning"
    elif not es_service.ES_ENABLED:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "runtime": runtime,
    }


def _combine_status(checks: dict[str, dict[str, object]]) -> str:
    statuses = [str(check.get("status", "error")) for check in checks.values()]
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def _build_health_details(
    multi_db,
    *,
    collection_names: list[str] | None = None,
    index_names: list[str] | None = None,
    include_es: bool = False,
) -> dict[str, object]:
    checks: dict[str, dict[str, object]] = {
        "db_dir": _directory_status(Path(DB_DIR), "chroma_db_dir"),
        "certificates": _certificate_status(),
        "embedding_model": _embedding_status(multi_db),
        "reranker": get_reranker_runtime_status(),
        "collections": _collection_status(multi_db, list(collection_names or [])),
    }

    if include_es:
        checks["elasticsearch"] = _es_status(list(index_names or []))

    status = _combine_status(checks)
    return {
        "service": "rag-rpc",
        "status": status,
        "ready": status != "error",
        "port": PORT,
        "default_collection": COLLECTION_NAME,
        "checks": checks,
    }


def _assert_startup_dependencies() -> None:
    Path(DB_DIR).mkdir(parents=True, exist_ok=True)
    checks = {
        "db_dir": _directory_status(Path(DB_DIR), "chroma_db_dir"),
        "certificates": _certificate_status(),
        "embedding_model": _embedding_status(None),
        "reranker": get_reranker_runtime_status(),
    }
    status = _combine_status(checks)
    for name, check in checks.items():
        if check.get("status") == "warning":
            _log_print(f"[startup] warning: {name}: {json.dumps(check, ensure_ascii=False)}")
    if status == "error":
        _log_print("[startup] dependency check failed:")
        for name, check in checks.items():
            if check.get("status") == "error":
                _log_print(f"  - {name}: {json.dumps(check, ensure_ascii=False)}")
        sys.exit(1)


# ----------------- RPC Configuration -----------------
PORT = os.getenv("RPC_PORT", "50051")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "rag_collection_default")
MAX_WORKERS = _get_int_env("RPC_MAX_WORKERS", 10)


class MultiCollectionVectorDatabase:
    def __init__(self, db_dir: str, embedding_function):
        self.db_dir = db_dir
        self.embedding_function = embedding_function
        self.collections = {}
        self._collections_lock = threading.Lock()

    def get_db(self, collection_name: str) -> LocalVectorDatabase:
        if not collection_name:
            collection_name = COLLECTION_NAME
        if collection_name not in self.collections:
            with self._collections_lock:
                if collection_name not in self.collections:
                    chroma_store = Chroma(
                        collection_name=collection_name,
                        embedding_function=self.embedding_function,
                        persist_directory=self.db_dir,
                    )
                    self.collections[collection_name] = LocalVectorDatabase(chroma_store)
        return self.collections[collection_name]


class VectorDatabaseServicer(pb2_grpc.VectorDatabaseServiceServicer):
    def __init__(self, multi_db: MultiCollectionVectorDatabase):
        self.multi_db = multi_db

    def _log_request(self, context, method_name, collection_name):
        peer = context.peer()
        _log_print(f"[gRPC] {method_name} | Collection: {collection_name} | Peer: {peer}")

    def SimilaritySearch(self, request, context):
        self._log_request(context, "SimilaritySearch", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        filter_dict = json.loads(request.filter_json) if request.filter_json else None
        k = request.k if request.k > 0 else 4
        docs = db.similarity_search_with_score(request.query, k=k, filter=filter_dict)

        response = pb2.SimilaritySearchResponse()
        for d in docs:
            metadata_json = json.dumps(d.metadata) if d.metadata else "{}"
            response.documents.append(
                pb2.Document(
                    page_content=d.page_content,
                    metadata_json=metadata_json,
                )
            )
        return response

    def Upsert(self, request, context):
        self._log_request(context, "Upsert", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        metadatas = [json.loads(mj) for mj in request.metadatas_json]
        embeddings = [list(e.values) for e in request.embeddings]

        if not embeddings and getattr(self.multi_db, "embedding_function", None) and request.documents:
            docs = list(request.documents)
            _log_print(f"[gRPC] Calculating embeddings for {len(docs)} documents...")
            start_time = time.perf_counter()
            embeddings = self.multi_db.embedding_function.embed_documents(docs)
            total_elapsed = time.perf_counter() - start_time
            _log_print(f"[gRPC] Finished embedding {len(docs)} chunks in {total_elapsed:.2f}s")

        db.upsert(
            ids=list(request.ids),
            documents=list(request.documents),
            metadatas=metadatas,
            embeddings=embeddings,
        )
        return pb2.UpsertResponse(success=True)

    def DeleteBySourceFile(self, request, context):
        self._log_request(context, "DeleteBySourceFile", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        deleted = db.delete_by_source_file(request.source_file)
        return pb2.DeleteResponse(deleted_count=deleted)

    def Clear(self, request, context):
        self._log_request(context, "Clear", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        deleted = db.clear()
        return pb2.ClearResponse(deleted_count=deleted)

    def Count(self, request, context):
        self._log_request(context, "Count", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        count = db.count()
        return pb2.CountResponse(count=count)

    def GetRecords(self, request, context):
        self._log_request(context, "GetRecords", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        where_dict = json.loads(request.where_json) if request.where_json else None
        include_list = list(request.include)
        limit = int(request.limit) if request.limit > 0 else None
        offset = max(0, int(request.offset))
        records = db.get_records(include=include_list, where=where_dict, limit=limit, offset=offset)

        ids = records.get("ids", [])
        documents = records.get("documents", [])
        metadatas = records.get("metadatas", [])
        metadatas_json = [json.dumps(m) for m in metadatas] if metadatas else []
        total_count = records.get("total_count")

        return pb2.GetRecordsResponse(
            ids=ids,
            documents=documents,
            metadatas_json=metadatas_json,
            total_count=int(total_count) if total_count is not None else -1,
            next_offset=int(records.get("next_offset", offset + len(ids))),
            has_more=bool(records.get("has_more", False)),
        )

    def Retrieve(self, request, context):
        peer = context.peer()
        _log_print(
            f"[gRPC] Retrieve | KnowledgeBase: {request.knowledge_base or 'default'} "
            f"| User: {request.username or 'anonymous'} | Peer: {peer}"
        )

        try:
            result = run_retrieval_pipeline(
                query=request.query,
                username=request.username,
                domains=list(request.domains),
                source_files=list(request.source_files),
                query_type=request.query_type or None,
                top_k=request.top_k if request.top_k > 0 else None,
                knowledge_base=request.knowledge_base or None,
                debug=request.debug,
            )
        except (ValueError, FileNotFoundError) as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except Exception as exc:
            context.abort(grpc.StatusCode.INTERNAL, str(exc))

        return pb2.RetrieveResponse(
            status=result.get("status", "success"),
            response_json=json.dumps(_json_safe_value(result), ensure_ascii=False),
        )

    def UpsertEsChunks(self, request, context):
        self._log_request(context, "UpsertEsChunks", request.index_name)
        chunks: list[Document] = []

        for chunk in request.chunks:
            try:
                metadata = json.loads(chunk.metadata_json) if chunk.metadata_json else {}
            except json.JSONDecodeError:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "chunk metadata_json must be valid JSON")

            if not isinstance(metadata, dict):
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "chunk metadata_json must decode to an object")

            chunks.append(Document(page_content=chunk.page_content, metadata=metadata))

        result = es_service.upsert_chunks_to_es(chunks, index_name=request.index_name or es_service.ES_INDEX_NAME)
        return pb2.UpsertEsChunksResponse(
            enabled=result["enabled"],
            index_name=result["index_name"],
            indexed_count=result["indexed_count"],
            sync_seconds=result["sync_seconds"],
        )

    def DeleteBySourceFileInEs(self, request, context):
        self._log_request(context, "DeleteBySourceFileInEs", request.index_name)
        deleted = es_service.delete_by_source_file_in_es(
            request.source_file,
            index_name=request.index_name or es_service.ES_INDEX_NAME,
        )
        return pb2.DeleteResponse(deleted_count=deleted)

    def ClearEsIndex(self, request, context):
        self._log_request(context, "ClearEsIndex", request.index_name)
        deleted = es_service.clear_es_index(
            index_name=request.index_name or es_service.ES_INDEX_NAME,
            recreate=request.recreate,
        )
        return pb2.ClearResponse(deleted_count=deleted)

    def GetEsRuntimeConfig(self, request, context):
        self._log_request(context, "GetEsRuntimeConfig", ",".join(request.index_names))
        runtime = es_service.get_es_runtime_config(list(request.index_names) or None)
        return pb2.GetEsRuntimeConfigResponse(
            runtime_json=json.dumps(runtime, ensure_ascii=False),
        )

    def HealthCheck(self, request, context):
        self._log_request(context, "HealthCheck", ",".join(request.collection_names))
        details = _build_health_details(
            self.multi_db,
            collection_names=list(request.collection_names),
            index_names=list(request.index_names),
            include_es=bool(request.include_es),
        )
        return pb2.HealthCheckResponse(
            ready=bool(details["ready"]),
            status=str(details["status"]),
            details_json=json.dumps(details, ensure_ascii=False),
        )


def serve():
    _assert_startup_dependencies()
    _log_print(f"Loading embeddings model: {EMBEDDING_MODEL_NAME} on {EMBEDDING_DEVICE}...")
    embedding_function = get_embedding_function()

    _log_print(f"Initializing Multi-Collection Vector Database Manager at {DB_DIR}...")
    multi_db = MultiCollectionVectorDatabase(DB_DIR, embedding_function)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=MAX_WORKERS),
        options=[
            ("grpc.keepalive_time_ms", RPC_KEEPALIVE_TIME_MS),
            ("grpc.keepalive_timeout_ms", RPC_KEEPALIVE_TIMEOUT_MS),
            ("grpc.keepalive_permit_without_calls", RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS),
            ("grpc.http2.max_pings_without_data", RPC_HTTP2_MAX_PINGS_WITHOUT_DATA),
            ("grpc.http2.min_ping_interval_without_data_ms", RPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
        ],
    )
    pb2_grpc.add_VectorDatabaseServiceServicer_to_server(
        VectorDatabaseServicer(multi_db), server
    )

    with open(RPC_KEY, "rb") as f:
        private_key = f.read()
    with open(RPC_CRT, "rb") as f:
        certificate_chain = f.read()

    server_credentials = grpc.ssl_server_credentials(
        ((private_key, certificate_chain),)
    )

    server.add_secure_port(f"[::]:{PORT}", server_credentials)
    server.start()
    _log_print(f"gRPC Vector DB Server started SECURELY (TLS/HTTPS) on port {PORT}")
    _log_print(f"  max_workers={MAX_WORKERS}  db_dir={DB_DIR}  collection={COLLECTION_NAME}")

    def _graceful_shutdown(signum, _frame):
        _log_print(f"Received signal {signum}, shutting down gracefully...")
        done_event = server.stop(grace=5)
        done_event.wait()
        _log_print("Server stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    server.wait_for_termination()


if __name__ == "__main__":
    serve()
