from typing import Any, Dict, List, Optional
from langchain_chroma import Chroma
from langchain_core.documents import Document
from vector_database import VectorDatabase


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _relevance_score_from_distance(distance: float) -> float:
    return 1.0 / (1.0 + max(distance, 0.0))


def _attach_vector_score_metadata(document: Document, distance: object, rank: int) -> Document:
    metadata = dict(document.metadata or {})
    distance_value = _coerce_float(distance)
    metadata["vector_rank"] = rank
    if distance_value is not None:
        metadata["vector_distance"] = round(distance_value, 6)
        metadata["vector_score"] = round(_relevance_score_from_distance(distance_value), 6)
        metadata["vector_score_source"] = "chroma_distance"

    return Document(page_content=document.page_content, metadata=metadata)


class LocalVectorDatabase(VectorDatabase):
    def __init__(self, chroma_store: Chroma):
        self.store = chroma_store

    def similarity_search_with_score(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        if filter is not None:
            scored_results = self.store.similarity_search_with_score(query, k=k, filter=filter)
        else:
            scored_results = self.store.similarity_search_with_score(query, k=k)

        return [
            _attach_vector_score_metadata(document, distance, rank)
            for rank, (document, distance) in enumerate(scored_results, start=1)
        ]

    def similarity_search(self, query: str, k: int, filter: Optional[Dict[str, Any]] = None) -> List[Document]:
        return self.similarity_search_with_score(query, k=k, filter=filter)

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

    def get_records(
        self,
        include: List[str],
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        get_kwargs: Dict[str, Any] = {"include": include}
        if where is not None:
            get_kwargs["where"] = where
        if limit is not None and limit > 0:
            get_kwargs["limit"] = int(limit)
            get_kwargs["offset"] = max(0, int(offset))

        records = self.store._collection.get(**get_kwargs)
        ids = records.get("ids", []) or []
        returned_count = len(ids)
        normalized_offset = max(0, int(offset))
        next_offset = normalized_offset + returned_count
        total_count = self.count() if where is None else None
        records["next_offset"] = next_offset
        records["has_more"] = bool(
            limit is not None
            and limit > 0
            and (next_offset < total_count if total_count is not None else returned_count == int(limit))
        )
        records["total_count"] = total_count
        return records
