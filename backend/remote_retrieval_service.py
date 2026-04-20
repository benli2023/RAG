from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import grpc
from langchain_core.documents import Document

import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc
from rag_config import REMOTE_DB_CERT, REMOTE_DB_TARGET


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


def _raise_for_rpc_error(operation: str, exc: grpc.RpcError) -> None:
    if exc.code() in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND}:
        raise ValueError(exc.details()) from exc
    raise RuntimeError(
        f"Remote RPC {operation} failed: {exc.code().name}: {exc.details()}"
    ) from exc


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
        self.channel = grpc.secure_channel(target, credentials)
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

        try:
            response = self.stub.Retrieve(request)
        except grpc.RpcError as exc:
            _raise_for_rpc_error("Retrieve", exc)

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
        request = pb2.UpsertEsChunksRequest(
            index_name=index_name or "",
            chunks=[_serialize_document(document) for document in chunks],
        )

        try:
            response = self.stub.UpsertEsChunks(request)
        except grpc.RpcError as exc:
            _raise_for_rpc_error("UpsertEsChunks", exc)

        return {
            "enabled": response.enabled,
            "index_name": response.index_name or index_name,
            "indexed_count": response.indexed_count,
            "sync_seconds": response.sync_seconds,
        }

    def delete_by_source_file_in_es(self, source_file: str, index_name: str) -> int:
        request = pb2.DeleteBySourceFileInEsRequest(
            index_name=index_name or "",
            source_file=source_file,
        )

        try:
            response = self.stub.DeleteBySourceFileInEs(request)
        except grpc.RpcError as exc:
            _raise_for_rpc_error("DeleteBySourceFileInEs", exc)

        return int(response.deleted_count)

    def clear_es_index(self, index_name: str, recreate: bool = False) -> int:
        request = pb2.ClearEsIndexRequest(
            index_name=index_name or "",
            recreate=recreate,
        )

        try:
            response = self.stub.ClearEsIndex(request)
        except grpc.RpcError as exc:
            _raise_for_rpc_error("ClearEsIndex", exc)

        return int(response.deleted_count)

    def get_es_runtime_config(self, index_names: list[str] | None = None) -> dict:
        request = pb2.GetEsRuntimeConfigRequest(
            index_names=list(index_names or []),
        )

        try:
            response = self.stub.GetEsRuntimeConfig(request)
        except grpc.RpcError as exc:
            _raise_for_rpc_error("GetEsRuntimeConfig", exc)

        return _decode_json_payload(
            response.runtime_json,
            "Remote ES RPC returned invalid JSON payload",
        )


@lru_cache(maxsize=1)
def get_remote_retrieval_service() -> RemoteRetrievalService:
    return RemoteRetrievalService(
        target=REMOTE_DB_TARGET,
        cert_path=str(REMOTE_DB_CERT),
    )