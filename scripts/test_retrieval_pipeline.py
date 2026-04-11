from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_backend_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


get_es_client = _load_backend_attr("es_service", "get_es_client")
ES_INDEX_NAME = _load_backend_attr("rag_config", "ES_INDEX_NAME")
vectorstore = _load_backend_attr("rag_store", "vectorstore")
run_retrieval_pipeline = _load_backend_attr("retrieval_pipeline_service", "run_retrieval_pipeline")


@dataclass
class EvaluationCase:
    name: str
    query: str
    username: str = "anonymous"
    domains: list[str] = field(default_factory=list)
    query_type: str = "semantic"
    expected_status: str = "success"
    expected_routed_domains: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    require_non_empty_context: bool = True
    require_bm25_hits: bool = False
    require_compression_hit: bool = False
    min_shared_fused_hits: int = 0


def _build_default_cases() -> list[EvaluationCase]:
    return [
        EvaluationCase(
            name="user-login-reference",
            query="前端调登录接口的时候，需要传哪些参数？如果密码输错了会返回什么错误码？",
            expected_routed_domains=["user-center"],
            expected_keywords=["手机号", "密码", "10003"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
        EvaluationCase(
            name="token-design-rationale",
            query="为什么我们系统要搞 Access Token 和 Refresh Token 两个 Token？只用一个不行吗？",
            expected_routed_domains=["user-center"],
            expected_keywords=["Access Token", "Refresh Token", "2 小时", "30 天"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
        EvaluationCase(
            name="order-cancel-status",
            query="订单处于什么状态的时候，才允许被取消？",
            expected_routed_domains=["order-center"],
            expected_keywords=["INIT", "CANCELED"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
        EvaluationCase(
            name="order-timeout-flow",
            query="如果用户下单了但一直不付款，这笔订单会怎么处理？会不会一直占着库存？",
            expected_routed_domains=["order-center"],
            expected_keywords=["15", "RocketMQ", "库存"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
        EvaluationCase(
            name="cross-module-payment",
            query="当用户在 App 里面点击提交订单后，到最终支付成功，这中间的系统调用链路是怎样的？状态是怎么变的？",
            expected_routed_domains=["global", "order-center", "payment-gateway"],
            expected_keywords=["INIT", "PAID", "支付"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
        EvaluationCase(
            name="login-permission-troubleshooting",
            query="用户登录失败，怎么排查权限问题？",
            domains=["user-center", "global"],
            query_type="semantic",
            expected_routed_domains=["user-center"],
            expected_keywords=["登录", "access_token", "refresh_token"],
            require_bm25_hits=True,
            min_shared_fused_hits=1,
        ),
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the hybrid retrieval pipeline and report stage-by-stage quality signals.",
    )
    parser.add_argument("--query", help="Run a single ad-hoc query instead of the built-in evaluation suite.")
    parser.add_argument("--username", default="anonymous", help="Username used for ACL-aware retrieval.")
    parser.add_argument("--case-file", help="Path to a JSON file containing evaluation cases.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Rebuild Chroma and Elasticsearch indexes before evaluation.",
    )
    return parser.parse_args()


def _load_cases(case_file: str | None, query: str | None, username: str) -> list[EvaluationCase]:
    if case_file:
        raw_cases = json.loads(Path(case_file).read_text(encoding="utf-8"))
        if not isinstance(raw_cases, list):
            raise ValueError("case file must be a JSON array")

        return [
            EvaluationCase(
                name=str(item.get("name") or f"case-{index + 1}"),
                query=str(item["query"]),
                username=str(item.get("username", username)),
                domains=list(item.get("domains", [])),
                query_type=str(item.get("query_type", "semantic")),
                expected_status=str(item.get("expected_status", "success")),
                expected_routed_domains=list(item.get("expected_routed_domains", item.get("expected_authorized_domains", []))),
                expected_keywords=list(item.get("expected_keywords", [])),
                require_non_empty_context=bool(item.get("require_non_empty_context", True)),
                require_bm25_hits=bool(item.get("require_bm25_hits", False)),
                require_compression_hit=bool(item.get("require_compression_hit", False)),
                min_shared_fused_hits=int(item.get("min_shared_fused_hits", 0)),
            )
            for index, item in enumerate(raw_cases)
        ]

    if query:
        return [
            EvaluationCase(
                name="ad-hoc",
                query=query,
                username=username,
                require_bm25_hits=True,
            )
        ]

    return _build_default_cases()


def _collect_index_health() -> dict[str, Any]:
    vector_count = int(vectorstore._collection.count())
    client = get_es_client()
    es_count = None
    es_available = client is not None
    if client is not None:
        if client.indices.exists(index=ES_INDEX_NAME):
            es_count = int(client.count(index=ES_INDEX_NAME).get("count", 0))
        else:
            es_count = 0

    return {
        "vector_count": vector_count,
        "es_available": es_available,
        "es_index_name": ES_INDEX_NAME,
        "es_count": es_count,
    }


def _document_summary(doc, stage: str) -> dict[str, Any]:
    metadata = doc.metadata or {}
    return {
        "stage": stage,
        "source_file": metadata.get("source_file", "unknown"),
        "domain": metadata.get("domain", "unknown"),
        "type": metadata.get("type", "unknown"),
        "retrieval_sources": list(metadata.get("retrieval_sources", [])),
        "vector_rank": metadata.get("vector_rank"),
        "bm25_rank": metadata.get("bm25_rank"),
        "rrf_score": metadata.get("rrf_score"),
        "reranker_score": metadata.get("reranker_score"),
        "context_compressed": metadata.get("context_compressed", False),
        "context_compression_mode": metadata.get("context_compression_mode", "none"),
        "char_count": len(doc.page_content),
        "preview": doc.page_content[:120].replace("\n", " "),
    }


def _collect_compression_modes(final_results) -> list[str]:
    modes = []
    for doc in final_results:
        mode = str(doc.metadata.get("context_compression_mode", "none"))
        if doc.metadata.get("context_compressed") is True and mode not in modes:
            modes.append(mode)

    return modes or ["none"]


def _count_compression_modes(final_results) -> dict[str, int]:
    mode_counts: dict[str, int] = {}
    for doc in final_results:
        if doc.metadata.get("context_compressed") is not True:
            continue

        mode = str(doc.metadata.get("context_compression_mode", "none"))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

    return mode_counts or {"none": 0}


def _evaluate_case(case: EvaluationCase) -> dict[str, Any]:
    pipeline_result = run_retrieval_pipeline(
        query=case.query,
        username=case.username,
        domains=case.domains,
        query_type=case.query_type,
    )
    response = pipeline_result["response"]
    trace = pipeline_result["trace"]
    status = pipeline_result["status"]
    context_entries = response.get("context", []) if isinstance(response, dict) else []
    context_text = "\n".join(
        entry.get("page_content", "") for entry in context_entries if isinstance(entry, dict)
    )
    matched_keywords = [keyword for keyword in case.expected_keywords if keyword.lower() in context_text.lower()]
    observed_final_domains = sorted({str(doc.metadata.get("domain", "unknown")) for doc in trace["final_results"]})
    routed_domains = response.get("routed_domains", [])
    expanded_routed_domains = response.get("expanded_routed_domains", [])
    routed_domain_check_field = "expanded_routed_domains" if len(case.expected_routed_domains) > 1 else "routed_domains"
    routed_domain_candidates = expanded_routed_domains if routed_domain_check_field == "expanded_routed_domains" else routed_domains

    checks = {
        "status": status == case.expected_status,
        "routed_domains": set(case.expected_routed_domains).issubset(set(routed_domain_candidates)),
        "context_non_empty": (len(context_entries) > 0) if case.require_non_empty_context else True,
        "bm25_hits": trace["metrics"]["bm25_hit_count"] > 0 if case.require_bm25_hits else True,
        "shared_fusion": trace["metrics"]["shared_fused_hit_count"] >= case.min_shared_fused_hits,
        "compression": trace["metrics"]["compression"]["compressed_doc_count"] > 0 if case.require_compression_hit else True,
        "keywords": len(matched_keywords) == len(case.expected_keywords),
    }
    passed = all(checks.values())

    return {
        "name": case.name,
        "query": case.query,
        "username": case.username,
        "status": status,
        "passed": passed,
        "checks": checks,
        "matched_keywords": matched_keywords,
        "missing_keywords": [keyword for keyword in case.expected_keywords if keyword not in matched_keywords],
        "routed_domains": routed_domains,
        "expanded_routed_domains": expanded_routed_domains,
        "routed_domain_check_field": routed_domain_check_field,
        "authorized_domains": response.get("authorized_domains", []),
        "observed_final_domains": observed_final_domains,
        "metrics": trace["metrics"],
        "compression_modes": _collect_compression_modes(trace["final_results"]),
        "compression_mode_counts": _count_compression_modes(trace["final_results"]),
        "stage_previews": {
            "parent": [_document_summary(doc, "parent") for doc in trace.get("parent_results", [])[:3]],
            "vector": [_document_summary(doc, "vector") for doc in trace["vector_results"][:3]],
            "bm25": [_document_summary(doc, "bm25") for doc in trace["bm25_results"][:3]],
            "fused": [_document_summary(doc, "fused") for doc in trace["fused_results"][:3]],
            "reranked": [_document_summary(doc, "reranked") for doc in trace["reranked_results"][:3]],
            "final": [_document_summary(doc, "final") for doc in trace["final_results"]],
        },
    }


def _summarize_results(results: list[dict[str, Any]], index_health: dict[str, Any]) -> dict[str, Any]:
    total_cases = len(results)
    passed_cases = sum(1 for result in results if result["passed"])
    total_compression_mode_counts: dict[str, int] = {}
    total_compressed_docs = 0
    total_final_docs = 0

    for result in results:
        metrics = result["metrics"]
        compression = metrics["compression"]
        total_compressed_docs += int(compression["compressed_doc_count"])
        total_final_docs += int(metrics["final_hit_count"])
        for mode, count in result["compression_mode_counts"].items():
            total_compression_mode_counts[mode] = total_compression_mode_counts.get(mode, 0) + int(count)

    return {
        "case_count": total_cases,
        "passed_count": passed_cases,
        "passed_ratio": round(passed_cases / total_cases, 4) if total_cases else 0.0,
        "index_health": index_health,
        "compression_mode_counts": total_compression_mode_counts or {"none": 0},
        "compressed_doc_count": total_compressed_docs,
        "final_doc_count": total_final_docs,
    }


def _print_human_report(index_health: dict[str, Any], results: list[dict[str, Any]]) -> None:
    print("=== Index Health ===")
    print(f"vector_count={index_health['vector_count']}")
    print(f"es_available={index_health['es_available']}")
    print(f"es_index_name={index_health['es_index_name']}")
    print(f"es_count={index_health['es_count']}")
    print()

    for result in results:
        metrics = result["metrics"]
        compression = metrics["compression"]
        print(f"=== Case: {result['name']} ===")
        print(f"status={'PASS' if result['passed'] else 'FAIL'} query={result['query']}")
        print(
            f"routed_domains={result['routed_domains']} expanded_routed_domains={result.get('expanded_routed_domains', [])} "
            f"routed_domain_check_field={result.get('routed_domain_check_field', 'routed_domains')} "
            f"authorized_domains={result['authorized_domains']} observed_final_domains={result['observed_final_domains']}"
        )
        print(
            "stage_hits="
            f"parent={metrics.get('parent_hit_count', 0)} "
            f"vector={metrics['vector_hit_count']} "
            f"bm25={metrics['bm25_hit_count']} "
            f"fused={metrics['fused_hit_count']} "
            f"reranked={metrics['reranked_hit_count']} "
            f"selected={metrics['selected_hit_count']} "
            f"final={metrics['final_hit_count']} "
            f"shared_fused={metrics['shared_fused_hit_count']} "
            f"two_stage={metrics.get('two_stage_applied', False)}"
        )
        print(
            "compression="
            f"compressed_docs={compression['compressed_doc_count']} "
            f"saved_chars={compression['saved_char_count']} "
            f"saved_ratio={compression['saved_ratio']:.2%} "
            f"sentence_limit={compression['sentence_limit']} "
            f"modes={result['compression_modes']}"
        )
        print(f"checks={result['checks']}")
        if result["matched_keywords"]:
            print(f"matched_keywords={result['matched_keywords']}")
        if result["missing_keywords"]:
            print(f"missing_keywords={result['missing_keywords']}")
        print("final_docs:")
        for doc in result["stage_previews"]["final"]:
            print(
                f"  - source={doc['source_file']} domain={doc['domain']} "
                f"sources={doc['retrieval_sources']} compressed={doc['context_compressed']} "
                f"mode={doc['context_compression_mode']} chars={doc['char_count']} preview={doc['preview']}"
            )
        print()

    passed_count = sum(1 for result in results if result["passed"])
    print(f"Summary: {passed_count}/{len(results)} cases passed")


def main() -> int:
    args = _parse_args()

    if args.rebuild_index:
        rebuild_index = _load_backend_attr("server", "rebuild_index")

        print("Rebuilding indexes before evaluation...")
        rebuild_result = rebuild_index()
        print(json.dumps(rebuild_result, ensure_ascii=False, indent=2))

    cases = _load_cases(args.case_file, args.query, args.username)
    index_health = _collect_index_health()
    results = [_evaluate_case(case) for case in cases]
    summary = _summarize_results(results, index_health)

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    else:
        _print_human_report(index_health, results)

    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())