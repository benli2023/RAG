from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

import grpc
from langchain_core.documents import Document

import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc
from rag_config import REMOTE_DB_CERT, REMOTE_DB_TARGET, REMOTE_RPC_ADMIN_TIMEOUT_SECONDS, REMOTE_RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS, REMOTE_RPC_KEEPALIVE_TIME_MS, REMOTE_RPC_KEEPALIVE_TIMEOUT_MS, REMOTE_RPC_MAX_BATCH_BYTES, REMOTE_RPC_RETRY_ATTEMPTS, REMOTE_RPC_RETRIEVE_TIMEOUT_SECONDS, REMOTE_RPC_UPSERT_TIMEOUT_SECONDS


ResponseT = TypeVar("ResponseT")


RETRYABLE_RPC_CODES = {
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.UNAVAILABLE,
}


GRPC_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", REMOTE_RPC_KEEPALIVE_TIME_MS),
    ("grpc.keepalive_timeout_ms", REMOTE_RPC_KEEPALIVE_TIMEOUT_MS),
    ("grpc.keepalive_permit_without_calls", REMOTE_RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS),
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
]


TRACE_DOCUMENT_LIST_KEYS = {
    "parent_results",
    "parent_vector_results",
    "parent_bm25_results",
    "vector_results",
    "bm25_results",
    "fused_results",
    "reranked_results",
    "selected_results",
    "final_results",
}


def _serialize_document(document: Document) -> pb2.EsDocument:
    return pb2.EsDocument(
        page_content=document.page_content,
        metadata_json=json.dumps(document.metadata or {}, ensure_ascii=False, default=str),
    )


def _estimate_es_document_bytes(document: Document) -> int:
    metadata_json = json.dumps(document.metadata or {}, ensure_ascii=False, default=str)
    return len(document.page_content.encode("utf-8")) + len(metadata_json.encode("utf-8")) + 256


def _iter_es_document_batches(documents: list[Document]) -> list[list[Document]]:
    batches: list[list[Document]] = []
    current_batch: list[Document] = []
    current_size = 0

    for document in documents:
        document_size = _estimate_es_document_bytes(document)
        if current_batch and current_size + document_size > REMOTE_RPC_MAX_BATCH_BYTES:
            batches.append(current_batch)
            current_batch = []
            current_size = 0

        current_batch.append(document)
        current_size += document_size

    if current_batch:
        batches.append(current_batch)

    return batches


def _raise_for_rpc_error(operation: str, exc: grpc.RpcError) -> None:
    if exc.code() in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND}:
        raise ValueError(exc.details()) from exc
    raise RuntimeError(
        f"Remote RPC {operation} failed: {exc.code().name}: {exc.details()}"
    ) from exc


def _call_rpc_with_retries(operation: str, call: Callable[[float], ResponseT], timeout_seconds: int) -> ResponseT:
    last_error: grpc.RpcError | None = None
    for attempt in range(1, REMOTE_RPC_RETRY_ATTEMPTS + 1):
        try:
            return call(float(timeout_seconds))
        except grpc.RpcError as exc:
            last_error = exc
            if exc.code() not in RETRYABLE_RPC_CODES or attempt >= REMOTE_RPC_RETRY_ATTEMPTS:
                _raise_for_rpc_error(operation, exc)
            time.sleep(min(0.25 * attempt, 1.0))

    if last_error is not None:
        _raise_for_rpc_error(operation, last_error)
    raise RuntimeError(f"Remote RPC {operation} failed without an error response")


def _decode_json_payload(payload_text: str, error_message: str) -> dict:
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(error_message) from exc

    if not isinstance(payload, dict):
        return {}

    return payload


def _restore_document(value: object) -> object:
    if isinstance(value, dict) and "page_content" in value and "metadata" in value:
        return Document(
            page_content=str(value.get("page_content", "")),
            metadata=dict(value.get("metadata") or {}),
        )

    return value


def _restore_trace_payload(trace_payload: object) -> object:
    if not isinstance(trace_payload, dict):
        return trace_payload

    restored_trace = dict(trace_payload)
    for key in TRACE_DOCUMENT_LIST_KEYS:
        documents = restored_trace.get(key)
        if isinstance(documents, list):
            restored_trace[key] = [_restore_document(item) for item in documents]

    return restored_trace


class RemoteRetrievalService:
    def __init__(self, target: str, cert_path: str):
        certificate_path = Path(cert_path)
        if not certificate_path.exists():
            raise FileNotFoundError(
                f"Certificate file not found at: {cert_path}. "
                "Please ensure the RPC server certificate is available to the backend."
            )

        with open(certificate_path, "rb") as file_handle:
            trusted_certs = file_handle.read()

        credentials = grpc.ssl_channel_credentials(root_certificates=trusted_certs)
        self.channel = grpc.secure_channel(target, credentials, options=GRPC_CHANNEL_OPTIONS)
        self.stub = pb2_grpc.VectorDatabaseServiceStub(self.channel)

    def run_retrieval_pipeline(
        self,
        *,
        query: str,
        username: str,
        domains: list[str] | None = None,
        source_files: list[str] | None = None,
        query_type: str | None = None,
        top_k: int | None = None,
        knowledge_base: str | None = None,
        debug: bool = False,
    ) -> dict:
        request = pb2.RetrieveRequest(
            query=query,
            username=username,
            domains=list(domains or []),
            source_files=list(source_files or []),
            query_type=query_type or "",
            top_k=int(top_k) if top_k is not None else 0,
            knowledge_base=knowledge_base or "",
            debug=debug,
        )

        response = _call_rpc_with_retries(
            "Retrieve",
            lambda timeout: self.stub.Retrieve(request, timeout=timeout),
            REMOTE_RPC_RETRIEVE_TIMEOUT_SECONDS,
        )

        payload = _decode_json_payload(
            response.response_json,
            "Remote retrieval RPC returned invalid JSON payload",
        )

        response_payload = payload.get("response", {})
        trace_payload = _restore_trace_payload(payload.get("trace"))

        return {
            "status": response.status or "success",
            "response": response_payload,
            "trace": trace_payload,
        }

    def upsert_chunks_to_es(self, chunks: list[Document], index_name: str) -> dict:
        if not chunks:
            request = pb2.UpsertEsChunksRequest(index_name=index_name or "", chunks=[])
            response = _call_rpc_with_retries(
                "UpsertEsChunks",
                lambda timeout: self.stub.UpsertEsChunks(request, timeout=timeout),
                REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
            )
            return {
                "enabled": response.enabled,
                "index_name": response.index_name or index_name,
                "indexed_count": response.indexed_count,
                "sync_seconds": response.sync_seconds,
            }

        total_indexed_count = 0
        total_sync_seconds = 0.0
        resolved_index_name = index_name
        enabled = True

        for batch in _iter_es_document_batches(chunks):
            request = pb2.UpsertEsChunksRequest(
                index_name=index_name or "",
                chunks=[_serialize_document(document) for document in batch],
            )
            response = _call_rpc_with_retries(
                "UpsertEsChunks",
                lambda timeout: self.stub.UpsertEsChunks(request, timeout=timeout),
                REMOTE_RPC_UPSERT_TIMEOUT_SECONDS,
            )
            enabled = response.enabled
            resolved_index_name = response.index_name or resolved_index_name
            total_indexed_count += int(response.indexed_count)
            total_sync_seconds += float(response.sync_seconds)
            if not enabled:
                break

        return {
            "enabled": enabled,
            "index_name": resolved_index_name,
            "indexed_count": total_indexed_count,
            "sync_seconds": round(total_sync_seconds, 2),
        }

    def delete_by_source_file_in_es(self, source_file: str, index_name: str) -> int:
        request = pb2.DeleteBySourceFileInEsRequest(
            index_name=index_name or "",
            source_file=source_file,
        )

        response = _call_rpc_with_retries(
            "DeleteBySourceFileInEs",
            lambda timeout: self.stub.DeleteBySourceFileInEs(request, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )

        return int(response.deleted_count)

    def clear_es_index(self, index_name: str, recreate: bool = False) -> int:
        request = pb2.ClearEsIndexRequest(
            index_name=index_name or "",
            recreate=recreate,
        )

        response = _call_rpc_with_retries(
            "ClearEsIndex",
            lambda timeout: self.stub.ClearEsIndex(request, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )

        return int(response.deleted_count)

    def get_es_runtime_config(self, index_names: list[str] | None = None) -> dict:
        request = pb2.GetEsRuntimeConfigRequest(
            index_names=list(index_names or []),
        )

        response = _call_rpc_with_retries(
            "GetEsRuntimeConfig",
            lambda timeout: self.stub.GetEsRuntimeConfig(request, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )

        return _decode_json_payload(
            response.runtime_json,
            "Remote ES RPC returned invalid JSON payload",
        )

    def health_check(
        self,
        *,
        collection_names: list[str] | None = None,
        index_names: list[str] | None = None,
        include_es: bool = True,
    ) -> dict:
        request = pb2.HealthCheckRequest(
            collection_names=list(collection_names or []),
            index_names=list(index_names or []),
            include_es=include_es,
        )

        response = _call_rpc_with_retries(
            "HealthCheck",
            lambda timeout: self.stub.HealthCheck(request, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )

        details = _decode_json_payload(
            response.details_json,
            "Remote health RPC returned invalid JSON payload",
        )
        details["ready"] = bool(response.ready)
        details["status"] = response.status or details.get("status", "unknown")
        return details


@lru_cache(maxsize=1)
def get_remote_retrieval_service() -> RemoteRetrievalService:
    return RemoteRetrievalService(
        target=REMOTE_DB_TARGET,
        cert_path=str(REMOTE_DB_CERT),
    )