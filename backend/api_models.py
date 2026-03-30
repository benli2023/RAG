from typing import List
from pydantic import BaseModel, Field
from access_control import ANONYMOUS_USERNAME


class SourceFileRequest(BaseModel):
    source_file: str


class QueryRequest(BaseModel):
    query: str
    domains: List[str] = Field(default_factory=list)
    username: str = ANONYMOUS_USERNAME
