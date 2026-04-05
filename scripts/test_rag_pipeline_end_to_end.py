from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import frontmatter


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "logs"

if str(BACKEND_DIR) not in sys.path:
	sys.path.insert(0, str(BACKEND_DIR))


def _load_backend_attr(module_name: str, attr_name: str):
	module = importlib.import_module(module_name)
	return getattr(module, attr_name)


DOCS_DIR = _load_backend_attr("rag_config", "DOCS_DIR")
ES_ENABLED = _load_backend_attr("rag_config", "ES_ENABLED")
ES_INDEX_NAME = _load_backend_attr("rag_config", "ES_INDEX_NAME")
ES_PARENT_INDEX_NAME = _load_backend_attr("rag_config", "ES_PARENT_INDEX_NAME")
list_docs_markdown_files = _load_backend_attr("documents_service", "list_docs_markdown_files")
run_retrieval_pipeline = _load_backend_attr("retrieval_pipeline_service", "run_retrieval_pipeline")
vectorstore = _load_backend_attr("rag_store", "vectorstore")
clear_vectorstore = _load_backend_attr("vectorstore_service", "clear_vectorstore")
get_chunk_statistics = _load_backend_attr("vectorstore_service", "get_chunk_statistics")
clear_es_index = _load_backend_attr("es_service", "clear_es_index")
get_es_client = _load_backend_attr("es_service", "get_es_client")
ingest_docs = _load_backend_attr("server", "ingest_docs")


@dataclass
class GeneratedCase:
	name: str
	query: str
	domains: list[str] = field(default_factory=list)
	username: str = "anonymous"
	expected_keywords: list[str] = field(default_factory=list)
	forbidden_keywords: list[str] = field(default_factory=list)
	expected_source_files: list[str] = field(default_factory=list)
	expected_retrieved_context: list[dict[str, str]] = field(default_factory=list)
	min_distinct_sources: int = 0
	expect_context_non_empty: bool = True
	note: str = ""


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Generate test questions from docs, rebuild the RAG indexes, run retrieval checks, and write a report.",
	)
	parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Directory where reports will be written.")
	parser.add_argument(
		"--report-prefix",
		default="rag_pipeline_end_to_end",
		help="Base file name used for report artifacts.",
	)
	parser.add_argument("--username", default="anonymous", help="Username used for ACL-aware retrieval.")
	parser.add_argument("--limit", type=int, default=0, help="Limit the number of generated test cases.")
	parser.add_argument("--json", action="store_true", help="Print the final report as JSON to stdout.")
	parser.add_argument("--no-rebuild", action="store_true", help="Skip clearing and rebuilding the indexes.")
	parser.add_argument(
		"--augment-arxiv",
		action="store_true",
		help="Download and build the arXiv survey corpus before rebuilding the indexes if you need a larger test set.",
	)
	return parser.parse_args()


def _cleanup_previous_report_artifacts(report_dir: Path, report_prefix: str) -> list[str]:
	removed_paths: list[str] = []
	for path in report_dir.iterdir():
		if not path.is_file():
			continue
		if path.name == "elasticsearch.log":
			continue
		if path.suffix not in {".json", ".md", ".log"}:
			continue
		path.unlink()
		removed_paths.append(str(path))
	return sorted(removed_paths)


def _relative_source_file(path: Path) -> str:
	return str(path.resolve().relative_to(DOCS_DIR.resolve())).replace("\\", "/")


@lru_cache(maxsize=1)
def _source_file_domain_map() -> dict[str, str]:
	mapping: dict[str, str] = {}
	for path in list_docs_markdown_files():
		metadata, _ = _load_doc(path)
		mapping[_relative_source_file(path)] = str(metadata.get("domain", "unknown"))
	return mapping


def _source_file_domain(source_file: str) -> str:
	return _source_file_domain_map().get(source_file, "unknown")


def _build_source_records(source_files: list[str], matched_source_files: set[str] | None = None) -> list[dict[str, Any]]:
	records: list[dict[str, Any]] = []
	seen: set[str] = set()
	for source_file in source_files:
		normalized_source_file = str(source_file).strip()
		if not normalized_source_file or normalized_source_file in seen:
			continue
		seen.add(normalized_source_file)
		record: dict[str, Any] = {
			"source_file": normalized_source_file,
			"domain": _source_file_domain(normalized_source_file),
		}
		if matched_source_files is not None:
			record["matched"] = normalized_source_file in matched_source_files
		records.append(record)
	return records


def _load_doc(path: Path) -> tuple[dict[str, Any], str]:
	with open(path, "r", encoding="utf-8") as handle:
		post = frontmatter.load(handle)
	metadata = dict(post.metadata) if isinstance(post.metadata, dict) else {}
	return metadata, str(post.content)


def _contains_all(text: str, keywords: list[str]) -> bool:
	normalized = text.lower()
	return all(keyword.lower() in normalized for keyword in keywords)


def _contains_any(text: str, keywords: list[str]) -> bool:
	normalized = text.lower()
	return any(keyword.lower() in normalized for keyword in keywords)


def _expected_chunk(source_file: str, chunk_text: str) -> dict[str, str]:
	return {
		"source_file": source_file,
		"chunk_text": chunk_text,
	}


def _build_readme_case_catalog() -> list[GeneratedCase]:
	return [
		GeneratedCase(
			name="readme-01-user-login-params",
			query="前端调登录接口的时候，需要传哪些参数？如果密码输错了会返回什么错误码？",
			domains=["user-center"],
			expected_keywords=["phone", "password", "10003"],
			expected_source_files=["user-center/reference-api.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"user-center/reference-api.md",
					"### 请求参数 (Request Body)\n| 字段名 | 类型 | 必填 | 说明 |\n|---|---|---|---|\n| phone | String | 是 | 11位手机号 |\n| password | String | 是 | 明文密码（前端需做基础校验） |\n\n### 业务错误码字典\n* `10003`: 密码错误",
				),
			],
			note="README 测试问 1。",
		),
		GeneratedCase(
			name="readme-02-order-amount-field",
			query="订单表里的金额字段叫什么？它的数据类型和单位是什么？",
			domains=["order-center"],
			expected_keywords=["total_amount", "INT", "分"],
			expected_source_files=["order-center/reference-db.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"order-center/reference-db.md",
					"## 1. 核心订单主表 (t_order)\n本表存储订单的主干信息。由于数据量庞大，按 `user_id` 进行分库分表。\n\n| 字段名 | 数据类型 | 约束 | 业务说明 |\n|---|---|---|---|\n| order_no | VARCHAR(32) | 主键 | 订单号，使用雪花算法生成 |\n| user_id | BIGINT | 非空, 索引 | 下单用户的全网唯一ID |\n| total_amount | INT | 非空 | 订单最终支付总金额，**单位统一为分** |\n| order_status | VARCHAR(16) | 非空 | 当前订单状态，见状态枚举字典 |\n| created_at | DATETIME | 非空 | 订单落库时间 |",
				),
			],
			note="README 测试问 2。",
		),
		GeneratedCase(
			name="readme-03-order-cancel-status",
			query="订单处于什么状态的时候，才允许被取消？",
			domains=["order-center"],
			expected_keywords=["INIT", "CANCELED"],
			expected_source_files=["order-center/faq.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"order-center/faq.md",
					"[FAQ: true] [类型: faq] 【用户常问】：订单处于什么状态时才允许取消？ 【标准解答】： 只有处于 `INIT`（初始化 / 待支付）状态的订单才允许取消。 取消成功后，订单状态会流转为 `CANCELED`。",
				),
			],
			note="README 测试问 3。",
		),
		GeneratedCase(
			name="readme-04-token-rationale",
			query="为什么我们系统要搞 Access Token 和 Refresh Token 两个 Token？只用一个不行吗？",
			domains=["user-center"],
			expected_keywords=["Access Token", "Refresh Token", "2 小时", "30 天"],
			expected_source_files=["user-center/explanation.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"user-center/explanation.md",
					"## 2. Token 生命周期\n* **Access Token**: 有效期为 2 小时。用于请求各个微服务的业务接口。\n* **Refresh Token**: 有效期为 30 天。仅用于向【用户中心】换取新的 Access Token。",
				),
			],
			note="README 测试问 4。",
		),
		GeneratedCase(
			name="readme-05-order-id-policy",
			query="后端在写创建订单接口的时候，订单号可以直接用数据库的自增 ID 吗？为什么？",
			domains=["order-center"],
			expected_keywords=["自增 ID", "IdGenerator.nextSnowflakeId", "雪花算法"],
			expected_source_files=["order-center/howto-create.md", "order-center/faq.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"order-center/howto-create.md",
					"## 1. 生成唯一订单号\n调用基础设施层的分布式 ID 生成服务 `IdGenerator.nextSnowflakeId()` 获取一个 64 位的订单号。绝对不要使用数据库自增 ID 以防泄露商业数据。",
				),
			],
			note="README 测试问 5。",
		),
		GeneratedCase(
			name="readme-06-order-timeout-flow",
			query="如果用户下单了但一直不付款，这笔订单会怎么处理？会不会一直占着库存？",
			domains=["order-center"],
			expected_keywords=["15 分钟", "RocketMQ", "库存"],
			expected_source_files=["order-center/explanation.md", "order-center/faq.md", "order-center/howto-create.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"order-center/explanation.md",
					"## 2. 支付超时防悬挂机制\n电商场景下，库存极其珍贵。如果用户下单后迟迟不付款，会导致库存被恶意锁定。\n因此，【订单中心】规定：任何一笔处于 `INIT`（待支付）状态的普通订单，如果在创建后的 15 分钟内未收到支付成功回调，系统将触发“超时取消”机制，自动关闭订单并释放底层库存。",
				),
			],
			note="README 测试问 6。",
		),
		GeneratedCase(
			name="readme-07-cross-module-checkout",
			query="当用户在 App 里面点击提交订单后，到最终支付成功，这中间的系统调用链路是怎样的？状态是怎么变的？",
			domains=["global", "order-center", "payment-gateway"],
			expected_keywords=["INIT", "PAID", "PaymentSuccessEvent", "订单中心", "支付网关"],
			expected_source_files=["global-workflows/howto-checkout.md", "payment-gateway/faq-payment.md", "order-center/explanation.md"],
			min_distinct_sources=2,
			expected_retrieved_context=[
				_expected_chunk(
					"global-workflows/howto-checkout.md",
					"## 4. 异步状态流转 (消息流转)\n当用户支付成功后，【支付网关】会收到微信的异步回调。此时支付网关必须发出 `PaymentSuccessEvent` 的 MQ 消息。\n【订单中心】监听到该消息后，负责将 `t_order` 表中的状态从 `INIT` 修改为 `PAID`，完成订单闭环。",
				),
			],
			note="README 测试问 7。",
		),
		GeneratedCase(
			name="readme-08-401-refresh-order-request",
			query="如果前端请求下单接口的时候，报了 401 错误，前端应该怎么做？需要让用户重新输入账号密码吗？",
			domains=["user-center", "order-center"],
			expected_keywords=["401 Unauthorized", "/api/v1/user/refresh", "重新发起", "Access Token"],
			expected_source_files=["user-center/howto-login.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"user-center/howto-login.md",
					"## 4. 处理 Token 过期 (无感刷新)\n如果业务接口返回 HTTP 状态码 `401 Unauthorized`，说明 Access Token 已过期。前端需静默调用 `/api/v1/user/refresh` 接口换取新 Token，并重新发起刚才失败的业务请求。",
				),
			],
			note="README 测试问 8。",
		),
		GeneratedCase(
			name="readme-09-no-email-field",
			query="登录接口里面，email 字段是必填的吗？",
			domains=["user-center"],
			expected_keywords=["phone", "password"],
			forbidden_keywords=["email"],
			expected_source_files=["user-center/reference-api.md"],
			expected_retrieved_context=[
				_expected_chunk(
					"user-center/reference-api.md",
					"### 请求参数 (Request Body)\n| 字段名 | 类型 | 必填 | 说明 |\n|---|---|---|---|\n| phone | String | 是 | 11位手机号 |\n| password | String | 是 | 明文密码（前端需做基础校验） |",
				),
			],
			note="README 测试问 9。",
		),
		GeneratedCase(
			name="readme-10-no-shipping-api-doc",
			query="商家发货后，怎么对接顺丰快递的 API 获取物流单号？",
			forbidden_keywords=["顺丰", "物流单号", "快递 API"],
			expect_context_non_empty=False,
			note="README 测试问 10。该用例主要检查检索结果里没有伪造的物流对接证据；最终生成层的诚实回答仍需结合 Copilot 输出人工确认。",
		),
	]


def _build_case_catalog() -> list[GeneratedCase]:
	cases: list[GeneratedCase] = []

	for path in list_docs_markdown_files():
		source_file = _relative_source_file(path)
		metadata, content = _load_doc(path)
		combined_text = f"{source_file}\n{metadata}\n{content}"

		if source_file == "user-center/reference-api.md" and _contains_all(combined_text, ["/api/v1/user/login", "10003", "phone", "password"]):
			cases.append(
				GeneratedCase(
					name="user-login-api-params",
					query="用户中心 API 参考手册里，POST /api/v1/user/login 的 Request Body 有哪些字段？phone 和 password 是否必填？密码错误会返回什么错误码？",
					domains=["user-center"],
					expected_keywords=["phone", "password", "10003"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"### 请求参数 (Request Body)\n| 字段名 | 类型 | 必填 | 说明 |\n|---|---|---|---|\n| phone | String | 是 | 11位手机号 |\n| password | String | 是 | 明文密码（前端需做基础校验） |\n\n### 业务错误码字典\n* `10003`: 密码错误",
						),
					],
					note="从用户中心登录接口参考手册自动生成。",
				),
			)

		elif source_file == "user-center/explanation.md" and _contains_all(combined_text, ["Access Token", "Refresh Token", "2 小时", "30 天"]):
			cases.append(
				GeneratedCase(
					name="user-token-lifecycle",
					query="为什么要用 Access Token 和 Refresh Token 双 Token 机制？它们的有效期分别是多少？",
					domains=["user-center"],
					expected_keywords=["Access Token", "Refresh Token", "2 小时", "30 天"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 2. Token 生命周期\n* **Access Token**: 有效期为 2 小时。用于请求各个微服务的业务接口。\n* **Refresh Token**: 有效期为 30 天。仅用于向【用户中心】换取新的 Access Token。",
						),
					],
					note="从用户认证系统说明自动生成。",
				),
			)

		elif source_file == "user-center/howto-login.md" and _contains_all(combined_text, ["401 Unauthorized", "/api/v1/user/refresh", "LocalStorage", "HttpOnly Cookie"]):
			cases.append(
				GeneratedCase(
					name="user-login-refresh-flow",
					query="前端收到 401 Unauthorized 后应该怎么处理？Token 应该存到 LocalStorage 吗？",
					domains=["user-center"],
					expected_keywords=["401 Unauthorized", "/api/v1/user/refresh", "LocalStorage", "HttpOnly Cookie"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 2. 存储 Token 数据\n登录成功后，前端必须将返回的 `access_token` 和 `refresh_token` 存储在本地安全的存储中（例如浏览器的 HttpOnly Cookie 或 App 的安全沙箱中，严禁存在 LocalStorage 以防 XSS 攻击）。\n\n## 4. 处理 Token 过期 (无感刷新)\n如果业务接口返回 HTTP 状态码 `401 Unauthorized`，说明 Access Token 已过期。前端需静默调用 `/api/v1/user/refresh` 接口换取新 Token，并重新发起刚才失败的业务请求。",
						),
					],
					note="从前端标准化登录指南自动生成。",
				),
			)

		elif source_file == "order-center/reference-db.md" and _contains_all(combined_text, ["total_amount", "order_status", "INIT", "PAID", "CANCELED"]):
			cases.append(
				GeneratedCase(
					name="order-db-schema",
					query="t_order 里的 total_amount 字段是什么类型和单位？order_status 字段是什么类型？",
					domains=["order-center"],
					expected_keywords=["total_amount", "INT", "分", "order_status"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 1. 核心订单主表 (t_order)\n本表存储订单的主干信息。由于数据量庞大，按 `user_id` 进行分库分表。\n\n| 字段名 | 数据类型 | 约束 | 业务说明 |\n|---|---|---|---|\n| order_no | VARCHAR(32) | 主键 | 订单号，使用雪花算法生成 |\n| user_id | BIGINT | 非空, 索引 | 下单用户的全网唯一ID |\n| total_amount | INT | 非空 | 订单最终支付总金额，**单位统一为分** |\n| order_status | VARCHAR(16) | 非空 | 当前订单状态，见状态枚举字典 |\n| created_at | DATETIME | 非空 | 订单落库时间 |",
						),
					],
					note="从订单数据库设计规范自动生成。",
				),
			)

		elif source_file == "order-center/howto-create.md" and _contains_all(combined_text, ["IdGenerator.nextSnowflakeId", "order_delay_topic", "15 分钟", "INIT"]):
			cases.append(
				GeneratedCase(
					name="order-create-implementation",
					query="后端创建订单时为什么不能用数据库自增 ID？延迟 MQ 要发多久？",
					domains=["order-center"],
					expected_keywords=["IdGenerator.nextSnowflakeId", "自增 ID", "RocketMQ", "15 分钟", "INIT"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 1. 生成唯一订单号\n调用基础设施层的分布式 ID 生成服务 `IdGenerator.nextSnowflakeId()` 获取一个 64 位的订单号。绝对不要使用数据库自增 ID 以防泄露商业数据。\n\n## 3. 执行本地事务落库\n开启数据库事务 `@Transactional`，向 `t_order` 表插入一条记录。此时 `order_status` 必须硬编码设置为 `INIT`。\n\n## 4. 发送延迟 MQ 消息\n事务提交成功后，必须向 RocketMQ 的 `order_delay_topic` 发送一条延迟 15 分钟的消息。该消息用于触发“超时未支付自动取消”的死信检查逻辑。",
						),
					],
					note="从订单落库实施指南自动生成。",
				),
			)

		elif source_file == "order-center/explanation.md" and _contains_all(combined_text, ["15 分钟", "INIT", "超时取消", "释放底层库存"]):
			cases.append(
				GeneratedCase(
					name="order-timeout-cancel",
					query="订单处于什么状态时会在多久后自动取消？取消后会发生什么？",
					domains=["order-center"],
					expected_keywords=["15 分钟", "INIT", "超时取消", "释放底层库存"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 2. 支付超时防悬挂机制\n电商场景下，库存极其珍贵。如果用户下单后迟迟不付款，会导致库存被恶意锁定。\n因此，【订单中心】规定：任何一笔处于 `INIT`（待支付）状态的普通订单，如果在创建后的 15 分钟内未收到支付成功回调，系统将触发“超时取消”机制，自动关闭订单并释放底层库存。",
						),
					],
					note="从订单核心领域模型说明自动生成。",
				),
			)

		elif source_file == "order-center/faq.md" and _contains_any(combined_text, ["CANCELED", "15 分钟", "订单号生成", "超时未支付"]):
			cases.append(
				GeneratedCase(
					name="order-faq-cancel-status",
					query="订单在什么状态时可以取消？",
					domains=["order-center"],
					expected_keywords=["INIT", "CANCELED"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"[FAQ: true] [类型: faq] 【用户常问】：订单处于什么状态时才允许取消？ 【标准解答】： 只有处于 `INIT`（初始化 / 待支付）状态的订单才允许取消。 取消成功后，订单状态会流转为 `CANCELED`。",
						),
					],
					note="从订单 FAQ 自动生成。",
				),
			)

		elif source_file == "payment-gateway/faq-payment.md" and _contains_any(combined_text, ["SIGN_ERROR", "notify_status", "Redis 分布式锁"]):
			cases.append(
				GeneratedCase(
					name="payment-faq-sign-error",
					query="支付网关出现 SIGN_ERROR 签名错误时应该怎么排查？退款能并发调用吗？",
					domains=["payment-gateway"],
					expected_keywords=["SIGN_ERROR", "ASCII", "Redis 分布式锁"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"[FAQ: true] [类型: faq] 【用户常问】：调用统一下单接口时，报 `SIGN_ERROR` 签名错误怎么办？ 【标准解答】： 签名错误 90% 是因为参数排序不对。请确保传入的 JSON 参数在生成 MD5 之前，已经按照 ASCII 码从小到大进行了字典序排序，并且最后拼接了 `&key=商户秘钥`。\n\n[FAQ: true] [类型: faq] 【用户常问】：退款接口可以并发调用吗？ 【标准解答】： 绝对不行。同一个 `order_no` 的退款请求必须串行。并发调用会导致微信底层重复扣款，引发极高的客诉风险。请在调用前加上 Redis 分布式锁。",
						),
					],
					note="从支付网关 FAQ 自动生成。",
				),
			)

		elif source_file == "global-workflows/howto-checkout.md" and _contains_all(combined_text, ["PaymentSuccessEvent", "INIT", "PAID", "订单中心"]):
			cases.append(
				GeneratedCase(
					name="global-checkout-flow",
					query="用户提交订单并支付成功后，订单状态是怎么流转的？涉及哪些系统？",
					domains=[],
					expected_keywords=["INIT", "PAID", "PaymentSuccessEvent", "订单中心", "支付网关"],
					expected_source_files=[source_file, "order-center/howto-create.md", "order-center/explanation.md", "payment-gateway/faq-payment.md"],
					min_distinct_sources=2,
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"## 4. 异步状态流转 (消息流转)\n当用户支付成功后，【支付网关】会收到微信的异步回调。此时支付网关必须发出 `PaymentSuccessEvent` 的 MQ 消息。\n【订单中心】监听到该消息后，负责将 `t_order` 表中的状态从 `INIT` 修改为 `PAID`，完成订单闭环。",
						),
						_expected_chunk(
							"order-center/explanation.md",
							"## 1. 订单状态机概念\n在【订单中心】中，订单的生命周期是单向流转的。为了防止并发修改导致的状态错乱，任何状态的流转（如从未支付到已支付）都必须通过乐观锁（基于 `version` 字段）进行控制。",
						),
					],
					note="从全局下单链路自动生成，保留跨模块检索压力。",
				),
			)

		elif source_file == "global-workflows/demo.md" and _contains_any(combined_text, ["RAG", "ROI", "时序图", "知识治理"]):
			cases.append(
				GeneratedCase(
					name="global-rag-demo",
					query="这份演示稿主要想解决什么 RAG 痛点？它强调了哪些架构和知识治理能力？",
					domains=["global"],
					expected_keywords=["RAG", "知识治理", "架构", "时序图"],
					expected_source_files=[source_file],
					expected_retrieved_context=[
						_expected_chunk(
							source_file,
							"* **三个核心痛点（Bullet Points）：** 1. **大杂烩文档（Garbage In）：** 架构说明、API 字典、操作步骤混在一起，切片后语义支离破碎。 2. **跨模块幻觉（Cross-module Hallucination）：** 向量检索无法区分相似概念，AI 容易精神分裂。 3. **检索结果不可解释（Black-box Retrieval）：** 没有可靠的证据链，开发者不知道答案是怎么来的。",
						),
					],
					note="从 RAG 演示稿自动生成。",
				),
			)

	cases.extend(_build_readme_case_catalog())
	return cases


def _maybe_augment_corpus(enable: bool) -> dict[str, Any]:
	if not enable:
		return {"enabled": False, "message": "not requested"}

	module = importlib.import_module("build_arxiv_surveys_corpus")
	module.main()
	return {"enabled": True, "message": "arXiv survey corpus was generated"}


def _collect_index_health() -> dict[str, Any]:
	vector_count = int(vectorstore._collection.count())
	chunk_stats = get_chunk_statistics(vectorstore)

	es_available = False
	es_count = None
	parent_es_count = None
	es_error = None
	if ES_ENABLED:
		try:
			client = get_es_client()
			es_available = client is not None
			if client is not None and client.indices.exists(index=ES_INDEX_NAME):
				es_count = int(client.count(index=ES_INDEX_NAME).get("count", 0))
			elif client is not None:
				es_count = 0
			if client is not None and client.indices.exists(index=ES_PARENT_INDEX_NAME):
				parent_es_count = int(client.count(index=ES_PARENT_INDEX_NAME).get("count", 0))
			elif client is not None:
				parent_es_count = 0
		except Exception as exc:
			es_error = str(exc)

	return {
		"vector_count": vector_count,
		"es_enabled": ES_ENABLED,
		"es_available": es_available,
		"es_index_name": ES_INDEX_NAME,
		"es_count": es_count,
		"es_parent_index_name": ES_PARENT_INDEX_NAME,
		"es_parent_count": parent_es_count,
		"es_error": es_error,
		"chunk_stats": chunk_stats,
	}


def _clear_and_rebuild_indexes() -> dict[str, Any]:
	deleted_vector_count = clear_vectorstore(vectorstore)
	deleted_parent_es_count = 0
	deleted_es_count = None
	es_error = None

	if ES_ENABLED:
		try:
			deleted_parent_es_count = clear_es_index(index_name=ES_PARENT_INDEX_NAME)
			deleted_es_count = clear_es_index(index_name=ES_INDEX_NAME)
		except Exception as exc:
			es_error = str(exc)
			raise
	else:
		deleted_parent_es_count = 0
		deleted_es_count = 0

	ingest_result = ingest_docs()
	post_ingest_health = _collect_index_health()

	return {
		"deleted_vector_count": deleted_vector_count,
		"deleted_parent_es_count": deleted_parent_es_count,
		"deleted_es_count": deleted_es_count,
		"es_error": es_error,
		"ingest_result": ingest_result,
		"post_ingest_health": post_ingest_health,
	}


def _extract_context_sources(context_entries: list[dict[str, Any]]) -> list[str]:
	sources: list[str] = []
	for entry in context_entries:
		metadata = entry.get("metadata", {})
		source_file = str(metadata.get("source_file", "")).strip()
		if source_file and source_file not in sources:
			sources.append(source_file)
	return sources


def _extract_context_text(context_entries: list[dict[str, Any]]) -> str:
	parts: list[str] = []
	for entry in context_entries:
		page_content = entry.get("page_content", "")
		if page_content:
			parts.append(str(page_content))
	return "\n".join(parts)


def _tokenize_match_text(text: str) -> set[str]:
	return {
		token.strip().lower()
		for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}", text)
		if token.strip()
	}


def _aggregate_context_text_by_source(context_entries: list[dict[str, Any]]) -> dict[str, str]:
	aggregated: dict[str, list[str]] = {}
	for entry in context_entries:
		metadata = entry.get("metadata", {}) if isinstance(entry, dict) else {}
		source_file = str(metadata.get("source_file", "")).strip()
		page_content = str(entry.get("page_content", "")).strip()
		if not source_file or not page_content:
			continue
		aggregated.setdefault(source_file, []).append(page_content)
	return {source_file: "\n".join(parts) for source_file, parts in aggregated.items()}


def _summarize_document_results(documents: list[Any], preview_length: int = 120) -> list[dict[str, Any]]:
	return [
		{
			"source_file": doc.metadata.get("source_file", "unknown"),
			"domain": doc.metadata.get("domain", "unknown"),
			"parent_title": doc.metadata.get("parent_title", ""),
			"rrf_score": doc.metadata.get("rrf_score"),
			"vector_rank": doc.metadata.get("vector_rank"),
			"bm25_rank": doc.metadata.get("bm25_rank"),
			"preview": doc.page_content[:preview_length].replace("\n", " "),
		}
		for doc in documents
	]


def _matches_expected_context(expected_text: str, actual_text: str) -> bool:
	normalized_expected = " ".join(expected_text.split()).lower()
	normalized_actual = " ".join(actual_text.split()).lower()
	if not normalized_expected or not normalized_actual:
		return False

	if normalized_expected in normalized_actual or normalized_actual in normalized_expected:
		return True

	expected_tokens = _tokenize_match_text(expected_text)
	actual_tokens = _tokenize_match_text(actual_text)
	if not expected_tokens or not actual_tokens:
		return False

	overlap_count = len(expected_tokens & actual_tokens)
	required_overlap = max(2, min(4, len(expected_tokens) // 3 + 1))
	return overlap_count >= required_overlap


def _evaluate_case(case: GeneratedCase) -> dict[str, Any]:
	pipeline_result = run_retrieval_pipeline(
		query=case.query,
		username=case.username,
	)
	response = pipeline_result["response"]
	trace = pipeline_result["trace"]
	status = pipeline_result["status"]
	context_entries = response.get("context", []) if isinstance(response, dict) else []
	context_sources = _extract_context_sources(context_entries)
	context_text = _extract_context_text(context_entries)
	context_text_by_source = _aggregate_context_text_by_source(context_entries)
	parent_results = trace.get("parent_results", [])
	parent_stage_expected_source_files = case.expected_source_files
	parent_stage_actual_sources = _extract_context_sources(
		[
			{
				"metadata": doc.metadata,
				"page_content": doc.page_content,
			}
			for doc in parent_results
		]
	)
	expected_first_stage_sources = _build_source_records(parent_stage_expected_source_files, set(parent_stage_actual_sources))
	observed_first_stage_sources = _build_source_records(parent_stage_actual_sources)
	matched_keywords = [keyword for keyword in case.expected_keywords if keyword.lower() in context_text.lower()]
	matched_forbidden_keywords = [keyword for keyword in case.forbidden_keywords if keyword.lower() in context_text.lower()]
	keyword_check = len(matched_keywords) == len(case.expected_keywords)
	forbidden_keywords_check = len(matched_forbidden_keywords) == 0
	routed_domains = response.get("routed_domains", []) if isinstance(response, dict) else []
	expanded_routed_domains = response.get("expanded_routed_domains", []) if isinstance(response, dict) else []
	routed_domain_check_field = "expanded_routed_domains" if len(case.domains) > 1 else "routed_domains"
	routed_domain_candidates = expanded_routed_domains if routed_domain_check_field == "expanded_routed_domains" else routed_domains
	routed_domains_check = True if not case.domains else set(case.domains).issubset(set(routed_domain_candidates))
	expected_context_matches = []
	for expected_context in case.expected_retrieved_context:
		expected_source_file = str(expected_context.get("source_file", "")).strip()
		expected_chunk_text = str(expected_context.get("chunk_text", "")).strip()
		match_found = False
		actual_source_text = context_text_by_source.get(expected_source_file, "")
		if expected_source_file and actual_source_text:
			match_found = _matches_expected_context(expected_chunk_text, actual_source_text)
		expected_context_matches.append(
			{
				"source_file": expected_source_file,
				"chunk_text": expected_chunk_text,
				"matched": match_found,
			}
		)
	expected_context_check = all(item["matched"] for item in expected_context_matches)
	file_check = True if not case.expected_source_files else any(
		source_file in context_sources for source_file in case.expected_source_files
	)
	parent_stage_source_check = True if not parent_stage_expected_source_files else any(
		source_file in parent_stage_actual_sources for source_file in parent_stage_expected_source_files
	)
	parent_stage_expected_matches = [
		{
			"source_file": source_file,
			"matched": source_file in parent_stage_actual_sources,
		}
		for source_file in parent_stage_expected_source_files
	]
	distinct_sources_check = len(context_sources) >= case.min_distinct_sources
	context_non_empty_check = len(context_entries) > 0 if case.expect_context_non_empty else True
	passed = (
		status == "success"
		and routed_domains_check
		and context_non_empty_check
		and keyword_check
		and forbidden_keywords_check
		and file_check
		and expected_context_check
		and parent_stage_source_check
		and distinct_sources_check
	)

	return {
		"name": case.name,
		"query": case.query,
		"expected_routed_domains": case.domains,
		"username": case.username,
		"note": case.note,
		"expected_keywords": case.expected_keywords,
		"expected_source_files": case.expected_source_files,
		"expected_first_stage_sources": expected_first_stage_sources,
		"observed_first_stage_sources": observed_first_stage_sources,
		"min_distinct_sources": case.min_distinct_sources,
		"status": status,
		"passed": passed,
		"checks": {
			"status": status == "success",
			"routed_domains": routed_domains_check,
			"context_non_empty": context_non_empty_check,
			"keywords": keyword_check,
			"forbidden_keywords": forbidden_keywords_check,
			"expected_source_file_present": file_check,
			"expected_context_present": expected_context_check,
			"distinct_sources": distinct_sources_check,
		},
		"routed_domain_check_field": routed_domain_check_field,
		"observed_routed_domains": routed_domains,
		"observed_expanded_routed_domains": expanded_routed_domains,
		"matched_keywords": matched_keywords,
		"missing_keywords": [keyword for keyword in case.expected_keywords if keyword not in matched_keywords],
		"forbidden_keywords": case.forbidden_keywords,
		"matched_forbidden_keywords": matched_forbidden_keywords,
		"observed_context_sources": context_sources,
		"observed_context_count": len(context_entries),
		"expected_retrieved_context": expected_context_matches,
		"first_stage_results": [
			{
				"source_file": doc.metadata.get("source_file", "unknown"),
				"domain": doc.metadata.get("domain", "unknown"),
			}
			for doc in parent_results
		],
		"retrieved_chunks": [
			{
				"source_file": entry.get("metadata", {}).get("source_file", "unknown"),
				"domain": entry.get("metadata", {}).get("domain", "unknown"),
				"context_compressed": entry.get("metadata", {}).get("context_compressed", False),
				"context_compression_mode": entry.get("metadata", {}).get("context_compression_mode", "none"),
				"page_content": entry.get("page_content", ""),
			}
			for entry in context_entries
		],
		"metrics": trace["metrics"],
		"compression_modes": sorted(
			{
				str(doc.metadata.get("context_compression_mode", "none"))
				for doc in trace["final_results"]
				if doc.metadata.get("context_compressed") is True
			}
			or {"none"}
		),
		"stage_previews": {
			"vector": [
				{
					"source_file": doc.metadata.get("source_file", "unknown"),
					"domain": doc.metadata.get("domain", "unknown"),
					"preview": doc.page_content[:120].replace("\n", " "),
				}
				for doc in trace["vector_results"][:3]
			],
			"bm25": [
				{
					"source_file": doc.metadata.get("source_file", "unknown"),
					"domain": doc.metadata.get("domain", "unknown"),
					"preview": doc.page_content[:120].replace("\n", " "),
				}
				for doc in trace["bm25_results"][:3]
			],
			"final": [
				{
					"source_file": doc.metadata.get("source_file", "unknown"),
					"domain": doc.metadata.get("domain", "unknown"),
					"context_compressed": doc.metadata.get("context_compressed", False),
					"context_compression_mode": doc.metadata.get("context_compression_mode", "none"),
					"preview": doc.page_content[:160].replace("\n", " "),
				}
				for doc in trace["final_results"]
			],
		},
	}


def _summarize_report(index_health_before: dict[str, Any], index_health_after: dict[str, Any], rebuild_result: dict[str, Any] | None, generated_cases: list[GeneratedCase], results: list[dict[str, Any]]) -> dict[str, Any]:
	passed_count = sum(1 for result in results if result["passed"])
	failed_cases = [result["name"] for result in results if not result["passed"]]
	all_context_sources = sorted({source for result in results for source in result["observed_context_sources"]})
	all_compression_modes = sorted({mode for result in results for mode in result["compression_modes"]})

	return {
		"generated_at": datetime.now().isoformat(timespec="seconds"),
		"corpus": {
			"docs_dir": str(DOCS_DIR),
			"doc_count": len(list_docs_markdown_files()),
			"generated_case_count": len(generated_cases),
			"case_names": [case.name for case in generated_cases],
		},
		"index_health_before": index_health_before,
		"index_health_after": index_health_after,
		"rebuild_result": rebuild_result,
		"summary": {
			"case_count": len(results),
			"passed_count": passed_count,
			"failed_count": len(results) - passed_count,
			"passed_ratio": round(passed_count / len(results), 4) if results else 0.0,
			"failed_cases": failed_cases,
			"observed_context_sources": all_context_sources,
			"compression_modes": all_compression_modes,
		},
		"results": results,
	}


def _report_status(results: list[dict[str, Any]]) -> str:
	return "success" if all(result["passed"] for result in results) else "failure"


def _build_status_report(base_report: dict[str, Any], report_variant: str, overall_status: str) -> dict[str, Any]:
	if report_variant not in {"all", "success", "failure"}:
		raise ValueError(f"unsupported report variant: {report_variant}")

	selected_results = list(base_report.get("results", []))
	if report_variant == "success":
		selected_results = [result for result in selected_results if result.get("passed")]
	elif report_variant == "failure":
		selected_results = [result for result in selected_results if not result.get("passed")]

	passed_count = sum(1 for result in selected_results if result.get("passed"))
	failed_cases = [result["name"] for result in selected_results if not result.get("passed")]
	all_context_sources = sorted({source for result in selected_results for source in result.get("observed_context_sources", [])})
	all_compression_modes = sorted({mode for result in selected_results for mode in result.get("compression_modes", [])})
	base_report_copy = dict(base_report)
	base_report_copy["status"] = overall_status
	base_report_copy["report_variant"] = report_variant
	base_report_copy["summary"] = {
		"case_count": len(selected_results),
		"passed_count": passed_count,
		"failed_count": len(selected_results) - passed_count,
		"passed_ratio": round(passed_count / len(selected_results), 4) if selected_results else 0.0,
		"failed_cases": failed_cases,
		"observed_context_sources": all_context_sources,
		"compression_modes": all_compression_modes,
	}
	base_report_copy["results"] = selected_results
	return base_report_copy


def _write_report_artifacts(report_dir: Path, report_prefix: str, report: dict[str, Any], report_suffix: str) -> dict[str, str]:
	timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	file_prefix = f"{report_prefix}-{report_suffix}" if report_suffix else report_prefix
	json_report_path = report_dir / f"{file_prefix}.json"
	md_report_path = report_dir / f"{file_prefix}.md"
	timestamp_json_report_path = report_dir / f"{file_prefix}-{timestamp}.json"
	timestamp_md_report_path = report_dir / f"{file_prefix}-{timestamp}.md"
	json_payload = json.dumps(report, ensure_ascii=False, indent=2)
	json_report_path.write_text(json_payload, encoding="utf-8")
	timestamp_json_report_path.write_text(json_payload, encoding="utf-8")

	markdown_report = _render_markdown_report(report)
	md_report_path.write_text(markdown_report, encoding="utf-8")
	timestamp_md_report_path.write_text(markdown_report, encoding="utf-8")

	return {
		"json": str(json_report_path),
		"markdown": str(md_report_path),
		"timestamped_json": str(timestamp_json_report_path),
		"timestamped_markdown": str(timestamp_md_report_path),
	}


def _render_markdown_report(report: dict[str, Any]) -> str:
	summary = report["summary"]
	index_before = report["index_health_before"]
	index_after = report["index_health_after"]
	rebuild_result = report.get("rebuild_result") or {}

	lines = [
		"# RAG Pipeline End-to-End Report",
		"",
		f"Generated at: {report['generated_at']}",
		f"Run status: {report.get('status', 'unknown')}",
		f"Docs dir: {report['corpus']['docs_dir']}",
		f"Generated cases: {report['corpus']['generated_case_count']} / docs scanned: {report['corpus']['doc_count']}",
		"",
		"## Rebuild",
		f"- Vector docs before: {index_before['vector_count']}",
		f"- ES enabled: {index_before['es_enabled']}",
		f"- ES available: {index_before['es_available']}",
		f"- Deleted vector docs: {rebuild_result.get('deleted_vector_count', 0)}",
		f"- Deleted ES docs: {rebuild_result.get('deleted_es_count', 0)}",
		f"- Parent ES docs after: {index_after.get('es_parent_count')}",
		f"- Vector docs after: {index_after['vector_count']}",
		f"- ES docs after: {index_after['es_count']}",
		"",
		"## Summary",
		f"- Passed: {summary['passed_count']}/{summary['case_count']}",
		f"- Failed cases: {', '.join(summary['failed_cases']) if summary['failed_cases'] else 'none'}",
		f"- Observed context sources: {', '.join(summary['observed_context_sources']) if summary['observed_context_sources'] else 'none'}",
		f"- Compression modes: {', '.join(summary['compression_modes']) if summary['compression_modes'] else 'none'}",
		"",
		"## Cases",
	]

	for result in report["results"]:
		lines.extend(
			[
				f"### {result['name']}",
				f"- Status: {'PASS' if result['passed'] else 'FAIL'}",
				f"- Query: {result['query']}",
				f"- Expected routed domains: {result['expected_routed_domains'] or ['<auto-infer>']}",
				f"- Observed routed domains: {result['observed_routed_domains'] or ['<none>']}",
				f"- Observed expanded routed domains: {result.get('observed_expanded_routed_domains', []) or ['<none>']}",
				f"- Routed domain check field: {result.get('routed_domain_check_field', 'routed_domains')}",
				f"- Observed sources: {', '.join(result['observed_context_sources']) if result['observed_context_sources'] else 'none'}",
				f"- Expected keywords: {', '.join(result['expected_keywords']) if result['expected_keywords'] else 'none'}",
				f"- Matched keywords: {', '.join(result['matched_keywords']) if result['matched_keywords'] else 'none'}",
				f"- Missing keywords: {', '.join(result['missing_keywords']) if result['missing_keywords'] else 'none'}",
				f"- Forbidden keywords: {', '.join(result['forbidden_keywords']) if result['forbidden_keywords'] else 'none'}",
				f"- Matched forbidden keywords: {', '.join(result['matched_forbidden_keywords']) if result['matched_forbidden_keywords'] else 'none'}",
				f"- Stage hits: vector={result['metrics']['vector_hit_count']}, bm25={result['metrics']['bm25_hit_count']}, fused={result['metrics']['fused_hit_count']}, reranked={result['metrics']['reranked_hit_count']}, final={result['metrics']['final_hit_count']}",
				f"- Compression: compressed_docs={result['metrics']['compression']['compressed_doc_count']}, saved_chars={result['metrics']['compression']['saved_char_count']}, saved_ratio={result['metrics']['compression']['saved_ratio']:.2%}",
				"",
			]
		)
		lines.append("#### First Stage")
		if not result.get("expected_first_stage_sources"):
			lines.append("- Expected first-stage sources: none")
		else:
			lines.append("- Expected first-stage sources:")
			for expected_first_stage in result.get("expected_first_stage_sources", []):
				lines.append(
					f"  - {expected_first_stage.get('source_file', 'unknown')} | {expected_first_stage.get('domain', 'unknown')} | {'matched' if expected_first_stage.get('matched', False) else 'missing'}"
				)
		if not result.get("observed_first_stage_sources"):
			lines.append("- Observed first-stage sources: none")
		else:
			lines.append("- Observed first-stage sources:")
			for observed_first_stage in result.get("observed_first_stage_sources", []):
				lines.append(
					f"  - {observed_first_stage.get('source_file', 'unknown')} | {observed_first_stage.get('domain', 'unknown')}"
				)
		lines.append("#### Expected Retrieved Context")
		if not result.get("expected_retrieved_context"):
			lines.append("- none")
		for expected_chunk in result.get("expected_retrieved_context", []):
			expected_text = str(expected_chunk.get("chunk_text", "")).rstrip()
			if not expected_text:
				continue
			lines.extend(
				[
					f"  - Expected source: {expected_chunk.get('source_file', 'unknown')}",
					f"  - Matched: {expected_chunk.get('matched', False)}",
					"  - Expected text:",
					"```text",
					expected_text,
					"```",
				],
			)
		lines.extend(["", "#### Retrieved Chunks"])
		if not result.get("retrieved_chunks"):
			lines.append("- none")
		for chunk in result.get("retrieved_chunks", []):
			chunk_text = str(chunk.get("page_content", "")).rstrip()
			if not chunk_text:
				continue
			lines.extend(
				[
					f"  - Source: {chunk.get('source_file', 'unknown')}",
					f"  - Domain: {chunk.get('domain', 'unknown')}",
					f"  - Compressed: {chunk.get('context_compressed', False)} ({chunk.get('context_compression_mode', 'none')})",
					"  - Text:",
					"```text",
					chunk_text,
					"```",
				],
			)

	return "\n".join(lines).rstrip() + "\n"


def _print_console_summary(report: dict[str, Any]) -> None:
	summary = report["summary"]
	report_paths = report["report_paths"]
	print("=== RAG Pipeline End-to-End ===")
	print(f"generated_at={report['generated_at']}")
	print(f"docs_scanned={report['corpus']['doc_count']} cases_generated={report['corpus']['generated_case_count']}")
	print(
		"rebuild="
		f"vector_before={report['index_health_before']['vector_count']} "
		f"vector_after={report['index_health_after']['vector_count']} "
		f"es_before={report['index_health_before']['es_count']} "
		f"es_after={report['index_health_after']['es_count']}"
	)
	print(f"passed={summary['passed_count']}/{summary['case_count']} failed={summary['failed_cases'] or 'none'}")
	print(f"report_all_json={report_paths['all']['json']}")
	print(f"report_all_markdown={report_paths['all']['markdown']}")
	print(f"report_success_json={report_paths['success']['json']}")
	print(f"report_success_markdown={report_paths['success']['markdown']}")
	print(f"report_failure_json={report_paths['failure']['json']}")
	print(f"report_failure_markdown={report_paths['failure']['markdown']}")


def main() -> int:
	args = _parse_args()
	report_dir = Path(args.report_dir)
	report_dir.mkdir(parents=True, exist_ok=True)
	removed_report_artifacts = _cleanup_previous_report_artifacts(report_dir, args.report_prefix)
	if removed_report_artifacts:
		print(f"Removed previous report artifacts: {len(removed_report_artifacts)}")
		for removed_path in removed_report_artifacts:
			print(f"- {removed_path}")

	if args.augment_arxiv:
		augmentation_result = _maybe_augment_corpus(True)
	else:
		augmentation_result = {"enabled": False, "message": "not requested"}

	index_health_before = _collect_index_health()
	generated_cases = _build_case_catalog()
	if args.limit > 0:
		generated_cases = generated_cases[: args.limit]

	if not generated_cases:
		raise RuntimeError("no evaluation cases were generated from docs; add more docs or enable --augment-arxiv")

	rebuild_result: dict[str, Any] | None = None
	if not args.no_rebuild:
		print("Clearing indexes and rebuilding from docs...")
		rebuild_result = _clear_and_rebuild_indexes()
	else:
		print("Skipping rebuild because --no-rebuild was set.")

	index_health_after = _collect_index_health()
	results = [_evaluate_case(case) for case in generated_cases]
	report_status = _report_status(results)
	combined_report = _summarize_report(index_health_before, index_health_after, rebuild_result, generated_cases, results)
	combined_report["status"] = report_status
	combined_report["report_variant"] = "all"
	combined_report["cleanup_result"] = {
		"removed_report_artifacts": removed_report_artifacts,
		"removed_report_artifact_count": len(removed_report_artifacts),
	}
	if augmentation_result["enabled"]:
		combined_report["augmentation_result"] = augmentation_result

	report_artifacts = {
		"all": _write_report_artifacts(report_dir, args.report_prefix, combined_report, ""),
		"success": _write_report_artifacts(
			report_dir,
			args.report_prefix,
			_build_status_report(combined_report, "success", report_status),
			"success",
		),
		"failure": _write_report_artifacts(
			report_dir,
			args.report_prefix,
			_build_status_report(combined_report, "failure", report_status),
			"failure",
		),
	}
	combined_report["report_paths"] = report_artifacts

	json_payload = json.dumps(combined_report, ensure_ascii=False, indent=2)
	_print_console_summary(combined_report)
	if args.json:
		print(json_payload)
	else:
		print(_render_markdown_report(combined_report))

	return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
	raise SystemExit(main())