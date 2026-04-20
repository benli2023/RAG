import sys
from rag_store import get_vectorstore
vs = get_vectorstore("default")
records = vs.get_records(include=["metadatas"])
for meta in records.get("metadatas", [])[:5]:
    print(meta)
