"""Parse uploaded CSVs / pasted URLs, map columns to canonical fields, and build
a preview plus the internal job list.

Two realistic CSV cases are handled (header row, or headerless with content
detection). Pasted URLs are handled by parse_urls_text.

Name handling: when a row has no name we guess one from the URL slug, marking
name_source="url_guess". Missing NAME variables are treated as soft warnings (the
runner captures the real name from the profile page and re-renders at send time),
while missing non-name variables, unknown templates, and over-limit notes are
hard issues that flag the row.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Callable

from . import db
from .models import (
    ACTIONS_NEEDING_MESSAGE,
    ActionType,
    ItemStatus,
    PreviewRow,
    TERMINAL_CONTACTED,
)
from .names import guess_name_from_url, split_full_name
from .settings import Settings
from .templating import render

CANONICAL_FIELDS = [
    "linkedin_url", "name", "first_name", "last_name",
    "company", "role", "email", "template", "action",
]

NAME_VARS = {"first_name", "last_name", "full_name"}

# A template/body resolver: given the per-row template name, return
# (template_id, template_name, body). None name => use the default.
TemplateResolver = Callable[[str], "tuple[int | None, str, str]"]


URL_PATTERN = re.compile(r'https?://[a-zA-Z0-9.-]*linkedin\.com/in/[a-zA-Z0-9_-]+/?')

def parse_csv_bytes(raw: bytes) -> list[list[str]]:
    if raw.startswith(b'PK\x03\x04'):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        sheet = wb.active
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None and str(cell).strip() for cell in row):
                rows.append([(str(cell).strip() if cell is not None else "") for cell in row])
        return rows

    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows: list[list[str]] = []
    for row in reader:
        if any((cell or "").strip() for cell in row):
            rows.append([(cell or "").strip() for cell in row])
    return rows


def parse_urls_text(text: str) -> list[list[str]]:
    """Parse a freeform block of URLs into [url, first, last, company, role] rows."""
    rows: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        
        match = URL_PATTERN.search(line)
        if not match:
            continue
            
        url = match.group(0)
        prefix = line[:match.start()].strip(" ,;\t\r\n-")
        first, last = "", ""
        if prefix:
            first, last = split_full_name(prefix)
            
        rows.append([url, first, last, "", ""])
    return rows


def _looks_like_url(value: str) -> bool:
    v = value.strip().lower()
    return v.startswith("http") or "linkedin.com" in v


def _build_header_index(headers: list[str], mapping: dict[str, list[str]]) -> dict[str, int]:
    lower_to_index: dict[str, int] = {}
    for i, h in enumerate(headers):
        key = h.strip().lower()
        if key and key not in lower_to_index:
            lower_to_index[key] = i
    resolved: dict[str, int] = {}
    for field, candidates in mapping.items():
        for cand in candidates:
            idx = lower_to_index.get(cand.strip().lower())
            if idx is not None:
                resolved[field] = idx
                break
    return resolved


def _detect_columns_by_content(raw_rows: list[list[str]], template_keys: set[str]) -> dict[str, int]:
    ncols = max((len(r) for r in raw_rows), default=0)
    columns = [[(r[i] if i < len(r) else "") for r in raw_rows] for i in range(ncols)]
    resolved: dict[str, int] = {}
    used: set[int] = set()

    def fraction(values: list[str], pred) -> float:
        nonempty = [v for v in values if v]
        if not nonempty:
            return 0.0
        return sum(1 for v in nonempty if pred(v)) / len(nonempty)

    for i, vals in enumerate(columns):
        if fraction(vals, _looks_like_url) >= 0.5:
            resolved["linkedin_url"] = i
            used.add(i)
            break
    tkeys = {k.lower() for k in template_keys}
    for i, vals in enumerate(columns):
        if i in used:
            continue
        if tkeys and fraction(vals, lambda v: v.lower() in tkeys) >= 0.5:
            resolved["template"] = i
            used.add(i)
            break
    for i, vals in enumerate(columns):
        if i in used:
            continue
        if fraction(vals, lambda v: "@" in v) >= 0.5:
            resolved["email"] = i
            used.add(i)
            break
    for i, vals in enumerate(columns):
        if i in used:
            continue
        if fraction(vals, lambda v: any(c.isalpha() for c in v) and not _looks_like_url(v)) >= 0.5:
            resolved["name"] = i
            used.add(i)
            break
    return resolved


def _resolve_table(
    settings: Settings, raw_rows: list[list[str]], template_keys: set[str]
) -> tuple[dict[str, int], list[list[str]], list[str]]:
    notes: list[str] = []
    if not raw_rows:
        return {}, [], notes

    mapping = {**settings.column_mapping}
    mapping.setdefault("action", ["action", "Action"])

    header_candidate = raw_rows[0]
    header_index = _build_header_index(header_candidate, mapping)
    header_has_url_value = any(_looks_like_url(c) for c in header_candidate)
    has_header = ("linkedin_url" in header_index) and not header_has_url_value

    if has_header:
        named = ", ".join(
            f"column {idx + 1} -> {field}"
            for field, idx in sorted(header_index.items(), key=lambda kv: kv[1])
        )
        notes.append(f"Detected a header row. Mapped {named}.")
        return header_index, raw_rows[1:], notes

    resolved = _detect_columns_by_content(raw_rows, template_keys)
    if resolved:
        named = ", ".join(
            f"column {idx + 1} -> {field}"
            for field, idx in sorted(resolved.items(), key=lambda kv: kv[1])
        )
        notes.append(
            "No header row detected, so columns were auto-detected by content: "
            f"{named}. If this is wrong, add a header row (e.g. url,name,template)."
        )
    else:
        notes.append(
            "Could not detect a header row or recognize any columns. "
            "Add a header row such as: url,name,template"
        )
    return resolved, raw_rows, notes


def _explain(issue: str) -> str:
    if issue == "missing linkedin_url":
        return "No LinkedIn URL in this row, so there is nobody to message."
    if issue == "previously contacted":
        return "Already contacted in a previous run, so it will be skipped."
    if issue == "no message template provided":
        return "This action needs a message, but no template/body was provided."
    if issue.startswith("unknown template"):
        name = issue.split(":", 1)[1].strip() if ":" in issue else ""
        return f"Template {name or '(blank)'} is not defined."
    if issue.startswith("missing variable:"):
        var = issue.split(":", 1)[1].strip()
        return f"The template needs a {{{var}}} value, but this row has none."
    if issue.startswith("exceeds"):
        return issue
    return issue


def _note_limit_for(action: ActionType) -> int | None:
    return 300 if action in {ActionType.AUTO, ActionType.CONNECT_NOTE} else None


def _build_one(
    idx: int,
    *,
    linkedin_url: str,
    first_name: str,
    last_name: str,
    full_name_in: str,
    company: str,
    role: str,
    email: str,
    row_action: str,
    template_id: int | None,
    template_name: str,
    template_body: str,
    action: ActionType,
    sender: str,
) -> tuple[PreviewRow, dict[str, Any]]:
    name_source = "csv" if (first_name or last_name or full_name_in) else ""

    if full_name_in and not first_name:
        f, l = split_full_name(full_name_in)
        first_name = f
        if not last_name:
            last_name = l
    if not first_name and linkedin_url:
        gf, gl = guess_name_from_url(linkedin_url)
        if gf:
            first_name = gf
            if not last_name:
                last_name = gl
            name_source = "url_guess"

    full_name = full_name_in or f"{first_name} {last_name}".strip()

    issues_hard: list[str] = []
    issues_soft: list[str] = []
    if not linkedin_url:
        issues_hard.append("missing linkedin_url")

    variables = {
        "first_name": first_name, "last_name": last_name, "full_name": full_name,
        "company": company, "role": role, "email": email, "sender": sender,
    }
    note_limit = _note_limit_for(action)
    rendered = ""
    needs_msg = action in ACTIONS_NEEDING_MESSAGE

    if not template_body:
        if needs_msg:
            issues_hard.append("no message template provided")
    else:
        rendered, rissues = render(template_body, variables, note_limit)
        for it in rissues:
            if it.startswith("missing variable:"):
                var = it.split(":", 1)[1].strip()
                if var in NAME_VARS and name_source != "csv":
                    issues_soft.append(f"{var} will be filled from the profile at send time")
                else:
                    issues_hard.append(it)
            else:
                issues_hard.append(it)

    already = db.is_already_contacted(linkedin_url, TERMINAL_CONTACTED) if linkedin_url else False

    template_ok = not issues_hard
    all_issues = [_explain(i) for i in issues_hard] + issues_soft
    if already:
        all_issues.append(_explain("previously contacted"))

    row = PreviewRow(
        row_index=idx,
        linkedin_url=linkedin_url,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        company=company,
        role=role,
        email=email,
        action=(row_action or action.value),
        template=template_name,
        rendered_message=rendered,
        char_count=len(rendered),
        template_ok=template_ok,
        name_source=name_source,
        already_contacted=already,
        issues=all_issues,
    )

    if not linkedin_url or not template_ok:
        precomputed = ItemStatus.NEEDS_ATTENTION.value
    elif already:
        precomputed = ItemStatus.SKIPPED_DEDUP.value
    else:
        precomputed = ItemStatus.QUEUED.value

    job = {
        "row_index": idx,
        "linkedin_url": linkedin_url,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "company": company,
        "role": role,
        "email": email,
        "action": row_action or "",
        "template": template_name,
        "template_id": template_id,
        "template_body": template_body,
        "variables": variables,
        "name_source": name_source,
        "message": rendered,
        "precomputed_status": precomputed,
        "issues": all_issues,
    }
    return row, job


def build_preview(
    settings: Settings,
    operator: str,
    raw_rows: list[list[str]],
    *,
    action: ActionType,
    resolve_template: TemplateResolver,
    template_keys: set[str],
) -> tuple[list[PreviewRow], list[dict[str, Any]], list[str]]:
    field_index, data_rows, notes = _resolve_table(settings, raw_rows, template_keys)
    sender = _sender_first_name(settings, operator)

    preview: list[PreviewRow] = []
    jobs: list[dict[str, Any]] = []
    for idx, row in enumerate(data_rows):
        def field(name: str) -> str:
            ci = field_index.get(name)
            if ci is None or ci >= len(row):
                return ""
            return (row[ci] or "").strip()

        row_template = field("template")
        template_id, template_name, body = resolve_template(row_template or None)
        row_action = field("action")

        pr, job = _build_one(
            idx,
            linkedin_url=field("linkedin_url"),
            first_name=field("first_name"),
            last_name=field("last_name"),
            full_name_in=field("name"),
            company=field("company"),
            role=field("role"),
            email=field("email"),
            row_action=row_action,
            template_id=template_id,
            template_name=template_name,
            template_body=body,
            action=action,
            sender=sender,
        )
        preview.append(pr)
        jobs.append(job)
    return preview, jobs, notes


def build_preview_from_urls(
    settings: Settings,
    operator: str,
    urls_text: str,
    *,
    action: ActionType,
    template_id: int | None,
    template_name: str,
    template_body: str,
) -> tuple[list[PreviewRow], list[dict[str, Any]], list[str]]:
    raw_rows = parse_urls_text(urls_text)
    if not raw_rows:
        return [], [], ["No valid LinkedIn URLs found. Paste one URL per line."]
    sender = _sender_first_name(settings, operator)

    preview: list[PreviewRow] = []
    jobs: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_rows):
        pr, job = _build_one(
            idx,
            linkedin_url=(row[0] if len(row) > 0 else ""),
            first_name=(row[1] if len(row) > 1 else ""),
            last_name=(row[2] if len(row) > 2 else ""),
            full_name_in="",
            company=(row[3] if len(row) > 3 else ""),
            role=(row[4] if len(row) > 4 else ""),
            email="",
            row_action="",
            template_id=template_id,
            template_name=template_name,
            template_body=template_body,
            action=action,
            sender=sender,
        )
        preview.append(pr)
        jobs.append(job)
    notes = [f"Direct URL mode: {len(preview)} profile(s) loaded."]
    return preview, jobs, notes


def build_jobs_from_profiles(
    settings: Settings,
    operator: str,
    profiles: list[dict[str, Any]],
    *,
    action: ActionType,
    template_id: int | None,
    template_name: str,
    template_body: str,
) -> tuple[list[PreviewRow], list[dict[str, Any]], list[str]]:
    """Build jobs from a list of structured profile dicts (programmatic API).

    Each profile: {linkedin_url, first_name?, last_name?, full_name?, company?,
    role?, email?}.
    """
    sender = _sender_first_name(settings, operator)
    preview: list[PreviewRow] = []
    jobs: list[dict[str, Any]] = []
    for idx, p in enumerate(profiles):
        pr, job = _build_one(
            idx,
            linkedin_url=str(p.get("linkedin_url") or p.get("url") or "").strip(),
            first_name=str(p.get("first_name") or "").strip(),
            last_name=str(p.get("last_name") or "").strip(),
            full_name_in=str(p.get("full_name") or p.get("name") or "").strip(),
            company=str(p.get("company") or "").strip(),
            role=str(p.get("role") or "").strip(),
            email=str(p.get("email") or "").strip(),
            row_action="",
            template_id=template_id,
            template_name=template_name,
            template_body=template_body,
            action=action,
            sender=sender,
        )
        preview.append(pr)
        jobs.append(job)
    notes = [f"Programmatic enqueue: {len(jobs)} profile(s)."]
    return preview, jobs, notes


def _sender_first_name(settings: Settings, operator: str) -> str:
    op_cfg = settings.operators.get(operator)
    sender_full = (op_cfg.label if op_cfg else operator) or operator
    return sender_full.split()[0] if sender_full else ""
