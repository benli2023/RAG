from __future__ import annotations

from functools import lru_cache

from knowledge_base_service import get_child_collection_name, get_parent_collection_name, normalize_knowledge_base_name
from rag_config import (
    REMOTE_DB_TARGET,
    REMOTE_DB_CERT,
)
from remote_vector_database import RemoteVectorDatabase
from vector_database import VectorDatabase


@lru_cache(maxsize=32)
def get_vectorstore(knowledge_base: str) -> VectorDatabase:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    return RemoteVectorDatabase(
        target=REMOTE_DB_TARGET,
        cert_path=REMOTE_DB_CERT,
        collection_name=get_child_collection_name(normalized_name),
    )


@lru_cache(maxsize=32)
def get_parent_vectorstore(knowledge_base: str) -> VectorDatabase:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    return RemoteVectorDatabase(
        target=REMOTE_DB_TARGET,
        cert_path=REMOTE_DB_CERT,
        collection_name=get_parent_collection_name(normalized_name),
    )
