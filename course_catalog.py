"""Validated, local course vocabulary; vocabulary is not classroom evidence."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = Path(__file__).with_name("course_presets.json")


class GlossaryValidationError(ValueError):
    """A safe explanation of an invalid glossary response or file."""


def _short_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise GlossaryValidationError(f"{label}必须是文字。")
    value = re.sub(r"\s+", " ", value).strip()
    if not value or len(value) > limit or "\x00" in value:
        raise GlossaryValidationError(f"{label}为空或过长。")
    return value


def parse_json_response(raw: str) -> Any:
    """Accept JSON or one fenced JSON block, not arbitrary surrounding prose."""
    text = str(raw).strip().lstrip("\ufeff")
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise GlossaryValidationError("术语结果不是完整的 JSON，请重试。") from None


def validate_glossary(value: Any) -> list[dict[str, Any]]:
    """Normalize a glossary, rejecting incomplete entries rather than guessing.

    Accepts the public list form, or {glossary: [...]} / {terms: [...]}.
    Duplicate English terms are merged case-insensitively; their first meaning
    is retained and aliases are combined. No fact claims are added.
    """
    if isinstance(value, dict):
        value = value.get("glossary", value.get("terms"))
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise GlossaryValidationError("术语表需要包含 1–200 个术语。")
    result: list[dict[str, Any]] = []
    by_term: dict[str, dict[str, Any]] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise GlossaryValidationError("每个术语必须包含 term、meaning 和 aliases。")
        term = _short_text(entry.get("term"), "英文术语", 120)
        meaning = _short_text(entry.get("meaning"), "中文释义", 240)
        if not re.search(r"[A-Za-z]", term):
            raise GlossaryValidationError("英文术语需要包含英文字母。")
        if not re.search(r"[\u3400-\u9fff]", meaning):
            raise GlossaryValidationError("术语释义需要包含中文。")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 12:
            raise GlossaryValidationError("aliases 必须是最多 12 个缩写或别名组成的列表。")
        clean_aliases: list[str] = []
        alias_keys = {term.casefold()}
        for alias in aliases:
            alias = _short_text(alias, "术语别名", 80)
            if alias.casefold() not in alias_keys:
                clean_aliases.append(alias)
                alias_keys.add(alias.casefold())
        key = term.casefold()
        if key in by_term:
            old = by_term[key]
            keys = {old["term"].casefold(), *(a.casefold() for a in old["aliases"])}
            for alias in clean_aliases:
                if alias.casefold() not in keys and len(old["aliases"]) < 12:
                    old["aliases"].append(alias)
                    keys.add(alias.casefold())
        else:
            normalized = {"term": term, "meaning": meaning, "aliases": clean_aliases}
            by_term[key] = normalized
            result.append(normalized)
    return result


def parse_glossary(raw: str) -> list[dict[str, Any]]:
    return validate_glossary(parse_json_response(raw))


def glossary_terms(glossary: list[dict[str, Any]]) -> str:
    """Full English vocabulary string for storage; not the ASR prompt budget."""
    terms = validate_glossary(glossary)
    return ", ".join(entry["term"] for entry in terms)


def validate_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("courses"), list):
        raise GlossaryValidationError("课程预设需要包含 courses 列表。")
    if len(value["courses"]) > 100:
        raise GlossaryValidationError("课程预设数量过多。")
    courses = []
    ids = set()
    for course in value["courses"]:
        if not isinstance(course, dict):
            raise GlossaryValidationError("课程预设格式无效。")
        identifier = _short_text(course.get("id"), "课程编号", 100)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in ids:
            raise GlossaryValidationError("课程编号无效或重复。")
        ids.add(identifier)
        name = _short_text(course.get("name"), "课程名称", 160)
        glossary = validate_glossary(course.get("glossary"))
        generated_at = _short_text(course.get("generated_at"), "生成时间", 64)
        if course.get("status") != "ready":
            raise GlossaryValidationError("仅保存已生成并校验成功的课程预设。")
        courses.append({
            "id": identifier, "name": name, "glossary": glossary,
            "terms": glossary_terms(glossary), "status": "ready", "generated_at": generated_at,
        })
    return {"courses": courses}


def load_catalog(path: str | Path = DEFAULT_CATALOG) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"courses": []}
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError:
        raise GlossaryValidationError("无法读取课程预设文件，原文件未修改。") from None
    return validate_catalog(parse_json_response(raw))


def save_catalog(value: Any, path: str | Path = DEFAULT_CATALOG) -> dict[str, Any]:
    catalog = validate_catalog(value)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=target.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(catalog, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return catalog
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def make_asr_prompt(user_terms: str = "", preset_terms: list | str = "") -> str:
    """At most 200 characters, current chapter/user terms before broad presets.

    This is a vocabulary hint only: never instruct the recognizer to insert
    content or treat preset definitions as words that the teacher actually said.
    """
    pieces = []
    if isinstance(user_terms, str):
        pieces.extend(re.split(r"[,，;；\n\r]+", user_terms))
    if isinstance(preset_terms, str):
        pieces.extend(re.split(r"[,，;；\n\r]+", preset_terms))
    elif isinstance(preset_terms, list):
        for entry in preset_terms:
            if isinstance(entry, str):
                pieces.append(entry)
            elif isinstance(entry, dict):
                pieces.append(entry.get("term", ""))
                aliases = entry.get("aliases", [])
                if isinstance(aliases, list):
                    pieces.extend(a for a in aliases if isinstance(a, str))
    chosen = []
    seen = set()
    used = 0
    for piece in pieces:
        if not isinstance(piece, str):
            continue
        piece = re.sub(r"\s+", " ", piece.replace("\x00", " ")).strip()
        if not piece or piece.casefold() in seen:
            continue
        extra = len(piece) + (2 if chosen else 0)
        if used + extra > 200:
            if not chosen:
                return piece[:200]
            # Preserve priority order; do not fill the gap with later presets.
            break
        chosen.append(piece)
        seen.add(piece.casefold())
        used += extra
    return ", ".join(chosen)
