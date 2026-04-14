from __future__ import annotations

import os
from functools import lru_cache

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings

from knowledge_base_service import get_child_collection_name, get_parent_collection_name, normalize_knowledge_base_name
from rag_config import (
    DB_DIR, DEFAULT_KNOWLEDGE_BASE, LOCAL_MODEL_PATH,
    USE_REMOTE_DB, REMOTE_DB_TARGET, REMOTE_DB_CERT
)
from local_vector_database import LocalVectorDatabase
from remote_vector_database import RemoteVectorDatabase
from vector_database import VectorDatabase


EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")
EMBEDDING_MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL_NAME",
    str(LOCAL_MODEL_PATH) if LOCAL_MODEL_PATH.exists() else "intfloat/multilingual-e5-small",
)
EMBEDDING_FALLBACK_MODEL_NAME = os.getenv("EMBEDDING_FALLBACK_MODEL_NAME", "intfloat/multilingual-e5-small")


def _create_embedding_function(model_name: str, local_files_only: bool) -> HuggingFaceEmbeddings:
    print(
        f"[embeddings] loading model: {model_name} "
        f"(device={EMBEDDING_DEVICE}, local_files_only={local_files_only})"
    )
    return HuggingFaceEmbeddings(
        model=model_name,
        model_kwargs={
            "device": EMBEDDING_DEVICE,
            "local_files_only": local_files_only,
        },
        encode_kwargs={"normalize_embeddings": True},
        query_encode_kwargs={"normalize_embeddings": True},
    )


class LazyEmbeddingFunction(Embeddings):
    def __init__(self) -> None:
        self._embedding_function: HuggingFaceEmbeddings | None = None
        self._loaded_model_name: str | None = None

    @property
    def loaded_model_name(self) -> str | None:
        return self._loaded_model_name

    def _load(self) -> HuggingFaceEmbeddings:
        if self._embedding_function is not None:
            return self._embedding_function

        load_attempts: list[tuple[str, bool]] = [
            (EMBEDDING_MODEL_NAME, LOCAL_MODEL_PATH.exists()),
        ]

        if EMBEDDING_FALLBACK_MODEL_NAME != EMBEDDING_MODEL_NAME:
            load_attempts.append((EMBEDDING_FALLBACK_MODEL_NAME, False))

        last_error: Exception | None = None
        for model_name, local_files_only in load_attempts:
            try:
                self._embedding_function = _create_embedding_function(model_name, local_files_only)
                self._loaded_model_name = model_name
                print(f"[embeddings] selected model: {model_name}")
                return self._embedding_function
            except OSError as exc:
                last_error = exc
                print(f"[embeddings] load failed for {model_name}: {exc}")
            except Exception as exc:
                last_error = exc
                print(f"[embeddings] load failed for {model_name}: {exc}")

        raise RuntimeError("failed to load any embedding model") from last_error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._load().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._load().embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


embedding_function = LazyEmbeddingFunction()


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
        embedding_function=embedding_function,
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
        embedding_function=embedding_function,
        persist_directory=DB_DIR,
    )
    return LocalVectorDatabase(chroma_store)


vectorstore = get_vectorstore(DEFAULT_KNOWLEDGE_BASE)
parent_vectorstore = get_parent_vectorstore(DEFAULT_KNOWLEDGE_BASE)
