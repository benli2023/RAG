import sys
from rag_store import get_vectorstore

vs = get_vectorstore("default")
printed = 0
offset = 0
while printed < 5:
    records = vs.get_records(include=["metadatas"], limit=100, offset=offset)
    for meta in records.get("metadatas", []):
        print(meta)
        printed += 1
        if printed >= 5:
            break
    if not records.get("has_more") or records.get("next_offset", offset) <= offset:
        break
    offset = int(records["next_offset"])
