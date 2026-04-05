from pydantic import BaseModel
from access_control import ANONYMOUS_USERNAME


class SourceFileRequest(BaseModel):
    source_file: str


class QueryRequest(BaseModel):
    query: str
    username: str = ANONYMOUS_USERNAME
