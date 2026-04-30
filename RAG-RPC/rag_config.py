import json
import os
from pathlib import Path

RAG_RPC_DIR = Path(__file__).resolve().parent
RAG_CONFIG_PATH = os.getenv("RAG_CONFIG_PATH", str(Path(__file__).resolve().parent / "config.json"))
try:
	with open(RAG_CONFIG_PATH, "r", encoding="utf-8") as _f:
		_rag_config_json = json.load(_f)
except Exception:
	_rag_config_json = {}

_certs_dir = _rag_config_json.get("certs_dir", "certs")
if os.path.isabs(_certs_dir):
	CERTS_DIR = Path(_certs_dir)
else:
	CERTS_DIR = Path(__file__).resolve().parent / _certs_dir


def _get_path_config(name: str, default: str, env_name: str | None = None) -> Path:
	if env_name is not None:
		env_value = os.getenv(env_name)
		if env_value:
			raw_value = env_value
		else:
			raw_value = _rag_config_json.get(name, default)
	else:
		raw_value = _rag_config_json.get(name, default)
	path = Path(str(raw_value)).expanduser()
	if path.is_absolute():
		return path.resolve()

	return (RAG_RPC_DIR / path).resolve()

SERVER_CRT = CERTS_DIR / _rag_config_json.get("server", {}).get("crt", "server.crt")
SERVER_KEY = CERTS_DIR / _rag_config_json.get("server", {}).get("key", "server.key")

RPC_CRT = CERTS_DIR / _rag_config_json.get("rpc", {}).get("crt", "server.crt")
RPC_KEY = CERTS_DIR / _rag_config_json.get("rpc", {}).get("key", "server.key")

ES_CRT = CERTS_DIR / _rag_config_json.get("elasticsearch", {}).get("crt", "server.crt")
ES_KEY = CERTS_DIR / _rag_config_json.get("elasticsearch", {}).get("key", "server.key")


def _parse_bool(value: object, default: bool) -> bool:
	if value is None:
		return default

	if isinstance(value, bool):
		return value

	return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_bool_config(env_name: str, default: bool) -> bool:
	return _parse_bool(os.getenv(env_name), default)


def _get_int_config(env_name: str, default: int) -> int:
	raw_value = os.getenv(env_name)
	if raw_value is None:
		return default

	try:
		return int(raw_value)
	except ValueError:
		return default


def _get_config_source(env_name: str) -> str:
	return "env" if os.getenv(env_name) is not None else "file"


def _build_runtime_health() -> dict[str, object]:
	alerts: list[dict[str, str]] = []
	field_severities: dict[str, str] = {}

	if ENABLE_ACL and not USERNAME_GROUP_MAPPING_FILE.is_file():
		alerts.append({
			"severity": "error",
			"title": "ACL 已开启，但权限映射文件缺失",
			"message": "当前启用了文档访问控制，但 username_group_mapping.json 不存在。依赖用户组的权限判断可能无法按预期生效。",
		})
		field_severities["access_control.enable_acl"] = "error"
		field_severities["access_control.username_group_mapping_exists"] = "error"

	if DASHBOARD_DIR.exists() and not DASHBOARD_INDEX.is_file():
		alerts.append({
			"severity": "error",
			"title": "Dashboard 目录存在，但首页文件缺失",
			"message": "`/dashboard` 入口依赖 static/index.html。当前目录可用，但首页文件不存在，页面将无法正常打开。",
		})
		field_severities["dashboard.dashboard_enabled"] = "error"
		field_severities["dashboard.dashboard_index_exists"] = "error"

	if RERANKER_ENABLED and RERANKER_DEVICE == "cpu":
		alerts.append({
			"severity": "warning",
			"title": "Reranker 正在使用 CPU",
			"message": "当前已启用 reranker，但推理设备为 cpu。检索精排可以工作，但响应耗时可能明显上升。",
		})
		field_severities["retrieval.reranker_device"] = "warning"

	if ENABLE_SUB_CHUNKING:
		alerts.append({
			"severity": "warning",
			"title": "子分块已启用",
			"message": "更细粒度切分通常会提高召回覆盖，但也会增加 chunk 数量、嵌入成本和重建时间。",
		})
		field_severities["chunking.enable_sub_chunking"] = "warning"

	if not ES_ENABLED:
		alerts.append({
			"severity": "warning",
			"title": "Elasticsearch BM25 未启用",
			"message": "当前未配置 ES BM25 召回，系统会仅使用向量召回并跳过 ES 写入与 BM25 检索。",
		})
		field_severities["retrieval.es_enabled"] = "warning"

	error_count = sum(1 for item in alerts if item["severity"] == "error")
	warning_count = sum(1 for item in alerts if item["severity"] == "warning")
	status = "error" if error_count else "warning" if warning_count else "ok"

	return {
		"status": status,
		"error_count": error_count,
		"warning_count": warning_count,
		"alerts": alerts,
		"field_severities": field_severities,
	}

# ==================== 路径配置 ====================

# 项目根目录
PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", str(RAG_RPC_DIR.parent))).expanduser().resolve()
# 文档知识库目录
DOCS_DIR = Path(os.getenv("DOCS_DIR", str(PROJECT_ROOT / "docs"))).expanduser().resolve()
# 默认知识库名称
DEFAULT_KNOWLEDGE_BASE = os.getenv("DEFAULT_KNOWLEDGE_BASE", "shop").strip() or "shop"
# 用户与权限组映射配置文件
USERNAME_GROUP_MAPPING_FILE = Path(__file__).resolve().parent / "username_group_mapping.json"

# ==================== 索引与模型配置 ====================

# 本地嵌入模型目录
LOCAL_MODEL_PATH = _get_path_config("local_model_path", "../my_local_bge_m3", "LOCAL_MODEL_PATH")
# 嵌入模型标识；本地目录存在时优先使用本地模型
EMBEDDING_MODEL_NAME = os.getenv(
	"EMBEDDING_MODEL_NAME",
	str(LOCAL_MODEL_PATH) if LOCAL_MODEL_PATH.exists() else "BAAI/bge-m3",
)
# 嵌入模型推理设备
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")
# 向量数据库存储目录
DB_DIR = _get_path_config("db_dir", "chroma_db", "DB_DIR")
# 本地 Reranker 模型目录
LOCAL_RERANKER_PATH = _get_path_config("local_reranker_path", "../my_local_bge_reranker", "LOCAL_RERANKER_PATH")
# Elasticsearch 连接地址
ES_URL = _rag_config_json.get("ES_URL", "https://localhost:9200")
# 是否启用 print 输出日志
PRINT_LOGGING_ENABLED = _parse_bool(
	os.getenv("PRINT_LOGGING_ENABLED"),
	_parse_bool(_rag_config_json.get("print_logging_enabled"), True),
)
# gRPC keepalive defaults stay conservative so long batch upserts do not trigger
# ENHANCE_YOUR_CALM / too_many_pings on clients or servers.
RPC_KEEPALIVE_TIME_MS = max(1_000, _get_int_config("RPC_KEEPALIVE_TIME_MS", 300_000))
RPC_KEEPALIVE_TIMEOUT_MS = max(1_000, _get_int_config("RPC_KEEPALIVE_TIMEOUT_MS", 10_000))
RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS = _get_bool_config("RPC_KEEPALIVE_PERMIT_WITHOUT_CALLS", False)
RPC_HTTP2_MAX_PINGS_WITHOUT_DATA = max(0, _get_int_config("RPC_HTTP2_MAX_PINGS_WITHOUT_DATA", 0))
RPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS = max(1_000, _get_int_config("RPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS", 300_000))
# Elasticsearch 子切片倒排索引名称
ES_INDEX_NAME = os.getenv("ES_INDEX_NAME", "rag_docs_bm25")
# Elasticsearch 父文档倒排索引名称
ES_PARENT_INDEX_NAME = os.getenv("ES_PARENT_INDEX_NAME", f"{ES_INDEX_NAME}_parent")

# ==================== 检索配置 ====================

# 初始召回的候选文档数量
RETRIEVAL_K = 12
# 最终传入大模型的上下文片段数量
FINAL_CONTEXT_K = max(1, _get_int_config("FINAL_CONTEXT_K", 6))
# 向量召回数量
VECTOR_RECALL_K = max(RETRIEVAL_K, _get_int_config("VECTOR_RECALL_K", 20))
# BM25 召回数量
BM25_RECALL_K = max(RETRIEVAL_K, _get_int_config("BM25_RECALL_K", 20))
# RRF 融合后进入精排的候选数量
FUSION_TOP_K = max(FINAL_CONTEXT_K, _get_int_config("FUSION_TOP_K", 10))
# RRF 公式平滑参数
RRF_K = max(1, _get_int_config("RRF_K", 60))
# 是否启用父子双层索引的两阶段检索
ENABLE_PARENT_CHILD_RETRIEVAL = _get_bool_config("ENABLE_PARENT_CHILD_RETRIEVAL", True)
# 阶段一父文档召回数量
PARENT_RECALL_K = max(1, _get_int_config("PARENT_RECALL_K", 15))
# 是否启用上下文压缩
CONTEXT_COMPRESSION_ENABLED = _get_bool_config("CONTEXT_COMPRESSION_ENABLED", True)
# 触发上下文压缩的最小文本长度（字符数）
CONTEXT_COMPRESSION_MIN_CHARS = max(1, _get_int_config("CONTEXT_COMPRESSION_MIN_CHARS", 1500))
# 每个文档压缩后保留的句子数量
CONTEXT_COMPRESSION_SENTENCE_K = max(1, _get_int_config("CONTEXT_COMPRESSION_SENTENCE_K", 3))
# 是否启用 Elasticsearch BM25 召回
ES_ENABLED = _get_bool_config("ES_ENABLED", True)
ES_ENABLED_CONFIGURED = os.getenv("ES_ENABLED") is not None
# 是否允许 IK 分词器缺失时降级到 standard analyzer；默认阻断以避免中文 BM25 质量静默下降
ES_ALLOW_ANALYZER_FALLBACK = _get_bool_config("ES_ALLOW_ANALYZER_FALLBACK", False)
# 是否启用 BGE Reranker 精排
# 可直接修改这里；如需按环境覆盖，可设置 RERANKER_ENABLED=true/false
RERANKER_ENABLED = _get_bool_config("RERANKER_ENABLED", True)
# Reranker 粗排召回数量，默认放大到 15 以提升精排效果
# 未启用 Reranker 时回退到 RETRIEVAL_K，避免无意义放大召回
RERANK_CANDIDATE_K = (
	max(FUSION_TOP_K, _get_int_config("RERANK_CANDIDATE_K", FUSION_TOP_K))
	if RERANKER_ENABLED
	else FUSION_TOP_K
)
# Reranker 模型标识；本地目录存在时优先加载本地模型
RERANKER_MODEL_NAME = os.getenv(
	"RERANKER_MODEL_NAME",
	str(LOCAL_RERANKER_PATH) if LOCAL_RERANKER_PATH.exists() else "BAAI/bge-reranker-base",
)
# Reranker 推理设备
RERANKER_DEVICE = os.getenv("RERANKER_DEVICE", "cpu")
# Reranker 单次 CrossEncoder 预测批大小；CPU 下过大容易拉高尾延迟
RERANKER_BATCH_SIZE = max(1, _get_int_config("RERANKER_BATCH_SIZE", 8))

# ==================== 权限与分块配置 ====================

# 是否启用文档访问控制 ACL
# 可直接修改这里；如需按环境覆盖，可设置 ENABLE_ACL=true/false
ENABLE_ACL = _get_bool_config("ENABLE_ACL", False)
# 是否启用更细粒度的子分块切分
# 可直接修改这里；如需按环境覆盖，可设置 ENABLE_SUB_CHUNKING=true/false
ENABLE_SUB_CHUNKING = _get_bool_config("ENABLE_SUB_CHUNKING", False)

# ==================== 前端页面配置 ====================

# 前端静态页面目录
DASHBOARD_DIR = PROJECT_ROOT / "static"
# 前端首页文件
DASHBOARD_INDEX = DASHBOARD_DIR / "index.html"

# ==================== 服务端配置 ====================

# 是否启用 HTTPS
ENABLE_HTTPS = _get_bool_config("ENABLE_HTTPS", False)


def get_runtime_config() -> dict[str, object]:
	health = _build_runtime_health()

	return {
		"paths": {
			"project_root": str(PROJECT_ROOT),
			"docs_dir": str(DOCS_DIR),
			"default_knowledge_base": DEFAULT_KNOWLEDGE_BASE,
			"username_group_mapping_file": str(USERNAME_GROUP_MAPPING_FILE),
			"dashboard_dir": str(DASHBOARD_DIR),
			"dashboard_index": str(DASHBOARD_INDEX),
		},
		"server": {
			"enable_https": ENABLE_HTTPS,
		},
		"indexing": {
			"db_dir": str(DB_DIR),
			"embedding_model_path": str(LOCAL_MODEL_PATH),
			"embedding_model_name": EMBEDDING_MODEL_NAME,
			"embedding_device": EMBEDDING_DEVICE,
			"reranker_model_path": str(LOCAL_RERANKER_PATH),
			"es_enabled": ES_ENABLED,
			"es_enabled_configured": ES_ENABLED_CONFIGURED,
			"es_url": ES_URL,
			"es_index_name": ES_INDEX_NAME,
			"es_parent_index_name": ES_PARENT_INDEX_NAME,
			"es_allow_analyzer_fallback": ES_ALLOW_ANALYZER_FALLBACK,
		},
		"retrieval": {
			"retrieval_k": RETRIEVAL_K,
			"vector_recall_k": VECTOR_RECALL_K,
			"bm25_recall_k": BM25_RECALL_K,
			"fusion_top_k": FUSION_TOP_K,
			"rrf_k": RRF_K,
			"enable_parent_child_retrieval": ENABLE_PARENT_CHILD_RETRIEVAL,
			"parent_recall_k": PARENT_RECALL_K,
			"context_compression_enabled": CONTEXT_COMPRESSION_ENABLED,
			"context_compression_min_chars": CONTEXT_COMPRESSION_MIN_CHARS,
			"context_compression_sentence_k": CONTEXT_COMPRESSION_SENTENCE_K,
			"final_context_k": FINAL_CONTEXT_K,
			"reranker_enabled": RERANKER_ENABLED,
			"rerank_candidate_k": RERANK_CANDIDATE_K,
			"reranker_model_name": RERANKER_MODEL_NAME,
			"reranker_device": RERANKER_DEVICE,
			"reranker_batch_size": RERANKER_BATCH_SIZE,
			"print_logging_enabled": PRINT_LOGGING_ENABLED,
		},
		"access_control": {
			"enable_acl": ENABLE_ACL,
			"username_group_mapping_exists": USERNAME_GROUP_MAPPING_FILE.is_file(),
		},
		"chunking": {
			"enable_sub_chunking": ENABLE_SUB_CHUNKING,
		},
		"dashboard": {
			"dashboard_enabled": DASHBOARD_DIR.exists(),
			"dashboard_index_exists": DASHBOARD_INDEX.is_file(),
		},
		"config_sources": {
			"es_enabled": _get_config_source("ES_ENABLED"),
			"es_url": _get_config_source("ES_URL"),
			"es_index_name": _get_config_source("ES_INDEX_NAME"),
			"es_parent_index_name": _get_config_source("ES_PARENT_INDEX_NAME"),
			"es_allow_analyzer_fallback": _get_config_source("ES_ALLOW_ANALYZER_FALLBACK"),
			"db_dir": _get_config_source("DB_DIR"),
			"embedding_model_name": _get_config_source("EMBEDDING_MODEL_NAME"),
			"embedding_device": _get_config_source("EMBEDDING_DEVICE"),
			"enable_acl": _get_config_source("ENABLE_ACL"),
			"enable_sub_chunking": _get_config_source("ENABLE_SUB_CHUNKING"),
			"final_context_k": _get_config_source("FINAL_CONTEXT_K"),
			"vector_recall_k": _get_config_source("VECTOR_RECALL_K"),
			"bm25_recall_k": _get_config_source("BM25_RECALL_K"),
			"fusion_top_k": _get_config_source("FUSION_TOP_K"),
			"rrf_k": _get_config_source("RRF_K"),
			"enable_parent_child_retrieval": _get_config_source("ENABLE_PARENT_CHILD_RETRIEVAL"),
			"parent_recall_k": _get_config_source("PARENT_RECALL_K"),
			"context_compression_enabled": _get_config_source("CONTEXT_COMPRESSION_ENABLED"),
			"context_compression_min_chars": _get_config_source("CONTEXT_COMPRESSION_MIN_CHARS"),
			"context_compression_sentence_k": _get_config_source("CONTEXT_COMPRESSION_SENTENCE_K"),
			"reranker_enabled": _get_config_source("RERANKER_ENABLED"),
			"rerank_candidate_k": _get_config_source("RERANK_CANDIDATE_K"),
			"reranker_model_name": _get_config_source("RERANKER_MODEL_NAME"),
			"reranker_device": _get_config_source("RERANKER_DEVICE"),
			"reranker_batch_size": _get_config_source("RERANKER_BATCH_SIZE"),
			"print_logging_enabled": _get_config_source("PRINT_LOGGING_ENABLED"),
			"enable_https": _get_config_source("ENABLE_HTTPS"),
		},
		"health": health,
	}
