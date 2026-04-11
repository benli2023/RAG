from pydantic import BaseModel, Field
from access_control import ANONYMOUS_USERNAME
from rag_config import DEFAULT_KNOWLEDGE_BASE


class SourceFileRequest(BaseModel):
    source_file: str
    knowledge_base: str = DEFAULT_KNOWLEDGE_BASE


class QueryRequest(BaseModel):
    query: str
    domains: list[str] = Field(default_factory=list)
    query_type: str = "semantic"
    username: str = ANONYMOUS_USERNAME
    knowledge_base: str = DEFAULT_KNOWLEDGE_BASE
