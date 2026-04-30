from __future__ import annotations

import argparse
import importlib
import json
import math
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "logs"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_backend_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


DOCS_DIR = _load_backend_attr("rag_config", "DOCS_DIR")
get_child_es_index_name = _load_backend_attr("knowledge_base_service", "get_child_es_index_name")
get_parent_es_index_name = _load_backend_attr("knowledge_base_service", "get_parent_es_index_name")
get_vectorstore = _load_backend_attr("rag_store", "get_vectorstore")
get_parent_vectorstore = _load_backend_attr("rag_store", "get_parent_vectorstore")
get_remote_retrieval_service = _load_backend_attr("remote_retrieval_service", "get_remote_retrieval_service")
rebuild_index = _load_backend_attr("server", "rebuild_index")


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    query: str
    expected_token: str
    expected_source_file: str
    expected_domain: str
    query_type: str = "semantic"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a realistic synthetic RAG corpus, rebuild indexes, and benchmark retrieval latency.",
    )
    parser.add_argument(
        "--chunk-targets",
        type=int,
        nargs="+",
        default=[1000],
        help="One or more target child-chunk counts. Multiple targets create separate benchmark knowledge bases.",
    )
    parser.add_argument("--knowledge-base-prefix", default="benchmark_perf", help="Prefix for generated benchmark knowledge bases.")
    parser.add_argument("--sections-per-file", type=int, default=40, help="Approximate child chunks generated per Markdown file.")
    parser.add_argument("--domain-count", type=int, default=12, help="Number of synthetic business domains.")
    parser.add_argument("--queries", type=int, default=24, help="Timed query cases per target.")
    parser.add_argument("--warmup", type=int, default=3, help="Warm-up queries per target before timed measurements.")
    parser.add_argument("--runs-per-query", type=int, default=1, help="Timed repetitions for each query case.")
    parser.add_argument("--top-k", type=int, default=None, help="Optional final context top_k override. Omit to use query_type routing config final_context_k.")
    parser.add_argument(
        "--query-types",
        nargs="+",
        default=None,
        help="Optional query_type cycle for pressure testing routing-specific candidate/final_k configs, e.g. exact semantic comparative factoid sql_query.",
    )
    parser.add_argument("--skip-generate", action="store_true", help="Reuse an existing generated corpus.")
    parser.add_argument("--no-rebuild", action="store_true", help="Skip index rebuild and benchmark current indexes.")
    parser.add_argument("--force", action="store_true", help="Allow overwriting non-benchmark knowledge-base directories.")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Directory for JSON and Markdown reports.")
    parser.add_argument("--report-prefix", default="retrieval_performance_benchmark", help="Report filename prefix.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report to stdout.")
    return parser.parse_args()


def _benchmark_knowledge_base_name(prefix: str, target_chunks: int, target_count: int) -> str:
    cleaned_prefix = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in prefix).strip("_-")
    cleaned_prefix = cleaned_prefix or "benchmark_perf"
    if target_count == 1:
        return cleaned_prefix
    return f"{cleaned_prefix}_{target_chunks}"


def _domain_name(domain_index: int) -> str:
    return f"bench-domain-{domain_index:02d}"


def _benchmark_token(domain_index: int, doc_index: int, section_index: int) -> str:
    return f"BENCH_{domain_index:02d}_{doc_index:05d}_{section_index:03d}"


def _section_text(domain: str, doc_index: int, section_index: int, token: str) -> str:
    endpoint = f"/api/benchmark/{domain}/case/{doc_index:05d}/{section_index:03d}"
    table_name = f"t_bench_{domain.replace('-', '_')}_{doc_index % 97:02d}"
    error_code = f"B{doc_index % 1000:03d}{section_index:03d}"
    field_name = f"metric_{section_index:03d}_value"
    return "\n".join(
        [
            f"## 场景 {section_index:03d} {token}",
            "",
            f"Benchmark 标识 `{token}` 属于 `{domain}` 模块，接口路径是 `{endpoint}`。",
            f"核心表 `{table_name}` 必须写入字段 `{field_name}`，并保留 trace_id、tenant_id 和 version。",
            f"如果校验失败，服务返回错误码 `{error_code}`，调用方需要记录审计日志并触发补偿任务。",
            f"排障时优先检查 `{token}`、`{field_name}`、`{table_name}` 三个关键词是否同时出现在上下文。",
            "",
        ]
    )


def _write_module_directory(kb_dir: Path, domain_count: int) -> None:
    lines = ["modules:"]
    for domain_index in range(domain_count):
        domain = _domain_name(domain_index)
        lines.extend(
            [
                f"  - name: {domain}",
                f"    summary: Synthetic benchmark module {domain} for retrieval latency measurement.",
                "    keywords:",
                f"      - {domain}",
                "      - benchmark",
                "      - performance",
            ]
        )
    (kb_dir / "module-directory.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _generate_corpus(
    knowledge_base: str,
    target_chunks: int,
    sections_per_file: int,
    domain_count: int,
    *,
    force: bool,
) -> dict[str, Any]:
    if sections_per_file <= 0:
        raise ValueError("sections_per_file must be positive")
    if domain_count <= 0:
        raise ValueError("domain_count must be positive")

    kb_dir = (DOCS_DIR / knowledge_base).resolve()
    if kb_dir.exists():
        if not knowledge_base.startswith("benchmark") and not force:
            raise ValueError(
                f"Refusing to overwrite non-benchmark docs directory: {kb_dir}. "
                "Use --force if this is intentional."
            )
        shutil.rmtree(kb_dir)

    kb_dir.mkdir(parents=True, exist_ok=True)
    _write_module_directory(kb_dir, domain_count)

    doc_count = max(1, math.ceil(target_chunks / sections_per_file))
    for doc_index in range(doc_count):
        domain_index = doc_index % domain_count
        domain = _domain_name(domain_index)
        domain_dir = kb_dir / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        tokens = [_benchmark_token(domain_index, doc_index, section_index) for section_index in range(1, sections_per_file + 1)]
        frontmatter_lines = [
            "---",
            f"title: Benchmark Document {doc_index:05d}",
            f"domain: {domain}",
            "type: reference",
            f"summary: Synthetic benchmark document {doc_index:05d} for {domain}. Tokens include {' '.join(tokens[:5])}.",
            "keywords:",
        ]
        frontmatter_lines.extend(f"  - {token}" for token in tokens)
        frontmatter_lines.extend(
            [
                f"  - {domain}",
                "  - benchmark",
                "related_domains:",
                f"  - {domain}",
                "  - global",
                "acl: public",
                "---",
                "",
                f"# Benchmark Document {doc_index:05d}",
                "",
                f"This generated document belongs to `{domain}` and is used only for retrieval performance benchmarking.",
                "",
            ]
        )
        body_lines = list(frontmatter_lines)
        for section_index, token in enumerate(tokens, start=1):
            body_lines.append(_section_text(domain, doc_index, section_index, token))
        file_path = domain_dir / f"benchmark-doc-{doc_index:05d}.md"
        file_path.write_text("\n".join(body_lines), encoding="utf-8")

    return {
        "knowledge_base": knowledge_base,
        "docs_dir": str(kb_dir),
        "target_chunks": target_chunks,
        "doc_count": doc_count,
        "sections_per_file": sections_per_file,
        "estimated_child_chunks": doc_count * sections_per_file,
        "domain_count": domain_count,
    }


def _build_cases(
    doc_count: int,
    sections_per_file: int,
    domain_count: int,
    query_count: int,
    query_types: list[str] | None,
) -> list[BenchmarkCase]:
    normalized_query_types = [str(query_type).strip() for query_type in query_types or [] if str(query_type).strip()]
    cases: list[BenchmarkCase] = []
    for case_index in range(query_count):
        doc_index = (case_index * 17) % doc_count
        section_index = ((case_index * 11) % sections_per_file) + 1
        domain_index = doc_index % domain_count
        domain = _domain_name(domain_index)
        token = _benchmark_token(domain_index, doc_index, section_index)
        source_file = f"{domain}/benchmark-doc-{doc_index:05d}.md"
        if normalized_query_types:
            query_type = normalized_query_types[case_index % len(normalized_query_types)]
        else:
            query_type = "exact" if case_index % 3 == 0 else "semantic"
        cases.append(
            BenchmarkCase(
                name=f"case-{case_index + 1:03d}",
                query=f"请定位 {token} 的接口、核心表、字段和错误码，并说明补偿处理要求。",
                expected_token=token,
                expected_source_file=source_file,
                expected_domain=domain,
                query_type=query_type,
            )
        )
    return cases


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return round(sorted_values[0], 6)
    position = (len(sorted_values) - 1) * percentile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return round(sorted_values[lower_index], 6)
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    interpolated = lower_value + (upper_value - lower_value) * (position - lower_index)
    return round(interpolated, 6)


def _summarize_values(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "avg": round(sum(values) / len(values), 6),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": round(max(values), 6),
    }


def _collect_index_health(knowledge_base: str) -> dict[str, Any]:
    child_index_name = get_child_es_index_name(knowledge_base)
    parent_index_name = get_parent_es_index_name(knowledge_base)
    runtime = get_remote_retrieval_service().get_es_runtime_config([child_index_name, parent_index_name])
    counts_by_name = runtime.get("document_counts_by_name", {}) if isinstance(runtime, dict) else {}
    return {
        "knowledge_base": knowledge_base,
        "vector_count": int(get_vectorstore(knowledge_base).count()),
        "parent_vector_count": int(get_parent_vectorstore(knowledge_base).count()),
        "es_available": bool(runtime.get("available")) if isinstance(runtime, dict) else False,
        "es_index_name": child_index_name,
        "es_count": counts_by_name.get(child_index_name) if isinstance(counts_by_name, dict) else None,
        "es_parent_index_name": parent_index_name,
        "es_parent_count": counts_by_name.get(parent_index_name) if isinstance(counts_by_name, dict) else None,
    }


def _run_warmup(service, knowledge_base: str, cases: list[BenchmarkCase], warmup_count: int, top_k: int | None) -> None:
    for case in cases[: max(0, warmup_count)]:
        service.run_retrieval_pipeline(
            query=case.query,
            username="anonymous",
            query_type=case.query_type,
            knowledge_base=knowledge_base,
            top_k=top_k,
            debug=True,
        )


def _run_case(service, knowledge_base: str, case: BenchmarkCase, top_k: int | None) -> dict[str, Any]:
    started = time.perf_counter()
    result = service.run_retrieval_pipeline(
        query=case.query,
        username="anonymous",
        query_type=case.query_type,
        knowledge_base=knowledge_base,
        top_k=top_k,
        debug=True,
    )
    client_seconds = round(time.perf_counter() - started, 6)
    trace = result.get("trace", {}) if isinstance(result, dict) else {}
    response = result.get("response", {}) if isinstance(result, dict) else {}
    metrics = trace.get("metrics", {}) if isinstance(trace, dict) else {}
    reranker_metrics = metrics.get("reranker", {}) if isinstance(metrics, dict) else {}
    context_entries = response.get("context", []) if isinstance(response, dict) else []
    context_text = "\n".join(
        str(entry.get("page_content", ""))
        for entry in context_entries
        if isinstance(entry, dict)
    )
    final_results = trace.get("final_results", []) if isinstance(trace, dict) else []
    final_sources = [str(doc.metadata.get("source_file", "")) for doc in final_results if hasattr(doc, "metadata")]
    matched_token = case.expected_token in context_text
    matched_source = case.expected_source_file in final_sources
    return {
        "case": case.name,
        "query_type": case.query_type,
        "expected_token": case.expected_token,
        "expected_source_file": case.expected_source_file,
        "expected_domain": case.expected_domain,
        "status": result.get("status") if isinstance(result, dict) else "unknown",
        "passed": bool(matched_token and matched_source),
        "matched_token": matched_token,
        "matched_source": matched_source,
        "client_seconds": client_seconds,
        "metrics": metrics,
        "timings": metrics.get("timings", {}) if isinstance(metrics, dict) else {},
        "reranker": reranker_metrics if isinstance(reranker_metrics, dict) else {},
        "vector_score_metrics": metrics.get("vector_score_metrics", {}) if isinstance(metrics, dict) else {},
        "parent_vector_score_metrics": metrics.get("parent_vector_score_metrics", {}) if isinstance(metrics, dict) else {},
        "final_sources": final_sources,
    }


def _summarize_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
    client_values = [float(item["client_seconds"]) for item in observations]
    timing_keys = sorted({key for item in observations for key in item.get("timings", {}).keys()})
    timing_summary = {
        key: _summarize_values([
            float(item.get("timings", {}).get(key))
            for item in observations
            if item.get("timings", {}).get(key) is not None
        ])
        for key in timing_keys
    }
    vector_score_avgs = [
        float(item.get("vector_score_metrics", {}).get("score_avg"))
        for item in observations
        if item.get("vector_score_metrics", {}).get("score_avg") is not None
    ]
    reranker_predict_seconds = [
        float(item.get("reranker", {}).get("predict_seconds"))
        for item in observations
        if item.get("reranker", {}).get("predict_seconds") is not None
    ]
    reranker_candidate_counts = [
        float(item.get("reranker", {}).get("candidate_count"))
        for item in observations
        if item.get("reranker", {}).get("candidate_count") is not None
    ]
    reranker_batch_counts = [
        float(item.get("reranker", {}).get("batch_count"))
        for item in observations
        if item.get("reranker", {}).get("batch_count") is not None
    ]
    reranker_failures = [
        item.get("reranker", {}).get("failure_reason")
        for item in observations
        if item.get("reranker", {}).get("failure_reason")
    ]
    return {
        "observation_count": len(observations),
        "passed_count": sum(1 for item in observations if item.get("passed")),
        "passed_ratio": round(sum(1 for item in observations if item.get("passed")) / len(observations), 4) if observations else 0.0,
        "client_seconds": _summarize_values(client_values),
        "stage_timings": timing_summary,
        "child_vector_score_avg": _summarize_values(vector_score_avgs),
        "reranker": {
            "predict_seconds": _summarize_values(reranker_predict_seconds),
            "candidate_count": _summarize_values(reranker_candidate_counts),
            "batch_count": _summarize_values(reranker_batch_counts),
            "failure_count": len(reranker_failures),
            "failure_reasons": sorted({str(reason) for reason in reranker_failures}),
        },
        "slowest_cases": sorted(
            [
                {
                    "case": item["case"],
                    "query_type": item["query_type"],
                    "client_seconds": item["client_seconds"],
                    "expected_source_file": item["expected_source_file"],
                    "passed": item["passed"],
                }
                for item in observations
            ],
            key=lambda item: item["client_seconds"],
            reverse=True,
        )[:10],
    }


def _benchmark_target(args: argparse.Namespace, target_chunks: int, target_count: int) -> dict[str, Any]:
    knowledge_base = _benchmark_knowledge_base_name(args.knowledge_base_prefix, target_chunks, target_count)
    if args.skip_generate:
        corpus = {
            "knowledge_base": knowledge_base,
            "docs_dir": str((DOCS_DIR / knowledge_base).resolve()),
            "target_chunks": target_chunks,
            "doc_count": max(1, math.ceil(target_chunks / args.sections_per_file)),
            "sections_per_file": args.sections_per_file,
            "estimated_child_chunks": target_chunks,
            "domain_count": args.domain_count,
            "skipped_generation": True,
        }
    else:
        corpus = _generate_corpus(
            knowledge_base=knowledge_base,
            target_chunks=target_chunks,
            sections_per_file=args.sections_per_file,
            domain_count=args.domain_count,
            force=args.force,
        )

    rebuild_seconds = None
    rebuild_result = None
    if not args.no_rebuild:
        rebuild_start = time.perf_counter()
        rebuild_result = rebuild_index(knowledge_base=knowledge_base)
        rebuild_seconds = round(time.perf_counter() - rebuild_start, 6)

    index_health = _collect_index_health(knowledge_base)
    doc_count = int(corpus["doc_count"])
    cases = _build_cases(doc_count, args.sections_per_file, args.domain_count, args.queries, args.query_types)
    service = get_remote_retrieval_service()
    _run_warmup(service, knowledge_base, cases, args.warmup, args.top_k)

    observations: list[dict[str, Any]] = []
    for case in cases:
        for _ in range(args.runs_per_query):
            observations.append(_run_case(service, knowledge_base, case, args.top_k))

    return {
        "knowledge_base": knowledge_base,
        "corpus": corpus,
        "rebuild_seconds": rebuild_seconds,
        "rebuild_result": rebuild_result,
        "index_health": index_health,
        "config": {
            "queries": args.queries,
            "warmup": args.warmup,
            "runs_per_query": args.runs_per_query,
            "top_k": args.top_k,
            "uses_routing_final_context_k": args.top_k is None,
            "query_types": args.query_types,
        },
        "summary": _summarize_observations(observations),
        "observations": observations,
    }


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Retrieval Performance Benchmark",
        "",
        f"Generated at: {report['generated_at']}",
        f"Status: {report['status']}",
        "",
    ]

    for target in report["targets"]:
        summary = target["summary"]
        client = summary["client_seconds"]
        reranker = summary["reranker"]
        reranker_predict = reranker["predict_seconds"]
        reranker_candidates = reranker["candidate_count"]
        index_health = target["index_health"]
        lines.extend(
            [
                f"## {target['knowledge_base']}",
                f"- Target chunks: {target['corpus']['target_chunks']}",
                f"- Estimated generated chunks: {target['corpus']['estimated_child_chunks']}",
                f"- Vector docs: {index_health['vector_count']}",
                f"- Parent vector docs: {index_health['parent_vector_count']}",
                f"- ES docs: {index_health['es_count']}",
                f"- Parent ES docs: {index_health['es_parent_count']}",
                f"- Rebuild seconds: {target['rebuild_seconds']}",
                f"- Query observations: {summary['observation_count']}",
                f"- Pass ratio: {summary['passed_count']}/{summary['observation_count']} ({summary['passed_ratio']:.2%})",
                f"- Client latency seconds: p50={client['p50']}, p95={client['p95']}, p99={client['p99']}, max={client['max']}",
                f"- Reranker predict seconds: avg={reranker_predict['avg']}, p95={reranker_predict['p95']}, max={reranker_predict['max']}",
                f"- Reranker candidates: avg={reranker_candidates['avg']}, max={reranker_candidates['max']}, failures={reranker['failure_count']}",
                f"- Child vector score avg: {summary['child_vector_score_avg']['avg']}",
                "",
                "### Stage Timings",
                "| stage | avg_s | p50_s | p95_s | p99_s | max_s |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for stage, values in summary["stage_timings"].items():
            lines.append(
                f"| {stage} | {values['avg']} | {values['p50']} | {values['p95']} | {values['p99']} | {values['max']} |"
            )
        lines.extend(["", "### Slowest Cases"])
        for item in summary["slowest_cases"]:
            lines.append(
                f"- {item['case']} {item['client_seconds']}s passed={item['passed']} source={item['expected_source_file']}"
            )
        lines.append("")

    return "\n".join(lines)


def _write_reports(report: dict[str, Any], report_dir: Path, report_prefix: str) -> dict[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"{report_prefix}.json"
    md_path = report_dir / f"{report_prefix}.md"
    timestamp_json_path = report_dir / f"{report_prefix}-{timestamp}.json"
    timestamp_md_path = report_dir / f"{report_prefix}-{timestamp}.md"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    markdown = _render_markdown_report(report)
    json_path.write_text(payload, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    timestamp_json_path.write_text(payload, encoding="utf-8")
    timestamp_md_path.write_text(markdown, encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "timestamped_json": str(timestamp_json_path),
        "timestamped_markdown": str(timestamp_md_path),
    }


def main() -> int:
    args = _parse_args()
    targets = []
    target_count = len(args.chunk_targets)
    for target_chunks in args.chunk_targets:
        print(f"=== Benchmark target: {target_chunks} child chunks ===")
        targets.append(_benchmark_target(args, int(target_chunks), target_count))

    status = "success" if all(target["summary"]["passed_count"] == target["summary"]["observation_count"] for target in targets) else "failure"
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "targets": targets,
    }
    report_paths = _write_reports(report, Path(args.report_dir), args.report_prefix)
    report["report_paths"] = report_paths

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown_report(report))
        print("Report artifacts:")
        for key, value in report_paths.items():
            print(f"  {key}: {value}")

    return 0 if status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())