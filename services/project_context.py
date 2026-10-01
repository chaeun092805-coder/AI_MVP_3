import re

from config import ACTIVE_PROJECT_SEARCH_THRESHOLD, ACTIVE_PROJECT_SELECTION_MARGIN


ACTIVE_PROJECT_TERMS = (
    "새 프로젝트", "진행중인 프로젝트", "진행 중인 프로젝트", "현재 프로젝트",
    "현재 진행 프로젝트", "요즘 하는 프로젝트", "현재 진행하는 프로젝트",
    "새로 진행 중인 프로젝트", "진행 중인 행사", "현재 진행중인 행사",
    "현재 진행 중인 행사", "진행중 행사", "지금 하는 행사", "요즘 하는 행사",
    "진행 중인 기획", "현재 기획", "진행중인 업무", "진행 중인 업무",
)
CURRENT_PROJECT_TERMS = (
    "이번 프로젝트", "이번 행사", "이번 건", "현재 프로젝트", "현재 행사",
    "지금 프로젝트", "지금 진행하는 거", "진행중인 프로젝트", "진행 중인 프로젝트",
    "진행 중인 행사", "최근 진행하는 프로젝트", "요즘 하는 프로젝트",
    "현재 진행 상황", "지금 어떻게 되고 있어",
)
FOLLOWUP_TERMS = (
    "그중", "이 중", "여기서", "얘네 중", "진행중인 것 중", "새 프로젝트 중",
    "그 프로젝트들", "이 프로젝트들", "각각", "둘 중", "어느 프로젝트",
    "어떤 프로젝트", "그거", "그 프로젝트", "그 행사",
)
DETAIL_TERMS = (
    "일정", "담당자", "예산", "취소", "장소", "결정", "변경", "언제", "누구",
    "얼마", "진행 상황", "자세히",
)
REGISTRY_MARKERS = ("문서관리대장", "문서 관리 대장")
STATUS_FIELDS = ("진행상황", "진행 상태", "progress_status", "status")
ACTIVE_STATUS = "진행중"


def new_session_context():
    return {
        "active_project_scope": None,
        "active_entity": None,
        "recent_document_ids": [],
        "recent_chunk_ids": [],
        "unverified_user_facts": [],
    }


def _compact(value):
    return re.sub(r"\s+", "", str(value or "")).lower()


def normalize_status(value):
    return _compact(value)


def is_active_project_list_question(question):
    compact = _compact(question)
    return any(_compact(term) in compact for term in ACTIVE_PROJECT_TERMS)


def is_current_project_question(question):
    compact = _compact(question)
    if any(_compact(term) in compact for term in CURRENT_PROJECT_TERMS):
        return True
    words = [word for word in re.split(r"\s+", question.strip()) if word]
    return len(words) <= 5 and any(term in question for term in DETAIL_TERMS)


def is_followup_question(question):
    compact = _compact(question)
    return any(_compact(term) in compact for term in FOLLOWUP_TERMS) or (
        len(question.strip()) <= 20 and any(term in question for term in DETAIL_TERMS)
    )


def _registry_documents(con):
    rows = [dict(row) for row in con.execute("SELECT * FROM documents")]
    return [
        row for row in rows
        if any(marker in row.get("title", "") or marker in row.get("document_type", "")
               for marker in REGISTRY_MARKERS)
    ]


def _row_to_project(parts, registry):
    cleaned = [part.strip() for part in parts if part.strip()]
    status_index = next(
        (index for index, value in enumerate(cleaned) if _compact(value) == ACTIVE_STATUS), None
    )
    if status_index is None:
        return None
    candidates = [value for value in cleaned[:status_index] if _compact(value) not in {
        _compact(field) for field in STATUS_FIELDS
    }]
    if not candidates:
        return None
    project_id = next(
        (value.upper() for value in candidates if re.fullmatch(r"U-?\d{2}", value, re.I)),
        registry.get("project_id", ""),
    )
    project_id = project_id.replace("U", "U-").replace("--", "-") if project_id else ""
    name = next((value for value in candidates if value != project_id and len(value) > 2), candidates[0])
    document_id = next(
        (value for value in cleaned[status_index + 1:] if value != ACTIVE_STATUS),
        registry["document_id"],
    )
    return {
        "name": name,
        "document_id": document_id,
        "project_id": project_id,
        "status": ACTIVE_STATUS,
        "registry_document_id": registry["document_id"],
    }


def _parse_registry(registry):
    content = registry.get("content", "")
    projects = []
    # Prefer explicit table rows; this covers Markdown, CSV, TSV, and copied Sheets tables.
    headers = None
    for line in content.splitlines():
        parts = re.split(r"\s*[|,\t]\s*", line.strip().strip("|"))
        if len(parts) < 2:
            headers = None
            continue
        normalized = [_compact(part) for part in parts]
        if any(value in {_compact(field) for field in STATUS_FIELDS} for value in normalized):
            headers = normalized
            continue
        item = None
        if headers and len(parts) >= len(headers):
            status_at = next(
                (index for index, value in enumerate(headers)
                 if value in {_compact(field) for field in STATUS_FIELDS}), None
            )
            if status_at is not None and _compact(parts[status_at]) == ACTIVE_STATUS:
                name_at = next(
                    (index for index, value in enumerate(headers)
                     if value in {"프로젝트명", "문서명", "파일제목", "title", "name", "projectname"}), 0
                )
                project_at = next(
                    (index for index, value in enumerate(headers)
                     if value in {"프로젝트id", "projectid"}), None
                )
                document_at = next(
                    (index for index, value in enumerate(headers)
                     if value in {"문서id", "documentid"}), None
                )
                item = {
                    "name": parts[name_at].strip(),
                    "document_id": parts[document_at].strip() if document_at is not None else registry["document_id"],
                    "project_id": parts[project_at].strip() if project_at is not None else registry.get("project_id", ""),
                    "status": ACTIVE_STATUS,
                    "registry_document_id": registry["document_id"],
                }
        elif ACTIVE_STATUS in _compact(line):
            item = _row_to_project(parts, registry)
        if item:
            projects.append(item)
    # Also accept labelled blocks used by plain-text exports.
    block_pattern = re.compile(
        r"(?:프로젝트명|문서명|name)\s*[:：]\s*(?P<name>[^\n]+).*?"
        r"(?:진행상황|진행\s*상태|progress_status|status)\s*[:：]\s*(?P<status>[^\n]+)"
        r"(?:.*?(?:문서\s*ID|document_id)\s*[:：]\s*(?P<document_id>[^\n]+))?",
        re.I | re.S,
    )
    for match in block_pattern.finditer(content):
        if _compact(match.group("status")) != ACTIVE_STATUS:
            continue
        projects.append({
            "name": match.group("name").strip(),
            "document_id": (match.group("document_id") or registry["document_id"]).strip(),
            "project_id": registry.get("project_id", ""),
            "status": ACTIVE_STATUS,
            "registry_document_id": registry["document_id"],
        })
    return projects


def load_active_projects(con):
    projects = []
    registries = _registry_documents(con)
    print(f"[REGISTRY] registry loaded: {str(bool(registries)).lower()}")
    for registry in registries:
        lines = [line for line in registry.get("content", "").splitlines() if line.strip()]
        columns = re.split(r"\s*[|,\t]\s*", lines[0].strip().strip("|")) if lines else []
        parsed = _parse_registry(registry)
        status_at = next(
            (index for index, value in enumerate(columns)
             if _compact(value) in {_compact(field) for field in STATUS_FIELDS}), None
        )
        status_values = []
        if status_at is not None:
            for line in lines[1:]:
                parts = re.split(r"\s*[|,\t]\s*", line.strip().strip("|"))
                if len(parts) > status_at:
                    status_values.append(parts[status_at].strip())
        print(f"[REGISTRY] row count: {max(len(lines) - 1, 0)}")
        print(f"[REGISTRY] columns: {columns}")
        print(f"[REGISTRY] status values: {sorted(set(status_values))}")
        print(f"[REGISTRY] active rows: {len(parsed)}")
        projects.extend(parsed)
    documents = [dict(row) for row in con.execute(
        "SELECT document_id,document_family_id,project_id,title,source_drive_url FROM documents"
    )]
    for item in projects:
        referenced = next(
            (row for row in documents if row["document_id"] == item.get("document_id")
             or row["document_family_id"] == item.get("document_id")), None
        )
        if not referenced:
            key = _compact(item.get("name"))
            title_matches = [row for row in documents if key and key in _compact(row["title"])]
            project_ids_found = {row["project_id"] for row in title_matches}
            referenced = title_matches[0] if len(project_ids_found) == 1 and title_matches else None
        if referenced:
            item["project_id"] = referenced["project_id"]
            item["document_id"] = referenced["document_id"]
            item["search_document_id"] = referenced["document_id"]
            print(f"[REGISTRY] active document: {item['name']}")
            print(
                "[PROJECT_CONTEXT] matched search document: "
                f"title={referenced['title']} document_id={referenced['document_id']}"
            )
        else:
            item["search_document_id"] = ""
            print("[PROJECT_CONTEXT] document mapping FAILED")
            print(f"[PROJECT_CONTEXT] registry title={item['name']}")
    unique = {}
    for item in projects:
        key = (item["project_id"], _compact(item["name"]), item["document_id"])
        unique[key] = item
    return list(unique.values())


def active_project_answer(projects):
    if not projects:
        return "### 현재 진행 중인 프로젝트\n\n문서관리대장에서 진행중 상태인 프로젝트를 찾지 못했습니다."
    lines = [
        "### 현재 진행 중인 프로젝트", "",
        f"현재 문서관리대장 기준으로 진행중 상태인 프로젝트는 {len(projects)}건입니다.", "",
    ]
    lines.extend(f"- {item['name']}" for item in projects)
    return "\n".join(lines)


def explicit_project(projects, question):
    compact = _compact(question)
    matches = []
    for item in projects:
        names = (item.get("name", ""), item.get("project_id", ""), item.get("document_id", ""))
        if any(len(_compact(name)) >= 3 and _compact(name) in compact for name in names):
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def project_ids(projects):
    return sorted({item["project_id"] for item in projects if item.get("project_id")})


def select_entity_from_results(projects, results):
    if not results:
        return None, 0.0, False
    best_by_project = {}
    for row in results:
        pid = row.get("project_id", "")
        best_by_project[pid] = max(best_by_project.get(pid, 0.0), row.get("score", 0.0))
    ranked = sorted(best_by_project.items(), key=lambda item: item[1], reverse=True)
    best_pid, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    clear = best_score >= ACTIVE_PROJECT_SEARCH_THRESHOLD and (
        len(ranked) == 1 or best_score - runner_up >= ACTIVE_PROJECT_SELECTION_MARGIN
    )
    entity = next((item for item in projects if item.get("project_id") == best_pid), None)
    return (entity if clear else None), best_score, not clear
