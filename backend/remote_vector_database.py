import json
import time
import grpc
from typing import Any, Dict, List, Optional
from langchain_core.documents import Document
from vector_database import VectorDatabase
import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc
from rag_config import REMOTE_RPC_ADMIN_TIMEOUT_SECONDS, REMOTE_RPC_GET_RECORDS_PAGE_SIZE, REMOTE_RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS, REMOTE_RPC_KEEPALIVE_TIME_MS, REMOTE_RPC_KEEPALIVE_TIMEOUT_MS, REMOTE_RPC_RETRY_ATTEMPTS, REMOTE_RPC_RETRIEVE_TIMEOUT_SECONDS, REMOTE_RPC_UPSERT_TIMEOUT_SECONDS


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


def _raise_for_rpc_error(operation: str, exc: grpc.RpcError) -> None:
    if exc.code() in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND}:
        raise ValueError(exc.details()) from exc
    raise RuntimeError(f"Remote RPC {operation} failed: {exc.code().name}: {exc.details()}") from exc


def _call_rpc(operation: str, call, timeout_seconds: int):
    last_error = None
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


def _decode_records_response(resp) -> Dict[str, Any]:
    records: Dict[str, Any] = {
        "ids": list(resp.ids),
        "documents": list(resp.documents),
        "metadatas": [json.loads(m) for m in resp.metadatas_json],
        "total_count": int(resp.total_count),
        "next_offset": int(resp.next_offset),
        "has_more": bool(resp.has_more),
    }
    return records


def _merge_record_pages(pages) -> Dict[str, Any]:
    merged: Dict[str, Any] = {"ids": [], "documents": [], "metadatas": []}
    total_count = -1
    next_offset = 0
    for page in pages:
        merged["ids"].extend(page.get("ids", []))
        merged["documents"].extend(page.get("documents", []))
        merged["metadatas"].extend(page.get("metadatas", []))
        total_count = page.get("total_count", total_count)
        next_offset = page.get("next_offset", next_offset)

    merged["total_count"] = total_count
    merged["next_offset"] = next_offset
    merged["has_more"] = False
    return merged


class RemoteVectorDatabase(VectorDatabase):
    def __init__(self, target: str, cert_path: str, collection_name: str):
        import os
        if not os.path.exists(cert_path):
            raise FileNotFoundError(f"Certificate file not found at: {cert_path}. "
                                    f"Please ensure you have generated or copied the server.crt from RAG-RPC/certs/")
        
        with open(cert_path, "rb") as f:
            trusted_certs = f.read()
        credentials = grpc.ssl_channel_credentials(root_certificates=trusted_certs)
        self.channel = grpc.secure_channel(target, credentials, options=GRPC_CHANNEL_OPTIONS)
        self.stub = pb2_grpc.VectorDatabaseServiceStub(self.channel)
        self.collection_name = collection_name

    def similarity_search_with_score(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        filter_json = json.dumps(filter) if filter else ""
        req = pb2.SimilaritySearchRequest(collection_name=self.collection_name, query=query, k=k, filter_json=filter_json)
        resp = _call_rpc(
            "SimilaritySearch",
            lambda timeout: self.stub.SimilaritySearch(req, timeout=timeout),
            REMOTE_RPC_RETRIEVE_TIMEOUT_SECONDS,
        )
        
        docs = []
        for proto_document in resp.documents:
            metadata = json.loads(proto_document.metadata_json) if proto_document.metadata_json else {}
            docs.append(Document(page_content=proto_document.page_content, metadata=metadata))
        return docs

    def similarity_search(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        return self.similarity_search_with_score(query, k=k, filter=filter)

    def upsert(self, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]], embeddings: List[List[float]]) -> None:
        metadatas_json = [json.dumps(m) for m in metadatas]
        embeddings_proto = [pb2.FloatArray(values=e) for e in embeddings]
        req = pb2.UpsertRequest(
            collection_name=self.collection_name,
            ids=ids,
            documents=documents,
            metadatas_json=metadatas_json,
            embeddings=embeddings_proto
        )
        _call_rpc(
            "Upsert",
            lambda timeout: self.stub.Upsert(req, timeout=timeout),
            REMOTE_RPC_UPSERT_TIMEOUT_SECONDS,
        )

    def delete_by_source_file(self, source_file: str) -> int:
        req = pb2.DeleteBySourceFileRequest(collection_name=self.collection_name, source_file=source_file)
        resp = _call_rpc(
            "DeleteBySourceFile",
            lambda timeout: self.stub.DeleteBySourceFile(req, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )
        return resp.deleted_count

    def clear(self) -> int:
        req = pb2.ClearRequest(collection_name=self.collection_name)
        resp = _call_rpc(
            "Clear",
            lambda timeout: self.stub.Clear(req, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )
        return resp.deleted_count

    def count(self) -> int:
        req = pb2.CountRequest(collection_name=self.collection_name)
        resp = _call_rpc(
            "Count",
            lambda timeout: self.stub.Count(req, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )
        return resp.count

    def _get_records_page(
        self,
        include: List[str],
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        where_json = json.dumps(where) if where else ""
        req = pb2.GetRecordsRequest(
            collection_name=self.collection_name,
            include=include,
            where_json=where_json,
            limit=int(limit or 0),
            offset=max(0, int(offset)),
        )
        resp = _call_rpc(
            "GetRecords",
            lambda timeout: self.stub.GetRecords(req, timeout=timeout),
            REMOTE_RPC_ADMIN_TIMEOUT_SECONDS,
        )
        return _decode_records_response(resp)

    def iter_records(
        self,
        include: List[str],
        where: Optional[Dict[str, Any]] = None,
        batch_size: Optional[int] = None,
    ):
        page_size = max(1, int(batch_size or REMOTE_RPC_GET_RECORDS_PAGE_SIZE))
        offset = 0
        while True:
            page = self._get_records_page(include=include, where=where, limit=page_size, offset=offset)
            yield page
            if not page.get("has_more") or page.get("next_offset", offset) <= offset:
                break
            offset = int(page["next_offset"])

    def get_records(
        self,
        include: List[str],
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if limit is not None and limit > 0:
            return self._get_records_page(include=include, where=where, limit=limit, offset=offset)

        return _merge_record_pages(self.iter_records(include=include, where=where))
