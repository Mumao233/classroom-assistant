"""HTTP text limits shared with the UI through /api/state.

Lengths use Unicode code points (Python len), including whitespace before any
existing storage normalization. A combined emoji may contain several points.
"""

INPUT_LIMITS = {"text": 12000, "question": 4000, "syllabus": 12000}


def require_content(value: str) -> str:
    if not value or value.isspace():
        raise ValueError("blank_text")
    return value


def validation_message(path: str, errors: list[dict]) -> str:
    # Whitelist text endpoints; never echo values, exception context, credentials,
    # or arbitrary field names from Pydantic's default validation response.
    if path == "/api/text":
        field, label = "text", "原文"
    elif path.startswith("/api/segments/") and path.endswith("/edit"):
        field, label = "text", "修正后的原文"
    elif path == "/api/ask":
        field, label = "question", "问题"
    elif path.startswith("/api/courses/") and path.endswith("/glossary"):
        field, label = "syllabus", "大纲"
    else:
        return "提交的数据格式无效，请检查输入。"
    for error in errors:
        if error.get("loc") != ("body", field):
            continue
        value = error.get("input")
        if isinstance(value, str):
            maximum = INPUT_LIMITS[field]
            if len(value) > maximum:
                return f"{label}最多 {maximum} 个字符，当前 {len(value)} 个（按 Unicode 码点计数，含空白）；请缩短后重试。"
            if field != "syllabus" and (not value or value.isspace()):
                return f"{label}不能为空或仅包含空白。"
        return f"{label}格式无效，请输入文字。"
    return "提交的数据格式无效，请检查输入。"
