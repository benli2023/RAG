import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from langchain_core.documents import Document

class VectorDatabase(ABC):
    @abstractmethod
    def similarity_search_with_score(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        pass

    def similarity_search(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        return self.similarity_search_with_score(query, k=k, filter=filter)

    @abstractmethod
    def upsert(self, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]], embeddings: List[List[float]]) -> None:
        pass

    @abstractmethod
    def delete_by_source_file(self, source_file: str) -> int:
        pass

    @abstractmethod
    def clear(self) -> int:
        pass

    @abstractmethod
    def count(self) -> int:
        pass

    @abstractmethod
    def get_records(
        self,
        include: List[str],
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Returns dict with keys like 'ids', 'metadatas', 'documents'"""
        pass
