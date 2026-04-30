import json
from pathlib import Path
from typing import Any

from rag_config import ENABLE_ACL

ANONYMOUS_USERNAME = "anonymous"
PUBLIC_SUBJECT = "*"
AUTHENTICATED_SUBJECT = "$authenticated"
GROUP_SUBJECT_PREFIX = "group:"
GET_RECORDS_PAGE_SIZE = 1000


def _iter_vectorstore_records(vectorstore: Any, include: list[str], batch_size: int = GET_RECORDS_PAGE_SIZE):
    if hasattr(vectorstore, "iter_records"):
        yield from vectorstore.iter_records(include=include, batch_size=batch_size)
        return

    offset = 0
    while True:
        records = vectorstore.get_records(include=include, limit=batch_size, offset=offset)
        yield records
        next_offset = int(records.get("next_offset", offset + len(records.get("ids", []))))
        if not records.get("has_more") or next_offset <= offset:
            break
        offset = next_offset


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


def load_username_group_mapping(mapping_file: Path) -> dict[str, set[str]]:
    if not mapping_file.is_file():
        return {}

    try:
        with open(mapping_file, "r", encoding="utf-8") as file_handle:
            raw_mapping = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ACL] failed to load username group mapping: {exc}")
        return {}

    if not isinstance(raw_mapping, dict):
        print("[ACL] ignore username group mapping: root value must be a JSON object")
        return {}

    mapping: dict[str, set[str]] = {}
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
            mapping[username] = group_subjects

    return mapping


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


def load_document_access_manifest(vectorstore: Any) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    seen = set()

    try:
        record_pages = _iter_vectorstore_records(vectorstore, include=["metadatas"])
        for records in record_pages:
            for metadata in records.get("metadatas", []):
                if not isinstance(metadata, dict):
                    continue

                domain = str(metadata.get("domain", "")).strip() or "global"
                source_file = str(metadata.get("source_file", "")).strip()
                
                if not source_file:
                    continue

                key = f"{domain}:{source_file}"
                if key in seen:
                    continue
                seen.add(key)

                acl_subjects = parse_acl_subjects(metadata)

                manifest.append({
                    "domain": domain,
                    "source_file": source_file,
                    "acl_subjects": acl_subjects,
                })
    except Exception as exc:
        print(f"[ACL] failed to load metadatas from vectorstore: {exc}")
        return []

    return manifest


def resolve_user_subjects(username: str, group_mapping: dict[str, set[str]] | None = None) -> set[str]:
    subjects = {username}
    if username != ANONYMOUS_USERNAME:
        subjects.add(AUTHENTICATED_SUBJECT)

    resolved_group_mapping = group_mapping if group_mapping is not None else {}
    subjects.update(resolved_group_mapping.get(username, set()))
    return subjects


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
    vectorstore: Any,
    username_group_mapping_file: Path,
) -> tuple[list[str], list[str], list[str]]:
    manifest = load_document_access_manifest(vectorstore)
    group_mapping = load_username_group_mapping(username_group_mapping_file)
    user_subjects = resolve_user_subjects(username, group_mapping)
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


def resolve_accessible_source_files(
    username: str,
    requested_source_files: list[str],
    vectorstore: Any,
    username_group_mapping_file: Path,
) -> tuple[list[str], list[str], list[str]]:
    manifest = load_document_access_manifest(vectorstore)
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