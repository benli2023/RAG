from __future__ import annotations

import io
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"
TARGET_DIR = DOCS_DIR / "arxiv-surveys"

PAPER_IDS = [
    "2506.14634v3",
    "2107.08523v1",
    "2202.09061v4",
    "2305.02750v2",
    "2303.06574v2",
    "2107.03175v2",
    "2004.13820v2",
    "2311.12399v4",
    "2409.11564v2",
    "1811.06278v2",
    "2403.08319v2",
    "2010.04389v4",
    "2405.04404v1",
    "2402.01799v2",
    "2208.10099v1",
    "2409.06857v7",
    "2112.11739v2",
    "2105.10311v2",
    "2103.16929v1",
    "2106.15561v3",
]

QUESTION_SPECS = {
    "2506.14634v3": {
        "name": "llm-coding-german-open-ended-survey-responses",
        "query": "这篇论文研究的是哪类开放式 survey 回复编码任务？作者为什么要用大语言模型来做这件事？",
        "keywords": ["Large Language Models", "coding", "German Open-Ended Survey Responses", "survey motivation"],
    },
    "2107.08523v1": {
        "name": "argument-linking-survey-forecast",
        "query": "这篇综述聚焦的是自然语言理解里的哪一个语义任务？它和什么链接关系有关？",
        "keywords": ["Argument Linking", "Semantic role labeling", "predicate", "constituents"],
    },
    "2202.09061v4": {
        "name": "vision-language-pretraining-survey",
        "query": "这篇综述主要梳理的是哪种跨模态预训练？它覆盖了图文和什么类型的预训练？",
        "keywords": ["Vision-Language Pre-training", "image-text", "video-text", "pre-training"],
    },
    "2305.02750v2": {
        "name": "proactive-dialogue-systems-survey",
        "query": "这篇论文讨论的主动式对话系统主要想解决什么问题？它如何让 conversational agent 引导对话朝目标方向推进？",
        "keywords": ["Proactive dialogue systems", "conversational agent", "leading the conversation direction"],
    },
    "2303.06574v2": {
        "name": "diffusion-text-generation-survey",
        "query": "这篇综述研究的是 Diffusion Models for Non-autoregressive Text Generation 吗？它如何缓解 NAR text generation 的 inference latency 问题？",
        "keywords": ["Diffusion Models", "Non-autoregressive (NAR) text generation", "inference latency"],
    },
    "2107.03175v2": {
        "name": "dialogue-summarization-survey",
        "query": "这篇综述讨论的核心任务是什么？它要把什么类型的对话压缩成更短版本？",
        "keywords": ["Dialogue summarization", "condense", "dialogue data overload"],
    },
    "2004.13820v2": {
        "name": "semantic-similarity-survey",
        "query": "这篇综述围绕的是哪一个自然语言处理中的开放问题？它研究什么相似度？",
        "keywords": ["semantic similarity", "Natural Language Processing", "open research problem"],
    },
    "2311.12399v4": {
        "name": "graph-meets-llm-survey",
        "query": "这篇综述讨论的是图结构和大语言模型的什么交叉方向？它想解决哪些现实应用？",
        "keywords": ["Graph", "Large Language Models", "citation networks", "social networks"],
    },
    "2409.11564v2": {
        "name": "preference-tuning-human-feedback-survey",
        "query": "这篇 survey 研究的是哪类偏好调优？它把人类反馈用于哪些模态任务？",
        "keywords": ["Preference tuning", "human feedback", "Language", "Speech", "Vision"],
    },
    "1811.06278v2": {
        "name": "lexical-semantic-change-survey",
        "query": "这篇综述研究的是 lexical semantic change 还是 vocabulary change？它关注 diachronic conceptual change 的什么演化？",
        "keywords": ["lexical semantic change", "vocabulary change", "diachronic conceptual change"],
    },
    "2403.08319v2": {
        "name": "knowledge-conflicts-for-llms-survey",
        "query": "这篇综述分析的是 LLM 会遇到哪类知识冲突？它特别区分了哪两种知识？",
        "keywords": ["Knowledge conflicts", "contextual knowledge", "parametric knowledge"],
    },
    "2010.04389v4": {
        "name": "knowledge-enhanced-text-generation-survey",
        "query": "这篇综述研究的是哪种结合外部知识的文本生成任务？它的目标是什么？",
        "keywords": ["Knowledge-Enhanced Text Generation", "text generation", "knowledge"],
    },
    "2405.04404v1": {
        "name": "vision-mamba-survey",
        "query": "这篇综述总结的是哪一类视觉模型？它和状态空间模型有什么关系？",
        "keywords": ["Vision Mamba", "State Space Model", "taxonomy"],
    },
    "2402.01799v2": {
        "name": "faster-lighter-llms-survey",
        "query": "这篇综述讨论的是哪类更快、更轻量的大语言模型？它如何通过 model compression 和 inference efficiency 解决问题？",
        "keywords": ["model compression", "LLM inference", "system-level optimization"],
    },
    "2208.10099v1": {
        "name": "text-to-sql-survey",
        "query": "这篇综述研究的是自然语言到 SQL 的什么转换任务？它覆盖了什么方向？",
        "keywords": ["Text-to-SQL", "natural language", "SQL queries"],
    },
    "2409.06857v7": {
        "name": "small-models-in-llm-era-survey",
        "query": "这篇综述在讨论 LLM 时代里哪类小模型的作用？它要解决什么问题？",
        "keywords": ["Small Models", "LLM Era", "Large Language Models"],
    },
    "2112.11739v2": {
        "name": "natural-language-generation-survey",
        "query": "这篇综述回顾的是哪一个生成方向？它覆盖了哪些数据到文本和文本到文本方法？",
        "keywords": ["Natural Language Generation", "data-to-text generation", "text-to-text generation"],
    },
    "2105.10311v2": {
        "name": "pretrained-language-models-for-text-generation-survey",
        "query": "这篇综述研究的是哪类预训练语言模型在文本生成里的应用？它把什么任务作为重点？",
        "keywords": ["Pretrained Language Models", "Text Generation", "natural language processing"],
    },
    "2103.16929v1": {
        "name": "relation-triplets-extraction-survey",
        "query": "这篇综述讨论的是哪类关系三元组抽取方法？它和关系抽取有什么联系？",
        "keywords": ["Relation Triplets Extraction", "Relation Extraction", "deep neural architectures"],
    },
    "2106.15561v3": {
        "name": "neural-speech-synthesis-survey",
        "query": "这篇综述研究的是哪类神经语音合成任务？它和文本转语音有什么关系？",
        "keywords": ["Neural Speech Synthesis", "Text to speech", "speech synthesis"],
    },
}


def slugify(value: str, limit: int = 80) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:limit].strip("-") or "paper"


def clean_pdf_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"-\n(?=\w)", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page_texts = []
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - extraction failures are data-dependent
            extracted = f"[Page {page_index} extraction failed: {exc}]"
        extracted = clean_pdf_text(extracted)
        if extracted:
            page_texts.append(f"## Page {page_index}\n\n{extracted}")
    return "\n\n".join(page_texts)


def xml_text(entry: ET.Element, xpath: str, ns: dict[str, str], default: str = "") -> str:
    return entry.findtext(xpath, default=default, namespaces=ns) or default


def fetch_metadata(paper_ids: list[str]) -> dict[str, dict[str, object]]:
    ctx = ssl._create_unverified_context()
    api_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"id_list": ",".join(paper_ids), "start": 0, "max_results": len(paper_ids)}
    )
    request = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    xml_data = urllib.request.urlopen(request, context=ctx, timeout=60).read()
    root = ET.fromstring(xml_data)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    metadata: dict[str, dict[str, object]] = {}
    for entry in root.findall("atom:entry", ns):
        entry_id = xml_text(entry, "atom:id", ns).rsplit("/", 1)[-1]
        title = " ".join(xml_text(entry, "atom:title", ns).split())
        abstract = " ".join(xml_text(entry, "atom:summary", ns).split())
        published = xml_text(entry, "atom:published", ns)
        authors = [" ".join(author.findtext("atom:name", default="", namespaces=ns).split()) for author in entry.findall("atom:author", ns)]
        pdf_url = None
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href")
                break
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{entry_id}.pdf"
        metadata[entry_id] = {
            "title": title,
            "abstract": abstract,
            "published": published,
            "authors": authors,
            "pdf_url": pdf_url,
        }
    return metadata


def build_markdown(paper_id: str, info: dict[str, object], extracted_text: str) -> str:
    title = str(info["title"])
    abstract = str(info["abstract"])
    published = str(info["published"])
    pdf_url = str(info["pdf_url"])
    authors = [str(author) for author in info["authors"]]
    authors_yaml = "\n".join(f"  - {json.dumps(author, ensure_ascii=False)}" for author in authors)

    return f"""---
domain: arxiv-surveys
type: paper
source: arxiv
arxiv_id: {json.dumps(paper_id, ensure_ascii=False)}
title: {json.dumps(title, ensure_ascii=False)}
published: {json.dumps(published, ensure_ascii=False)}
pdf_url: {json.dumps(pdf_url, ensure_ascii=False)}
authors:
{authors_yaml}
---

# {title}

## Metadata

- ArXiv ID: {paper_id}
- Published: {published}
- PDF: {pdf_url}
- Authors: {', '.join(authors)}

## Abstract

{abstract}

## Extracted PDF Text

{extracted_text}
"""


def build_case_items() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for paper_id in PAPER_IDS:
        spec = QUESTION_SPECS[paper_id]
        items.append(
            {
                "name": spec["name"],
                "query": spec["query"],
                "username": "anonymous",
                "domains": ["arxiv-surveys"],
                "expected_status": "success",
                "expected_authorized_domains": ["arxiv-surveys"],
                "expected_keywords": spec["keywords"],
                "require_non_empty_context": True,
                "require_bm25_hits": False,
                "min_shared_fused_hits": 0,
            }
        )
    return items


def main() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    metadata = fetch_metadata(PAPER_IDS)

    missing = [paper_id for paper_id in PAPER_IDS if paper_id not in metadata]
    if missing:
        raise RuntimeError(f"missing metadata for: {missing}")

    ctx = ssl._create_unverified_context()
    for index, paper_id in enumerate(PAPER_IDS, start=1):
        info = metadata[paper_id]
        pdf_url = str(info["pdf_url"])
        request = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        pdf_bytes = urllib.request.urlopen(request, context=ctx, timeout=120).read()
        extracted_text = extract_pdf_text(pdf_bytes)
        if not extracted_text:
            extracted_text = "[PDF text extraction produced no readable text.]"

        file_name = f"{paper_id.replace('.', '-').replace('/', '-')}-{slugify(str(info['title']))}.md"
        markdown = build_markdown(paper_id, info, extracted_text)
        (TARGET_DIR / file_name).write_text(markdown, encoding="utf-8")
        print(f"[{index}/{len(PAPER_IDS)}] wrote docs/arxiv-surveys/{file_name}")
        time.sleep(0.2)

    case_file = TARGET_DIR / "test_questions.json"
    case_file.write_text(json.dumps(build_case_items(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote docs/arxiv-surveys/test_questions.json with {len(PAPER_IDS)} cases")


if __name__ == "__main__":
    main()
