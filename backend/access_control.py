import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import frontmatter

from knowledge_base_service import list_knowledge_bases, normalize_knowledge_base_name
from rag_config import ENABLE_ACL

ANONYMOUS_USERNAME = "anonymous"
PUBLIC_SUBJECT = "*"
AUTHENTICATED_SUBJECT = "$authenticated"
GROUP_SUBJECT_PREFIX = "group:"
ALL_KNOWLEDGE_BASES = "*"


def _file_cache_token(path: Path) -> tuple[str, int, int]:
    resolved_path = path.expanduser().resolve()
    try:
        stat = resolved_path.stat()
    except OSError:
        return str(resolved_path), 0, 0
    return str(resolved_path), int(stat.st_mtime_ns), int(stat.st_size)


def _markdown_cache_tokens(docs_dir: Path) -> tuple[tuple[str, int, int], ...]:
    resolved_docs_dir = docs_dir.expanduser().resolve()
    tokens: list[tuple[str, int, int]] = []
    for file_path in sorted(resolved_docs_dir.rglob("*.md")):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        relative_path = str(file_path.relative_to(resolved_docs_dir)).replace("\\", "/")
        tokens.append((relative_path, int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(tokens)


def normalize_username(username: str | None) -> str:
    if not username:
        return ANONYMOUS_USERNAME

    normalized = username.strip().lower()
    return normalized or ANONYMOUS_USERNAME


def coerce_subject_list(value: Any) -> set[str]:
    if isinstance(value, str):
        parts = [part.strip().lower() for part in value.split(",")]
        return {part for part in parts if part}

    if isinstance(value, list):
        return {
            item.strip().lower()
            for item in value
            if isinstance(item, str) and item.strip()
        }

    return set()


def normalize_acl_subject(subject: str) -> str:
    normalized = subject.strip().lower()
    if normalized in {"public", "anonymous", PUBLIC_SUBJECT}:
        return PUBLIC_SUBJECT
    if normalized in {"authenticated", AUTHENTICATED_SUBJECT}:
        return AUTHENTICATED_SUBJECT
    if normalized.startswith("@"):
        normalized = f"{GROUP_SUBJECT_PREFIX}{normalized[1:]}"
    if normalized.startswith("group/"):
        normalized = f"{GROUP_SUBJECT_PREFIX}{normalized.split('/', 1)[1]}"
    return normalized


def normalize_group_name(group_name: str) -> str:
    normalized = group_name.strip().lower()
    if normalized.startswith(GROUP_SUBJECT_PREFIX):
        normalized = normalized[len(GROUP_SUBJECT_PREFIX):]
    if normalized.startswith("@"):
        normalized = normalized[1:]
    return normalized


def to_group_subject(group_name: str) -> str:
    normalized = normalize_group_name(group_name)
    return f"{GROUP_SUBJECT_PREFIX}{normalized}" if normalized else ""


@lru_cache(maxsize=32)
def _load_username_group_mapping_cached(path_text: str, mtime_ns: int, size: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    mapping_file = Path(path_text)
    if mtime_ns == 0 or size == 0 or not mapping_file.is_file():
        return tuple()

    try:
        with open(mapping_file, "r", encoding="utf-8") as file_handle:
            raw_mapping = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ACL] failed to load username group mapping: {exc}")
        return tuple()

    if not isinstance(raw_mapping, dict):
        print("[ACL] ignore username group mapping: root value must be a JSON object")
        return tuple()

    mapping: dict[str, tuple[str, ...]] = {}
    for raw_username, raw_groups in raw_mapping.items():
        if not isinstance(raw_username, str):
            continue

        username = normalize_username(raw_username)
        group_subjects = {
            to_group_subject(group_name)
            for group_name in coerce_subject_list(raw_groups)
            if to_group_subject(group_name)
        }
        if username and group_subjects:
            mapping[username] = tuple(sorted(group_subjects))

    return tuple(sorted(mapping.items()))


def load_username_group_mapping(mapping_file: Path) -> dict[str, set[str]]:
    path_text, mtime_ns, size = _file_cache_token(mapping_file)
    cached_mapping = _load_username_group_mapping_cached(path_text, mtime_ns, size)
    return {username: set(groups) for username, groups in cached_mapping}


def coerce_knowledge_base_list(value: Any) -> set[str]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return {part for part in parts if part}

    if isinstance(value, list):
        return {
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        }

    return set()


@lru_cache(maxsize=32)
def _load_knowledge_base_group_mapping_cached(path_text: str, mtime_ns: int, size: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    mapping_file = Path(path_text)
    if mtime_ns == 0 or size == 0 or not mapping_file.is_file():
        return tuple()

    try:
        with open(mapping_file, "r", encoding="utf-8") as file_handle:
            raw_mapping = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ACL] failed to load knowledge base group mapping: {exc}")
        return tuple()

    if not isinstance(raw_mapping, dict):
        print("[ACL] ignore knowledge base group mapping: root value must be a JSON object")
        return tuple()

    mapping: dict[str, tuple[str, ...]] = {}
    for raw_group_name, raw_knowledge_bases in raw_mapping.items():
        if not isinstance(raw_group_name, str):
            continue

        group_name = normalize_group_name(raw_group_name)
        knowledge_bases = set()

        for raw_knowledge_base in coerce_knowledge_base_list(raw_knowledge_bases):
            if raw_knowledge_base == ALL_KNOWLEDGE_BASES:
                knowledge_bases.add(ALL_KNOWLEDGE_BASES)
                continue

            try:
                knowledge_bases.add(normalize_knowledge_base_name(raw_knowledge_base))
            except ValueError as exc:
                print(f"[ACL] ignore invalid knowledge base mapping for group '{group_name}': {exc}")

        if group_name and knowledge_bases:
            mapping[group_name] = tuple(sorted(knowledge_bases))

    return tuple(sorted(mapping.items()))


def load_knowledge_base_group_mapping(mapping_file: Path) -> dict[str, set[str]]:
    path_text, mtime_ns, size = _file_cache_token(mapping_file)
    cached_mapping = _load_knowledge_base_group_mapping_cached(path_text, mtime_ns, size)
    return {group_name: set(knowledge_bases) for group_name, knowledge_bases in cached_mapping}


def parse_acl_subjects(metadata: dict[str, Any]) -> set[str]:
    acl = metadata.get("acl")
    if acl is None:
        return {PUBLIC_SUBJECT}

    if isinstance(acl, str):
        normalized = normalize_acl_subject(acl)
        return {normalized} if normalized else set()

    if isinstance(acl, list):
        return {
            normalize_acl_subject(item)
            for item in acl
            if isinstance(item, str) and normalize_acl_subject(item)
        }

    if isinstance(acl, dict):
        subjects = set()
        if acl.get("public") is True:
            subjects.add(PUBLIC_SUBJECT)
        if acl.get("authenticated") is True or acl.get("allow_authenticated") is True:
            subjects.add(AUTHENTICATED_SUBJECT)
        subjects.update(coerce_subject_list(acl.get("allow")))
        subjects.update(coerce_subject_list(acl.get("users")))
        subjects.update(coerce_subject_list(acl.get("subjects")))
        subjects.update(to_group_subject(group_name) for group_name in coerce_subject_list(acl.get("groups")))
        subjects.update(to_group_subject(group_name) for group_name in coerce_subject_list(acl.get("allow_groups")))
        return {normalize_acl_subject(subject) for subject in subjects if normalize_acl_subject(subject)}

    return set()


@lru_cache(maxsize=16)
def _load_document_access_manifest_cached(
    docs_dir_text: str,
    markdown_tokens: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    docs_dir = Path(docs_dir_text)
    manifest: list[tuple[str, str, tuple[str, ...]]] = []

    for relative_path, _, _ in markdown_tokens:
        path_obj = docs_dir / relative_path
        try:
            with open(path_obj, "r", encoding="utf-8") as file_handle:
                post = frontmatter.load(file_handle)
        except OSError as exc:
            print(f"[ACL] failed to load document ACL metadata from {path_obj}: {exc}")
            continue

        metadata = post.metadata if isinstance(post.metadata, dict) else {}
        domain = str(metadata.get("domain") or path_obj.parent.name or "global").strip()
        source_file = relative_path.replace("\\", "/")
        acl_subjects = parse_acl_subjects(metadata)

        manifest.append((domain, source_file, tuple(sorted(acl_subjects))))

    return tuple(manifest)


def load_document_access_manifest(docs_dir: Path) -> list[dict[str, Any]]:
    resolved_docs_dir = docs_dir.expanduser().resolve()
    markdown_tokens = _markdown_cache_tokens(resolved_docs_dir)
    cached_manifest = _load_document_access_manifest_cached(str(resolved_docs_dir), markdown_tokens)

    return [
        {
            "domain": domain,
            "source_file": source_file,
            "acl_subjects": set(acl_subjects),
        }
        for domain, source_file, acl_subjects in cached_manifest
    ]


def resolve_user_subjects(username: str, group_mapping: dict[str, set[str]] | None = None) -> set[str]:
    subjects = {username}
    if username != ANONYMOUS_USERNAME:
        subjects.add(AUTHENTICATED_SUBJECT)

    resolved_group_mapping = group_mapping if group_mapping is not None else {}
    subjects.update(resolved_group_mapping.get(username, set()))
    return subjects


def resolve_user_group_names(username: str, group_mapping: dict[str, set[str]] | None = None) -> set[str]:
    subjects = resolve_user_subjects(normalize_username(username), group_mapping)
    return {
        subject[len(GROUP_SUBJECT_PREFIX):]
        for subject in subjects
        if subject.startswith(GROUP_SUBJECT_PREFIX) and subject[len(GROUP_SUBJECT_PREFIX):]
    }


def resolve_accessible_knowledge_bases(
    username: str,
    requested_knowledge_bases: list[str] | None,
    username_group_mapping_file: Path,
    knowledge_base_group_mapping_file: Path,
) -> tuple[list[str], list[str]]:
    normalized_requested = dedupe_values(requested_knowledge_bases or [])
    group_mapping = load_username_group_mapping(username_group_mapping_file)
    knowledge_base_group_mapping = load_knowledge_base_group_mapping(knowledge_base_group_mapping_file)

    if not knowledge_base_group_mapping:
        accessible = normalized_requested or list_knowledge_bases()
        return accessible, []

    user_group_names = resolve_user_group_names(username, group_mapping)
    if not user_group_names:
        candidate_knowledge_bases = normalized_requested or list_knowledge_bases()
        return [], candidate_knowledge_bases

    allowed_knowledge_bases: set[str] = set()
    allow_all_knowledge_bases = False
    for group_name in user_group_names:
        group_knowledge_bases = knowledge_base_group_mapping.get(group_name, set())
        if ALL_KNOWLEDGE_BASES in group_knowledge_bases:
            allow_all_knowledge_bases = True
            break
        allowed_knowledge_bases.update(group_knowledge_bases)

    candidate_knowledge_bases = normalized_requested or list_knowledge_bases()
    if allow_all_knowledge_bases:
        return candidate_knowledge_bases, []

    accessible_knowledge_bases = [
        knowledge_base
        for knowledge_base in candidate_knowledge_bases
        if knowledge_base in allowed_knowledge_bases
    ]
    restricted_knowledge_bases = [
        knowledge_base
        for knowledge_base in candidate_knowledge_bases
        if knowledge_base not in accessible_knowledge_bases
    ]
    return dedupe_values(accessible_knowledge_bases), dedupe_values(restricted_knowledge_bases)


def is_knowledge_base_accessible(
    username: str,
    knowledge_base: str,
    username_group_mapping_file: Path,
    knowledge_base_group_mapping_file: Path,
) -> bool:
    accessible_knowledge_bases, _ = resolve_accessible_knowledge_bases(
        username,
        [normalize_knowledge_base_name(knowledge_base)],
        username_group_mapping_file,
        knowledge_base_group_mapping_file,
    )
    return bool(accessible_knowledge_bases)


def is_document_accessible(user_subjects: set[str], acl_subjects: set[str]) -> bool:
    if not ENABLE_ACL:
        return True

    if not acl_subjects:
        return False

    if PUBLIC_SUBJECT in acl_subjects:
        return True

    if user_subjects.intersection(acl_subjects):
        return True

    return False


def dedupe_values(values: list[str]) -> list[str]:
    normalized_values: list[str] = []
    seen = set()

    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized_values.append(cleaned)

    return normalized_values


def build_source_file_filter(source_files: list[str]) -> dict | None:
    normalized_files = dedupe_values(source_files)
    if not normalized_files:
        return None

    if len(normalized_files) == 1:
        return {"source_file": normalized_files[0]}

    return {"source_file": {"$in": normalized_files}}


def resolve_accessible_sources(
    username: str,
    requested_domains: list[str],
    docs_dir: Path,
    username_group_mapping_file: Path,
) -> tuple[list[str], list[str], list[str]]:
    manifest = load_document_access_manifest(docs_dir)
    group_mapping = load_username_group_mapping(username_group_mapping_file)
    user_subjects = resolve_user_subjects(normalize_username(username), group_mapping)
    normalized_requested = dedupe_values(requested_domains)
    requested_set = set(normalized_requested)
    candidate_entries = [
        entry for entry in manifest
        if not requested_set or entry["domain"] in requested_set
    ]

    accessible_entries = [
        entry for entry in candidate_entries
        if is_document_accessible(user_subjects, entry["acl_subjects"])
    ]
    restricted_entries = [
        entry for entry in candidate_entries
        if entry not in accessible_entries
    ]

    accessible_source_files = [entry["source_file"] for entry in accessible_entries]
    restricted_source_files = [entry["source_file"] for entry in restricted_entries]
    accessible_domains = sorted({entry["domain"] for entry in accessible_entries})
    return accessible_source_files, restricted_source_files, accessible_domains


def resolve_accessible_query_scope(
    username: str,
    requested_domains: list[str] | None,
    requested_source_files: list[str] | None,
    docs_dir: Path,
    username_group_mapping_file: Path,
) -> tuple[list[str], list[str]]:
    normalized_requested_domains = dedupe_values(requested_domains or [])
    normalized_requested_source_files = dedupe_values(requested_source_files or [])
    manifest = load_document_access_manifest(docs_dir)
    group_mapping = load_username_group_mapping(username_group_mapping_file)
    user_subjects = resolve_user_subjects(normalize_username(username), group_mapping)
    requested_domain_set = set(normalized_requested_domains)
    requested_source_file_set = set(normalized_requested_source_files)

    if not normalized_requested_domains and not normalized_requested_source_files:
        accessible_entries = [
            entry for entry in manifest
            if is_document_accessible(user_subjects, entry["acl_subjects"])
        ]
        accessible_source_files = dedupe_values([entry["source_file"] for entry in accessible_entries])
        accessible_domains = sorted({entry["domain"] for entry in accessible_entries})
        return accessible_domains, accessible_source_files

    matched_entries: list[dict[str, Any]] = []
    domain_matched_entries: list[dict[str, Any]] = []
    source_file_matched_entries: list[dict[str, Any]] = []

    for entry in manifest:
        matches_domain = bool(requested_domain_set) and entry["domain"] in requested_domain_set
        matches_source_file = bool(requested_source_file_set) and entry["source_file"] in requested_source_file_set
        if not matches_domain and not matches_source_file:
            continue
        if not is_document_accessible(user_subjects, entry["acl_subjects"]):
            continue

        matched_entries.append(entry)
        if matches_domain:
            domain_matched_entries.append(entry)
        if matches_source_file:
            source_file_matched_entries.append(entry)

    if normalized_requested_domains:
        accessible_domains = sorted({entry["domain"] for entry in domain_matched_entries})
    else:
        accessible_domains = dedupe_values([str(entry["domain"]).strip() for entry in source_file_matched_entries if str(entry["domain"]).strip()])

    accessible_source_files = [entry["source_file"] for entry in matched_entries]

    return dedupe_values(accessible_domains), dedupe_values(accessible_source_files)


def resolve_accessible_source_files(
    username: str,
    requested_source_files: list[str],
    docs_dir: Path,
    username_group_mapping_file: Path,
) -> tuple[list[str], list[str], list[str]]:
    manifest = load_document_access_manifest(docs_dir)
    group_mapping = load_username_group_mapping(username_group_mapping_file)
    user_subjects = resolve_user_subjects(normalize_username(username), group_mapping)
    normalized_requested = dedupe_values(requested_source_files)
    requested_set = set(normalized_requested)
    candidate_entries = [
        entry for entry in manifest
        if not requested_set or entry["source_file"] in requested_set
    ]

    accessible_entries = [
        entry for entry in candidate_entries
        if is_document_accessible(user_subjects, entry["acl_subjects"])
    ]
    restricted_entries = [
        entry for entry in candidate_entries
        if entry not in accessible_entries
    ]

    accessible_source_files = dedupe_values([entry["source_file"] for entry in accessible_entries])
    restricted_source_files = dedupe_values([entry["source_file"] for entry in restricted_entries])
    accessible_domains = dedupe_values([str(entry["domain"]).strip() for entry in accessible_entries if str(entry["domain"]).strip()])
    return accessible_source_files, restricted_source_files, accessible_domains