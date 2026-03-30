from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceBgeEmbeddings
from rag_config import DB_DIR, LOCAL_MODEL_PATH

print(f"正在加载本地 BGE-M3 模型: {LOCAL_MODEL_PATH}")
embedding_function = HuggingFaceBgeEmbeddings(
    model_name=str(LOCAL_MODEL_PATH),
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True},
)

vectorstore = Chroma(embedding_function=embedding_function, persist_directory=DB_DIR)
