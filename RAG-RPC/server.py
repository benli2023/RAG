import sys
import os
import platform

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

import grpc
from langchain_chroma import Chroma
from langchain_core.documents import Document

import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc
from local_vector_database import LocalVectorDatabase
from rag_config import DB_DIR, EMBEDDING_DEVICE, EMBEDDING_MODEL_NAME
from rag_store import get_embedding_function
from retrieval_pipeline_service import run_retrieval_pipeline

RAG_RPC_DIR = Path(__file__).resolve().parent


def _json_safe_value(value):
    if isinstance(value, Document):
        return {
            "page_content": value.page_content,
            "metadata": value.metadata,
        }

    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]

    return value


# ----------------- RPC Configuration -----------------
PORT = os.getenv("RPC_PORT", "50051")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "rag_collection_default")


class MultiCollectionVectorDatabase:
    def __init__(self, db_dir: str, embedding_function):
        self.db_dir = db_dir
        self.embedding_function = embedding_function
        self.collections = {}

    def get_db(self, collection_name: str) -> LocalVectorDatabase:
        if not collection_name:
            collection_name = COLLECTION_NAME
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
        print(f"[gRPC] {method_name} | Collection: {collection_name} | Peer: {peer}")

    def SimilaritySearch(self, request, context):
        self._log_request(context, "SimilaritySearch", request.collection_name)
        db = self.multi_db.get_db(request.collection_name)
        filter_dict = json.loads(request.filter_json) if request.filter_json else None
        k = request.k if request.k > 0 else 4
        docs = db.similarity_search(request.query, k=k, filter=filter_dict)

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
            print(f"[gRPC] Calculating embeddings for {len(request.documents)} documents...")
            docs = list(request.documents)
            batch_size = 32
            start_time = time.perf_counter()
            embedded_count = 0
            total_documents = len(docs)
            for i in range(0, total_documents, batch_size):
                batch_docs = docs[i : i + batch_size]
                batch_start = time.perf_counter()
                batch_embeddings = self.multi_db.embedding_function.embed_documents(batch_docs)
                embeddings.extend(batch_embeddings)
                embedded_count += len(batch_docs)
                batch_elapsed = time.perf_counter() - batch_start
                print(
                    f"[gRPC] Embedded {embedded_count}/{total_documents} "
                    f"batch={i // batch_size + 1} size={len(batch_docs)} "
                    f"time={batch_elapsed:.2f}s"
                )
            total_elapsed = time.perf_counter() - start_time
            print(f"[gRPC] Finished embedding {total_documents} chunks in {total_elapsed:.2f}s")

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
        records = db.get_records(include=include_list, where=where_dict)

        ids = records.get("ids", [])
        documents = records.get("documents", [])
        metadatas = records.get("metadatas", [])
        metadatas_json = [json.dumps(m) for m in metadatas] if metadatas else []

        return pb2.GetRecordsResponse(
            ids=ids,
            documents=documents,
            metadatas_json=metadatas_json,
        )

    def Retrieve(self, request, context):
        peer = context.peer()
        print(
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


def serve():
    Path(DB_DIR).mkdir(parents=True, exist_ok=True)
    print(f"Loading embeddings model: {EMBEDDING_MODEL_NAME} on {EMBEDDING_DEVICE}...")
    embedding_function = get_embedding_function()

    print(f"Initializing Multi-Collection Vector Database Manager at {DB_DIR}...")
    multi_db = MultiCollectionVectorDatabase(DB_DIR, embedding_function)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_VectorDatabaseServiceServicer_to_server(
        VectorDatabaseServicer(multi_db), server
    )

    cert_dir = RAG_RPC_DIR / "certs"
    key_path = cert_dir / "server.key"
    crt_path = cert_dir / "server.crt"

    if not key_path.exists() or not crt_path.exists():
        print(f"Error: TLS certificate files not found in {cert_dir}")
        print("Please ensure 'server.key' and 'server.crt' are present.")
        sys.exit(1)

    with open(key_path, "rb") as f:
        private_key = f.read()
    with open(crt_path, "rb") as f:
        certificate_chain = f.read()

    server_credentials = grpc.ssl_server_credentials(
        ((private_key, certificate_chain),)
    )

    server.add_secure_port(f"[::]:{PORT}", server_credentials)
    server.start()
    print(f"gRPC Vector DB Server started SECURELY (TLS/HTTPS) on port {PORT}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
