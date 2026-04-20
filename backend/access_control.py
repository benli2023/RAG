import glob
import json
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


def load_knowledge_base_group_mapping(mapping_file: Path) -> dict[str, set[str]]:
    if not mapping_file.is_file():
        return {}

    try:
        with open(mapping_file, "r", encoding="utf-8") as file_handle:
            raw_mapping = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ACL] failed to load knowledge base group mapping: {exc}")
        return {}

    if not isinstance(raw_mapping, dict):
        print("[ACL] ignore knowledge base group mapping: root value must be a JSON object")
        return {}

    mapping: dict[str, set[str]] = {}
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
            mapping[group_name] = knowledge_bases

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


def load_document_access_manifest(docs_dir: Path) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    md_files = sorted(glob.glob(f"{docs_dir}/**/*.md", recursive=True))

    for file_path in md_files:
        path_obj = Path(file_path)
        with open(path_obj, "r", encoding="utf-8") as file_handle:
            post = frontmatter.load(file_handle)

        metadata = post.metadata if isinstance(post.metadata, dict) else {}
        domain = str(metadata.get("domain") or path_obj.parent.name or "global").strip()
        source_file = str(path_obj.resolve().relative_to(docs_dir.resolve())).replace("\\", "/")
        acl_subjects = parse_acl_subjects(metadata)

        manifest.append({
            "domain": domain,
            "source_file": source_file,
            "acl_subjects": acl_subjects,
        })

    return manifest


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

    if not normalized_requested_domains and not normalized_requested_source_files:
        accessible_source_files, _, accessible_domains = resolve_accessible_sources(
            username,
            [],
            docs_dir,
            username_group_mapping_file,
        )
        return accessible_domains, accessible_source_files

    accessible_domains: list[str] = []
    accessible_source_files: list[str] = []

    if normalized_requested_domains:
        domain_source_files, _, domain_accessible_domains = resolve_accessible_sources(
            username,
            normalized_requested_domains,
            docs_dir,
            username_group_mapping_file,
        )
        accessible_source_files.extend(domain_source_files)
        accessible_domains = domain_accessible_domains

    if normalized_requested_source_files:
        source_file_source_files, _, source_file_accessible_domains = resolve_accessible_source_files(
            username,
            normalized_requested_source_files,
            docs_dir,
            username_group_mapping_file,
        )
        accessible_source_files.extend(source_file_source_files)
        if not normalized_requested_domains:
            accessible_domains = source_file_accessible_domains

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