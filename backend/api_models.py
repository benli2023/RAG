from pydantic import BaseModel, Field
from access_control import ANONYMOUS_USERNAME


class SourceFileRequest(BaseModel):
    source_file: str


class QueryRequest(BaseModel):
    query: str
    domains: list[str] = Field(default_factory=list)
    query_type: str = "semantic"
    username: str = ANONYMOUS_USERNAME
