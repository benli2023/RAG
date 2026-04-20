from __future__ import annotations

from functools import lru_cache

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from knowledge_base_service import get_child_collection_name, get_parent_collection_name, normalize_knowledge_base_name
from rag_config import (
    DB_DIR,
    EMBEDDING_DEVICE,
    EMBEDDING_MODEL_NAME,
)
from local_vector_database import LocalVectorDatabase
from vector_database import VectorDatabase


@lru_cache(maxsize=1)
def get_embedding_function() -> HuggingFaceEmbeddings:
    print(f"[rag_store] loading embeddings model: {EMBEDDING_MODEL_NAME} (device={EMBEDDING_DEVICE})")
    return HuggingFaceEmbeddings(
        model=EMBEDDING_MODEL_NAME,
        model_kwargs={"device": EMBEDDING_DEVICE},
        encode_kwargs={"normalize_embeddings": True},
    )


@lru_cache(maxsize=32)
def get_vectorstore(knowledge_base: str) -> VectorDatabase:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    chroma_store = Chroma(
        collection_name=get_child_collection_name(normalized_name),
        embedding_function=get_embedding_function(),
        persist_directory=str(DB_DIR),
    )
    return LocalVectorDatabase(chroma_store)


@lru_cache(maxsize=32)
def get_parent_vectorstore(knowledge_base: str) -> VectorDatabase:
    normalized_name = normalize_knowledge_base_name(knowledge_base)
    chroma_store = Chroma(
        collection_name=get_parent_collection_name(normalized_name),
        embedding_function=get_embedding_function(),
        persist_directory=str(DB_DIR),
    )
    return LocalVectorDatabase(chroma_store)
