from __future__ import annotations

import os
from functools import lru_cache

from langchain_chroma import Chroma

from knowledge_base_service import get_child_collection_name, get_parent_collection_name, normalize_knowledge_base_name
from rag_config import (
    DB_DIR, DEFAULT_KNOWLEDGE_BASE,
    USE_REMOTE_DB, REMOTE_DB_TARGET, REMOTE_DB_CERT
)
from local_vector_database import LocalVectorDatabase
from remote_vector_database import RemoteVectorDatabase
from vector_database import VectorDatabase


@lru_cache(maxsize=32)
def get_vectorstore(knowledge_base: str) -> VectorDatabase:
    if USE_REMOTE_DB:
        normalized_name = normalize_knowledge_base_name(knowledge_base)
        return RemoteVectorDatabase(
            target=REMOTE_DB_TARGET,
            cert_path=REMOTE_DB_CERT,
            collection_name=get_child_collection_name(normalized_name)
        )

    normalized_name = normalize_knowledge_base_name(knowledge_base)
    chroma_store = Chroma(
        collection_name=get_child_collection_name(normalized_name),
        embedding_function=None,
        persist_directory=DB_DIR,
    )
    return LocalVectorDatabase(chroma_store)


@lru_cache(maxsize=32)
def get_parent_vectorstore(knowledge_base: str) -> VectorDatabase:
    if USE_REMOTE_DB:
        normalized_name = normalize_knowledge_base_name(knowledge_base)
        return RemoteVectorDatabase(
            target=REMOTE_DB_TARGET,
            cert_path=REMOTE_DB_CERT,
            collection_name=get_parent_collection_name(normalized_name)
        )

    normalized_name = normalize_knowledge_base_name(knowledge_base)
    chroma_store = Chroma(
        collection_name=get_parent_collection_name(normalized_name),
        embedding_function=None,
        persist_directory=DB_DIR,
    )
    return LocalVectorDatabase(chroma_store)


vectorstore = get_vectorstore(DEFAULT_KNOWLEDGE_BASE)
parent_vectorstore = get_parent_vectorstore(DEFAULT_KNOWLEDGE_BASE)
