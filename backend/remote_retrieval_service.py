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
            if exc.code() in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND}:
                raise ValueError(exc.details()) from exc
            raise RuntimeError(
                f"Remote retrieval RPC failed: {exc.code().name}: {exc.details()}"
            ) from exc

        try:
            payload = json.loads(response.response_json) if response.response_json else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("Remote retrieval RPC returned invalid JSON payload") from exc

        if not isinstance(payload, dict):
            payload = {}

        response_payload = payload.get("response", {})
        trace_payload = _restore_trace_payload(payload.get("trace"))

        return {
            "status": response.status or "success",
            "response": response_payload,
            "trace": trace_payload,
        }


@lru_cache(maxsize=1)
def get_remote_retrieval_service() -> RemoteRetrievalService:
    return RemoteRetrievalService(
        target=REMOTE_DB_TARGET,
        cert_path=str(REMOTE_DB_CERT),
    )