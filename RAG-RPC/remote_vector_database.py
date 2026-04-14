import json
import grpc
from typing import Any, Dict, List, Optional
from langchain_core.documents import Document
from vector_database import VectorDatabase
import proto.vector_database_pb2 as pb2
import proto.vector_database_pb2_grpc as pb2_grpc

class RemoteVectorDatabase(VectorDatabase):
    def __init__(self, target: str, cert_path: str, collection_name: str):
        """
        Initializes the remote vector database client.
        
        Args:
            target: The gRPC server address (e.g., 'localhost:50051').
            cert_path: Path to the server's certificate (server.crt) for TLS verification.
            collection_name: The name of the collection to interact with.
        """
        with open(cert_path, "rb") as f:
            trusted_certs = f.read()
        
        # Use SSL credentials for secure communication
        credentials = grpc.ssl_channel_credentials(root_certificates=trusted_certs)
        self.channel = grpc.secure_channel(target, credentials)
        self.stub = pb2_grpc.VectorDatabaseServiceStub(self.channel)
        self.collection_name = collection_name

    def similarity_search(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        filter_json = json.dumps(filter) if filter else ""
        req = pb2.SimilaritySearchRequest(
            collection_name=self.collection_name, 
            query=query, 
            k=k, 
            filter_json=filter_json
        )
        resp = self.stub.SimilaritySearch(req)
        
        docs = []
        for d in resp.documents:
            metadata = json.loads(d.metadata_json) if d.metadata_json else {}
            docs.append(Document(page_content=d.page_content, metadata=metadata))
        return docs

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
        self.stub.Upsert(req)

    def delete_by_source_file(self, source_file: str) -> int:
        req = pb2.DeleteBySourceFileRequest(collection_name=self.collection_name, source_file=source_file)
        resp = self.stub.DeleteBySourceFile(req)
        return resp.deleted_count

    def clear(self) -> int:
        req = pb2.ClearRequest(collection_name=self.collection_name)
        resp = self.stub.Clear(req)
        return resp.deleted_count

    def count(self) -> int:
        req = pb2.CountRequest(collection_name=self.collection_name)
        resp = self.stub.Count(req)
        return resp.count

    def get_records(self, include: List[str], where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        where_json = json.dumps(where) if where else ""
        req = pb2.GetRecordsRequest(collection_name=self.collection_name, include=include, where_json=where_json)
        resp = self.stub.GetRecords(req)
        
        res = {}
        if resp.ids:
            res["ids"] = list(resp.ids)
        if resp.documents:
            res["documents"] = list(resp.documents)
        if resp.metadatas_json:
            res["metadatas"] = [json.loads(m) for m in resp.metadatas_json]
        return res
