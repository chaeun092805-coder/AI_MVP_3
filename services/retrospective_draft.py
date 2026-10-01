import hashlib
import io
import json
import re
import zipfile
from copy import deepcopy
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET


TEMPLATE_FILE_ID = "16SWX544w2G94MrJ5MU0zclW4cwttf64i"
TEMPLATE_PATH = Path("data/templates/초안 회고록 양식.docx")
INPUT_NEEDED = "[입력 필요]"
IN_PROGRESS = "[프로젝트 진행중 - 추후 입력]"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W_NS)


def _drive_id(url):
    match = re.search(r"/(?:d|file/d)/([^/?]+)", url or "")
    return match.group(1) if match else ""


def _value(item):
    return (item.get("user_edited_text") or item.get("summary") or "").strip()


def grouped_candidates(con):
    rows = [dict(row) for row in con.execute(
        """SELECT c.*, d.title AS source_document_title, d.source_drive_url,
                  d.status AS document_status, d.created_at AS document_updated_at
           FROM memory_candidates c
           LEFT JOIN documents d ON d.document_id=c.source_document_id
           WHERE c.status='pending_review' AND (c.finalized_at='' OR c.drive_status='error')
           ORDER BY c.project_id,c.candidate_id"""
    )]
    groups = {}
    for item in rows:
        fallback = con.execute(
            """SELECT * FROM documents WHERE project_id=?
               ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END, created_at DESC LIMIT 1""",
            (item["project_id"],),
        ).fetchone()
        fallback = dict(fallback) if fallback else {}
        document_id = item.get("source_document_id") or fallback.get("document_id", "")
        drive_id = item.get("source_drive_file_id") or _drive_id(
            item.get("source_drive_url") or fallback.get("source_drive_url", "")
        )
        title = item.get("source_document_title") or fallback.get("title") or item["project_id"]
        normalized = re.sub(r"\W+", "", title).lower()
        key = drive_id or document_id or normalized or item["project_id"]
        group = groups.setdefault(key, {
            "key": key, "project_id": item["project_id"], "document_id": document_id,
            "drive_file_id": drive_id, "title": title,
            "status": item.get("document_status") or fallback.get("status", ""),
            "updated_at": item.get("document_updated_at") or fallback.get("created_at", ""),
            "items": [],
        })
        group["items"].append(item)
    return list(groups.values())


def _field(text, *labels):
    for label in labels:
        match = re.search(rf"(?mi)^\s*{re.escape(label)}\s*[:：]?\s*(?:\n\s*)?([^\n|]+)", text or "")
        if match and match.group(1).strip(" _-"):
            return match.group(1).strip()
    return ""


def _project_documents(con, project_id):
    return [dict(row) for row in con.execute(
        """SELECT * FROM documents WHERE project_id=?
           ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END, created_at DESC""",
        (project_id,),
    )]


def build_draft(con, project_id, candidate_ids):
    placeholders = ",".join("?" for _ in candidate_ids)
    candidates = [dict(row) for row in con.execute(
        f"SELECT * FROM memory_candidates WHERE candidate_id IN ({placeholders}) AND project_id=? ORDER BY candidate_id",
        (*candidate_ids, project_id),
    )] if candidate_ids else []
    if len(candidates) != len(set(candidate_ids)):
        raise ValueError("선택한 기억 후보 중 이 프로젝트에 속하지 않는 항목이 있습니다.")
    documents = _project_documents(con, project_id)
    primary = documents[0] if documents else {}
    combined = "\n".join(item.get("content", "") for item in documents)
    project = con.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    project = dict(project) if project else {}
    active = not any(item.get("document_type") == "성과보고서" and item.get("status") == "approved" for item in documents)
    project_name = project.get("project_name") or _field(combined, "프로젝트명") or primary.get("title") or INPUT_NEEDED
    evidence = [
        {"document_id": item["document_id"], "name": item["title"], "version": item["version"],
         "status": item["status"], "priority": 1 if item["document_type"] in {"기획안", "성과보고서", "회의록"} else 2}
        for item in documents
    ]
    memories = []
    issues = []
    gaps = []
    placements = {}
    sources = {}
    reason_terms = ("때문", "위해", "이유", "위험", "문제", "따라", "대응")
    action_terms = ("변경", "분리", "전환", "추가", "취소", "배치", "도입", "초청")
    for number, item in enumerate(candidates, 1):
        content = _value(item)
        memory_id = f"MEM-{number:03d}"
        memories.append({"id": memory_id, "candidate_id": item["candidate_id"], "title": item["title"],
                         "content": content, "source": "업무 대화", "reuse_condition": INPUT_NEEDED,
                         "status": "임시", "selection": "저장"})
        placement = ["08 조직기억 후보", "09 최종 조직기억 카드"]
        if any(term in content for term in ("문제", "위험", "장애", "저하", "대응", "조치", "분리", "전환")):
            issues.append({"memory_id": memory_id, "issue": content, "impact": INPUT_NEEDED,
                           "action": content if any(term in content for term in action_terms) else INPUT_NEEDED,
                           "result": INPUT_NEEDED, "limitation": INPUT_NEEDED,
                           "source": f"{project_id} / {memory_id}"})
            placement.extend(["05 시행착오", "09 해결방법", "11 다음 프로젝트 적용 액션"])
        if any(term in content for term in action_terms) and not any(term in content for term in reason_terms):
            gaps.append({"id": f"GAP-{len(gaps)+1:03d}", "fact": content,
                         "missing_reason": "판단 이유가 근거에 기록되지 않음",
                         "question": f"‘{item['title']}’ 결정을 내린 가장 큰 이유는 무엇이었나요?",
                         "status": "미답변"})
            placement.append("06 판단 이유 누락 점검")
        placements[str(item["candidate_id"])] = placement
        sources[f"memory.{memory_id}"] = {"source_document_ids": [item["source_document_id"]] if item["source_document_id"] else [],
                                            "source_memory_ids": [item["candidate_id"]]}
    sections = {
        "00 문서 정보": {
            "문서 ID": primary.get("document_id", INPUT_NEEDED), "프로젝트 ID": project_id,
            "프로젝트명": project_name, "작성자": primary.get("owner") or INPUT_NEEDED,
            "작성일": str(date.today()), "버전": primary.get("version", "v1.0"),
            "처리상태": "초안", "공개범위": primary.get("visibility") or INPUT_NEEDED,
            "최종 확인자": primary.get("approver") or INPUT_NEEDED, "확인일": INPUT_NEEDED,
        },
        "01 프로젝트 종료 기준정보": {
            "프로젝트 기간": project.get("started_at") or INPUT_NEEDED,
            "종료일": IN_PROGRESS if active else (project.get("ended_at") or INPUT_NEEDED),
            "PM / 책임자": _field(combined, "PM", "책임자") or INPUT_NEEDED,
            "최종 승인자": IN_PROGRESS if active else (primary.get("approver") or INPUT_NEEDED),
            "고객 / 요청부서": _field(combined, "고객", "요청부서") or INPUT_NEEDED,
            "협업 부서": _field(combined, "협업 부서") or INPUT_NEEDED,
            "최종 산출물": ", ".join(item["title"] for item in documents) or INPUT_NEEDED,
            "회고 참여자": INPUT_NEEDED,
        },
        "02 근거 문서 수집 및 우선순위": evidence,
        "03 반려·승인·변경 타임라인": [],
        "04 목표 대비 최종 결과": {"목표/KPI": _field(combined, "목표", "KPI") or INPUT_NEEDED,
                                      "목표값": INPUT_NEEDED,
                                      "실제값": IN_PROGRESS if active else INPUT_NEEDED,
                                      "판정": IN_PROGRESS if active else INPUT_NEEDED,
                                      "근거 문서": INPUT_NEEDED},
        "05 시행착오 및 문제 발생 기록": issues,
        "06 판단 이유 누락 점검": gaps,
        "07 회고 답변 및 대화 보충정보": [],
        "08 조직기억 후보 - 임시 저장": memories,
        "09 최종 조직기억 카드": {
            "결과": IN_PROGRESS if active else INPUT_NEEDED,
            "판단 이유": INPUT_NEEDED, "검토한 대안": INPUT_NEEDED,
            "시행착오와 원인": "\n".join(x["issue"] for x in issues) or INPUT_NEEDED,
            "해결방법": "\n".join(x["action"] for x in issues if x["action"] != INPUT_NEEDED) or INPUT_NEEDED,
            "재사용 조건": INPUT_NEEDED, "예외 조건": INPUT_NEEDED,
            "원본 출처": ", ".join(x["name"] for x in evidence) or "선택된 업무 대화",
            "신뢰 상태": "사용자 검토 필요",
        },
        "10 최종 저장 전 수정·삭제 확인": {"처리": "유지", "최종 선택": "초안 저장"},
        "11 다음 프로젝트 적용 액션": [{"개선 과제": x["action"], "담당자": INPUT_NEEDED,
                                           "적용 시점": "기획 단계", "완료 조건": INPUT_NEEDED, "상태": "예정"}
                                          for x in issues if x["action"] != INPUT_NEEDED],
        "12 최종 확인 및 배포": {"PM/책임자": INPUT_NEEDED, "검토자": INPUT_NEEDED,
                                  "최종 승인자": INPUT_NEEDED, "조직기억 관리자": INPUT_NEEDED},
    }
    draft = {"project_id": project_id, "project_name": project_name, "active": active, "sections": sections}
    return draft, sources, placements


def create_draft(con, project_id, candidate_ids):
    draft, sources, placements = build_draft(con, project_id, candidate_ids)
    cur = con.execute(
        """INSERT INTO closeout_drafts
           (project_id,reviewed_document_ids,gap_questions,status,evidence_conflicts,
            selected_candidate_ids,draft_json,field_sources,candidate_placements,template_file_id,template_path,updated_at)
           VALUES(?,?,?,'draft','[]',?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        (project_id,
         json.dumps([item["document_id"] for item in _project_documents(con, project_id)]),
         json.dumps(draft["sections"]["06 판단 이유 누락 점검"], ensure_ascii=False),
         json.dumps(candidate_ids), json.dumps(draft, ensure_ascii=False),
         json.dumps(sources, ensure_ascii=False), json.dumps(placements, ensure_ascii=False),
         TEMPLATE_FILE_ID, str(TEMPLATE_PATH)),
    )
    draft_id = cur.lastrowid
    marks = ",".join("?" for _ in candidate_ids)
    if candidate_ids:
        con.execute(f"UPDATE memory_candidates SET draft_status='included_in_draft',draft_id=? WHERE candidate_id IN ({marks})",
                    (draft_id, *candidate_ids))
    con.commit()
    return draft_id


def load_draft(con, draft_id):
    row = con.execute("SELECT * FROM closeout_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("selected_candidate_ids", "draft_json", "field_sources", "candidate_placements"):
        result[key] = json.loads(result[key] or ("{}" if key != "selected_candidate_ids" else "[]"))
    return result


def save_draft(con, draft_id, draft):
    con.execute("UPDATE closeout_drafts SET draft_json=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (json.dumps(draft, ensure_ascii=False), draft_id))
    con.commit()


def completion(draft):
    values = []
    for section in draft.get("sections", {}).values():
        if isinstance(section, dict):
            values.extend(section.values())
    required = [value for value in values if isinstance(value, str)]
    complete = [value for value in required if value.strip() and value not in {INPUT_NEEDED, IN_PROGRESS}]
    return round(100 * len(complete) / len(required)) if required else 0, len(required) - len(complete)


def _flatten(value, prefix=""):
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            lines.extend(_flatten(item, f"{key}: "))
    elif isinstance(value, list):
        for number, item in enumerate(value, 1):
            lines.append(f"{prefix}{number}")
            lines.extend(_flatten(item, "  "))
    else:
        lines.append(prefix + str(value))
    return lines


def _set_cell(cell, value):
    properties = cell.find(f"{{{W_NS}}}tcPr")
    for child in list(cell):
        if child is not properties:
            cell.remove(child)
    paragraph = ET.SubElement(cell, f"{{{W_NS}}}p")
    run = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    text = ET.SubElement(run, f"{{{W_NS}}}t")
    text.text = str(value)


def _fill_label_table(table, values):
    for row in table.findall(f"{{{W_NS}}}tr"):
        cells = row.findall(f"{{{W_NS}}}tc")
        for index in range(0, len(cells) - 1, 2):
            label = "".join(node.text or "" for node in cells[index].iter(f"{{{W_NS}}}t")).strip()
            if label in values:
                _set_cell(cells[index + 1], values[label])


def _fill_rows(table, items, fields):
    rows = table.findall(f"{{{W_NS}}}tr")[1:]
    for row, item in zip(rows, items):
        cells = row.findall(f"{{{W_NS}}}tc")
        for index, field in enumerate(fields):
            if index < len(cells) and field in item:
                _set_cell(cells[index], item[field])


def render_docx(draft, template_path=TEMPLATE_PATH):
    """Return a new DOCX; the cached Drive template is opened read-only and never modified."""
    template_path = Path(template_path)
    original = template_path.read_bytes()
    source_hash = hashlib.sha256(original).hexdigest()
    incoming = io.BytesIO(original)
    outgoing = io.BytesIO()
    with zipfile.ZipFile(incoming, "r") as src, zipfile.ZipFile(outgoing, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            payload = src.read(info.filename)
            if info.filename == "word/document.xml":
                root = ET.fromstring(payload)
                body = root.find(f"{{{W_NS}}}body")
                tables = list(root.iter(f"{{{W_NS}}}tbl"))
                sections = draft["sections"]
                if len(tables) >= 12:
                    _fill_label_table(tables[0], sections["00 문서 정보"])
                    _fill_label_table(tables[1], sections["01 프로젝트 종료 기준정보"])
                    _fill_rows(tables[2], sections["02 근거 문서 수집 및 우선순위"],
                               ("document_type", "name", "version", "status", "priority"))
                    _fill_rows(tables[5], sections["05 시행착오 및 문제 발생 기록"],
                               ("memory_id", "issue", "impact", "action", "result", "limitation", "source"))
                    _fill_rows(tables[6], sections["06 판단 이유 누락 점검"],
                               ("id", "fact", "missing_reason", "question", "status"))
                    _fill_rows(tables[8], sections["08 조직기억 후보 - 임시 저장"],
                               ("id", "content", "source", "reuse_condition", "status", "selection"))
                    _fill_label_table(tables[9], sections["09 최종 조직기억 카드"])
                    _fill_rows(tables[11], sections["11 다음 프로젝트 적용 액션"],
                               ("id", "개선 과제", "담당자", "적용 시점", "완료 조건", "상태"))
                sect = body.find(f"{{{W_NS}}}sectPr")
                appendix = ET.Element(f"{{{W_NS}}}p")
                appendix_run = ET.SubElement(appendix, f"{{{W_NS}}}r")
                appendix_text = ET.SubElement(appendix_run, f"{{{W_NS}}}t")
                appendix_text.text = "자동 작성 근거·추적 부록"
                body.insert(list(body).index(sect) if sect is not None else len(body), appendix)
                for title, value in draft["sections"].items():
                    for text in [title, *_flatten(value)]:
                        paragraph = ET.Element(f"{{{W_NS}}}p")
                        run = ET.SubElement(paragraph, f"{{{W_NS}}}r")
                        node = ET.SubElement(run, f"{{{W_NS}}}t")
                        node.text = text
                        body.insert(list(body).index(sect) if sect is not None else len(body), paragraph)
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            dst.writestr(info, payload)
    assert hashlib.sha256(template_path.read_bytes()).hexdigest() == source_hash
    return outgoing.getvalue()
