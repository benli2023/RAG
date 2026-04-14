from typing import Any, Dict, List, Optional
from langchain_chroma import Chroma
from langchain_core.documents import Document
from vector_database import VectorDatabase

class LocalVectorDatabase(VectorDatabase):
    def __init__(self, chroma_store: Chroma):
        self.store = chroma_store

    def similarity_search(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        if filter is not None:
            return self.store.similarity_search(query, k=k, filter=filter)
        return self.store.similarity_search(query, k=k)

    def upsert(self, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]], embeddings: List[List[float]]) -> None:
        self.store._collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)

    def delete_by_source_file(self, source_file: str) -> int:
        records = self.store._collection.get(where={"source_file": source_file}, include=[])
        ids = records.get("ids", [])
        if ids:
            self.store._collection.delete(ids=ids)
        return len(ids)

    def clear(self) -> int:
        existing = self.store._collection.get(include=[])
        ids = existing.get("ids", [])
        if ids:
            self.store._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        try:
            return int(self.store._collection.count())
        except Exception:
            return 0

    def get_records(self, include: List[str], where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if where is not None:
            return self.store._collection.get(where=where, include=include)
        return self.store._collection.get(include=include)
