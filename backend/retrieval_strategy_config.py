from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rag_config import BM25_RECALL_K, FUSION_TOP_K, VECTOR_RECALL_K


def _config_path() -> Path:
    configured_path = os.getenv("RETRIEVAL_ROUTING_CONFIG_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path(__file__).resolve().parent / "retrieval_routing_config.json"


def _config_path_source() -> str:
    return "env" if os.getenv("RETRIEVAL_ROUTING_CONFIG_PATH") else "default"


def _coerce_int(value: Any, fallback: int, minimum: int = 1) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, coerced)


def _coerce_float(value: Any, fallback: float, minimum: float = 0.0) -> float:
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, coerced)


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_string(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    cleaned = str(value).strip()
    return cleaned or fallback


def _build_default_query_types() -> dict[str, dict[str, Any]]:
    return {
        "exact": {
            "key": "exact",
            "label": "精确查询",
            "router_description": "精确查询，关键词、错误码、表名、字段名、参数名、枚举值等字面匹配很关键。",
            "execution_mode": "hybrid",
            "strategy": "bm25-priority",
            "vector_k": max(5, VECTOR_RECALL_K // 2),
            "bm25_k": max(BM25_RECALL_K, 30),
            "fusion_top_k": FUSION_TOP_K,
            "vector_weight": 0.5,
            "bm25_weight": 2.0,
            "enabled": True,
        },
        "semantic": {
            "key": "semantic",
            "label": "语义查询",
            "router_description": "语义查询，概念解释、流程说明、原因分析、操作指南等。",
            "execution_mode": "hybrid",
            "strategy": "semantic-priority",
            "vector_k": VECTOR_RECALL_K,
            "bm25_k": BM25_RECALL_K,
            "fusion_top_k": FUSION_TOP_K,
            "vector_weight": 2.0,
            "bm25_weight": 0.5,
            "enabled": True,
        },
        "comparative": {
            "key": "comparative",
            "label": "对比查询",
            "router_description": "对比查询，强调 A 和 B 的区别、优缺点、差异、对照。",
            "execution_mode": "hybrid",
            "strategy": "broad-dual-recall",
            "vector_k": max(VECTOR_RECALL_K, 30),
            "bm25_k": max(BM25_RECALL_K, 30),
            "fusion_top_k": max(FUSION_TOP_K, 15),
            "vector_weight": 1.3,
            "bm25_weight": 1.3,
            "enabled": True,
        },
        "factoid": {
            "key": "factoid",
            "label": "事实查询",
            "router_description": "事实查询，强调短答案、配置值、阈值、次数、时间、数字、是否条件等。",
            "execution_mode": "hybrid",
            "strategy": "fact-high-precision",
            "vector_k": max(10, VECTOR_RECALL_K // 2),
            "bm25_k": max(BM25_RECALL_K, 20),
            "fusion_top_k": FUSION_TOP_K,
            "vector_weight": 0.8,
            "bm25_weight": 1.6,
            "enabled": True,
        },
        "faq_lookup": {
            "key": "faq_lookup",
            "label": "FAQ 查询",
            "router_description": "FAQ 查询，适合标准问答、固定话术、常见问题和明确的标准答案。",
            "execution_mode": "faq_lookup",
            "strategy": "faq-only",
            "vector_k": max(10, VECTOR_RECALL_K // 2),
            "bm25_k": max(BM25_RECALL_K, 20),
            "fusion_top_k": min(FUSION_TOP_K, 8),
            "vector_weight": 1.2,
            "bm25_weight": 1.8,
            "enabled": True,
        },
        "sql_query": {
            "key": "sql_query",
            "label": "结构化查询",
            "router_description": "结构化查询，适合数据库字段、表结构、接口字段和强结构化参考信息。",
            "execution_mode": "sql_query",
            "strategy": "structured-reference",
            "vector_k": max(8, VECTOR_RECALL_K // 2),
            "bm25_k": max(BM25_RECALL_K, 16),
            "fusion_top_k": min(FUSION_TOP_K, 8),
            "vector_weight": 1.0,
            "bm25_weight": 2.2,
            "enabled": True,
        },
    }


def _load_raw_config() -> dict[str, Any]:
    config_path = _config_path()
    if not config_path.is_file():
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[routing-config] failed to load {config_path}: {exc}")
        return {}

    if not isinstance(payload, dict):
        print(f"[routing-config] ignore {config_path}: root value must be a JSON object")
        return {}
    return payload


def _semantic_fallback_definition(query_type: str, default_query_types: dict[str, dict[str, Any]]) -> dict[str, Any]:
    semantic_definition = dict(default_query_types.get("semantic", {}))
    semantic_definition["key"] = query_type
    semantic_definition["label"] = query_type
    semantic_definition["strategy"] = f"{query_type}-fallback"
    semantic_definition["router_description"] = f"{query_type} 类型查询。"
    return semantic_definition


def _normalize_query_type_definition(
    query_type: str,
    raw_definition: Any,
    default_definition: dict[str, Any],
) -> dict[str, Any]:
    candidate_definition = raw_definition if isinstance(raw_definition, dict) else {}

    return {
        "key": query_type,
        "label": _coerce_string(candidate_definition.get("label"), str(default_definition.get("label", query_type))),
        "router_description": _coerce_string(
            candidate_definition.get("router_description"),
            str(default_definition.get("router_description", f"{query_type} 类型查询。")),
        ),
        "execution_mode": _coerce_string(candidate_definition.get("execution_mode"), str(default_definition.get("execution_mode", "hybrid"))),
        "strategy": _coerce_string(candidate_definition.get("strategy"), str(default_definition.get("strategy", f"{query_type}-strategy"))),
        "vector_k": _coerce_int(candidate_definition.get("vector_k"), int(default_definition.get("vector_k", VECTOR_RECALL_K))),
        "bm25_k": _coerce_int(candidate_definition.get("bm25_k"), int(default_definition.get("bm25_k", BM25_RECALL_K))),
        "fusion_top_k": _coerce_int(candidate_definition.get("fusion_top_k"), int(default_definition.get("fusion_top_k", FUSION_TOP_K))),
        "vector_weight": _coerce_float(candidate_definition.get("vector_weight"), float(default_definition.get("vector_weight", 1.0))),
        "bm25_weight": _coerce_float(candidate_definition.get("bm25_weight"), float(default_definition.get("bm25_weight", 1.0))),
        "enabled": _coerce_bool(candidate_definition.get("enabled"), bool(default_definition.get("enabled", True))),
    }


def _load_routing_config() -> dict[str, Any]:
    config_path = _config_path()
    raw_config = _load_raw_config()
    default_query_types = _build_default_query_types()
    raw_query_types = raw_config.get("query_types", {})
    if not isinstance(raw_query_types, dict):
        raw_query_types = {}

    query_type_keys = list(default_query_types.keys()) + [
        key for key in raw_query_types.keys()
        if isinstance(key, str) and key not in default_query_types
    ]

    normalized_query_types: dict[str, dict[str, Any]] = {}
    for key in query_type_keys:
        if not isinstance(key, str):
            continue

        normalized_key = key.strip().lower()
        if not normalized_key:
            continue

        default_definition = default_query_types.get(normalized_key, _semantic_fallback_definition(normalized_key, default_query_types))
        normalized_definition = _normalize_query_type_definition(
            normalized_key,
            raw_query_types.get(key, raw_query_types.get(normalized_key, {})),
            default_definition,
        )
        if normalized_definition["enabled"]:
            normalized_query_types[normalized_key] = normalized_definition

    if not normalized_query_types:
        normalized_query_types = default_query_types

    raw_default_query_type = str(raw_config.get("default_query_type", "")).strip().lower()
    if raw_default_query_type in normalized_query_types:
        default_query_type = raw_default_query_type
    elif "semantic" in normalized_query_types:
        default_query_type = "semantic"
    else:
        default_query_type = next(iter(normalized_query_types.keys()))

    return {
        "config_path": str(config_path),
        "config_path_source": _config_path_source(),
        "loaded_from_file": config_path.is_file(),
        "default_query_type": default_query_type,
        "query_types": normalized_query_types,
    }


def get_default_query_type() -> str:
    config = _load_routing_config()
    return str(config["default_query_type"])


def normalize_query_type(query_type: str | None) -> str:
    config = _load_routing_config()
    normalized = str(query_type or "").strip().lower()
    if normalized in config["query_types"]:
        return normalized
    return str(config["default_query_type"])


def get_retrieval_strategy_plan(query_type: str | None) -> dict[str, Any]:
    config = _load_routing_config()
    normalized_query_type = normalize_query_type(query_type)
    plan = dict(config["query_types"][normalized_query_type])
    plan["query_type"] = normalized_query_type
    return plan


def get_routing_runtime_config() -> dict[str, Any]:
    config = _load_routing_config()
    return {
        "config_path": config["config_path"],
        "config_path_source": config["config_path_source"],
        "loaded_from_file": config["loaded_from_file"],
        "default_query_type": config["default_query_type"],
        "query_types": list(config["query_types"].values()),
    }