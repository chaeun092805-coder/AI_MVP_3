import json
import re
import urllib.request

from services.ollama_client import BASE_URL, ensure_started
from services.drive_write import create_google_doc


PROJECT_PATTERN = re.compile(r"(?<![A-Z0-9])U-?\d{2}(?!\d)", re.I)
ALLOWED_TYPES = {"new_fact", "changed_fact", "rationale", "conflict", "reusable_experience"}
QUESTION_ENDINGS = ("?", "알려줘", "보여줘", "찾아줘", "누구야", "뭐야", "언제야", "얼마야")
STATEMENT_MARKERS = (
    "했어", "했어요", "한다", "하기로", "정해졌어", "결정됐어", "결정했어",
    "변경됐어", "바뀌었어", "확정됐어", "확정했어", "담당자는", "장소는", "예산은",
)
STATEMENT_ENDING_PATTERN = re.compile(
    r"(?:했어(?:요)?|됐어(?:요)?|바뀌었어(?:요)?|정했어(?:요)?|선정했어(?:요)?|"
    r"결정했어(?:요)?|확정했어(?:요)?|맡았어(?:요)?|체결했어(?:요)?|"
    r"하기로\s*(?:했어(?:요)?|했다|결정했다)|이다|입니다|이야|야|있어(?:요)?|없어(?:요)?)\.?$"
)


def _project_id(text, contexts):
    match = PROJECT_PATTERN.search(text or "")
    if match:
        return match.group(0).upper().replace("U", "U-").replace("--", "-")
    projects = {item.get("project_id", "") for item in contexts if item.get("project_id")}
    return projects.pop() if len(projects) == 1 else ""


def _ask_model(prompt, schema):
    state = ensure_started()
    if not state["running"] or not state["model_ready"]:
        return None


def is_fact_statement(text):
    value = (text or "").strip()
    if not value or value.endswith("?"):
        return False
    if any(value.endswith(ending) for ending in QUESTION_ENDINGS):
        return False
    return any(marker in value for marker in STATEMENT_MARKERS) or bool(STATEMENT_ENDING_PATTERN.search(value))


def assess_statement(text, contexts, project_id=""):
    """Classify a definitive user statement before answer generation; never writes data."""
    if not is_fact_statement(text):
        return {"classification": "QUESTION", "candidate": None}
    packed = "\n\n".join(
        f"[{item['document_id']}] {item['title']}\n{item.get('context', '')}"
        for item in contexts[:5]
    ) or "(확인 가능한 기존 문서 없음)"
    prompt = f"""사용자의 확정적 사실 진술과 기존 승인 문서를 비교하세요.
분류는 반드시 다음 중 하나입니다.
- ALREADY_KNOWN: 같은 사실이 기존 문서에 이미 있음
- NEW_INFORMATION: 기존 문서에서 확인되지 않는 새로운 사실
- UPDATE_CANDIDATE: 같은 항목의 기존 값과 다른 변경 사실

질문이나 추측으로 바꾸지 말고 사용자가 말한 내용만 간결하게 정리하세요.
UPDATE_CANDIDATE이면 existing_fact와 conflict_note에 기존 값과 새 값의 차이를 적으세요.
문서에 없는 내용을 추론하지 마세요.

사용자 진술: {text}
프로젝트: {project_id or '미확정'}

기존 승인 문서:
{packed}"""
    schema = {
        "type": "object",
        "properties": {
            "classification": {"type": "string", "enum": ["ALREADY_KNOWN", "NEW_INFORMATION", "UPDATE_CANDIDATE"]},
            "title": {"type": "string"}, "summary": {"type": "string"},
            "existing_fact": {"type": "string"}, "reason": {"type": "string"},
            "conflict_note": {"type": "string"},
        },
        "required": ["classification", "title", "summary", "existing_fact", "reason", "conflict_note"],
    }
    result = _ask_model(prompt, schema)
    if not result:
        # Safe fallback: a definitive statement remains unverified and is never answered by inference.
        result = {
            "classification": "NEW_INFORMATION",
            "title": f"{project_id or '프로젝트'} 사용자 제공 정보",
            "summary": text.strip(), "existing_fact": "",
            "reason": "현재 연결된 승인 문서에서 동일 사실을 자동 확인하지 못했습니다.",
            "conflict_note": "",
        }
    classification = result.get("classification")
    if classification == "ALREADY_KNOWN":
        return {"classification": classification, "candidate": None, "existing_fact": result.get("existing_fact", "")}
    candidate_type = "changed_fact" if classification == "UPDATE_CANDIDATE" else "new_fact"
    candidate = {
        "project_id": project_id or _project_id(text, contexts),
        "original_text": text,
        "title": str(result.get("title") or f"{project_id or '프로젝트'} 사용자 제공 정보").strip(),
        "summary": str(result.get("summary") or text).strip(),
        "candidate_type": candidate_type,
        "reason": str(result.get("reason", "")).strip(),
        "conflict_note": str(result.get("conflict_note", "")).strip(),
        "existing_fact": str(result.get("existing_fact", "")).strip(),
        "classification": classification,
    }
    return {"classification": classification, "candidate": candidate, "existing_fact": candidate["existing_fact"]}
    payload = {
        "model": state["model"], "prompt": prompt, "stream": False, "format": schema,
        "keep_alive": "30m", "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 450},
    }
    request = urllib.request.Request(
        BASE_URL + "/api/generate", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(json.load(response).get("response", "{}"))
    except Exception:
        return None


def assess_candidate(text, contexts):
    """Compare a user utterance with retrieved approved context; never writes data."""
    if not text.strip():
        return None
    packed = "\n\n".join(
        f"[{item['document_id']}] {item['title']} / 프로젝트={item.get('project_id', '')} / 상태={item['status']}\n{item['context']}"
        for item in contexts
    ) or "(검색된 Context 없음)"
    prompt = f"""당신은 대화에서 재사용할 만한 조직 지식 후보를 찾는 검토자입니다.
사용자 발화와 검색된 승인 문서·승인 기억 Context의 의미를 비교하세요. 단어 유무가 아니라 사실의 내용과 관계를 판단하세요.

후보로 제안하는 경우:
- Context에서 확인되지 않는 새 업무 정보
- 기존 기록 뒤에 달라진 내용
- 기존 기억을 보완하는 구체적 이유·배경
- 기존 기억과 충돌할 수 있는 사실
- 다른 업무에서 다시 쓸 가치가 있는 경험·판단

제안하지 않는 경우:
- 사용자가 정보를 묻기만 하고 새 사실을 주장하지 않음
- Context만으로 충분히 확인되는 말의 반복·요약
- 단순 인사, 감상, 일회성 요청, 추측 또는 불명확한 말
- 특정 표현이 있다는 이유만으로 후보라고 판단한 경우

확인된 정보만 짧고 정확하게 구조화하세요. 충돌·변경이면 기존 Context와 무엇이 다른지 conflict_note에 구체적으로 쓰세요.
근거가 애매하면 is_candidate=false로 답하세요.

사용자 발화:
{text}

검색된 Context:
{packed}"""
    schema = {
        "type": "object",
        "properties": {
            "is_candidate": {"type": "boolean"}, "title": {"type": "string"},
            "summary": {"type": "string"}, "candidate_type": {"type": "string", "enum": sorted(ALLOWED_TYPES)},
            "reason": {"type": "string"}, "conflict_note": {"type": "string"},
        },
        "required": ["is_candidate", "title", "summary", "candidate_type", "reason", "conflict_note"],
    }
    result = _ask_model(prompt, schema)
    if not result or result.get("is_candidate") is not True:
        return None
    title = str(result.get("title", "")).strip()
    summary = str(result.get("summary", "")).strip()
    candidate_type = result.get("candidate_type")
    if not title or not summary or candidate_type not in ALLOWED_TYPES:
        return None
    return {
        "project_id": _project_id(text, contexts), "original_text": text, "title": title, "summary": summary,
        "candidate_type": candidate_type, "reason": str(result.get("reason", "")).strip(),
        "conflict_note": str(result.get("conflict_note", "")).strip(),
    }


def save_candidate(con, project_id, original_text, summary, candidate_type="new_fact", conversation_ids=(), edited_text="", conflict_note="", title="", source_document_id="", source_drive_file_id=""):
    complete = project_is_complete(con, project_id)
    cur = con.execute("""INSERT INTO memory_candidates(project_id,source_conversation_ids,original_text,title,summary,candidate_type,status,user_edited_text,conflict_note,project_complete_at_capture,source_document_id,source_drive_file_id)
      VALUES(?,?,?,?,?,?,'pending_review',?,?,?,?,?)""", (project_id, json.dumps(list(conversation_ids)), original_text, title, summary, candidate_type, edited_text, conflict_note, complete, source_document_id, source_drive_file_id))
    con.commit()
    return cur.lastrowid


def project_is_complete(con, project_id):
    return bool(con.execute(
        "SELECT 1 FROM documents WHERE project_id=? AND document_type='성과보고서' AND status='approved' LIMIT 1",
        (project_id,),
    ).fetchone())


def pending_candidates(con):
    return [dict(row) for row in con.execute(
        "SELECT * FROM memory_candidates WHERE status='pending_review' AND (finalized_at='' OR drive_status='error') ORDER BY project_id,candidate_id"
    )]


def update_candidate(con, candidate_id, title, summary):
    con.execute(
        "UPDATE memory_candidates SET title=?,summary=?,user_edited_text=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? AND finalized_at=''",
        (title.strip(), summary.strip(), summary.strip(), candidate_id),
    )
    con.commit()


def discard_candidate(con, candidate_id):
    con.execute("UPDATE memory_candidates SET status='discarded',updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? AND finalized_at=''", (candidate_id,))
    con.commit()


def _card_text(candidate):
    parts = [candidate["title"], "", candidate["user_edited_text"] or candidate["summary"], "", f"프로젝트: {candidate['project_id']}"]
    if candidate["conflict_note"]:
        parts.extend(["", "기존 기록과의 차이", candidate["conflict_note"]])
    parts.extend(["", "대화 원문", candidate["original_text"]])
    return "\n".join(parts)


def publish_candidate(con, candidate_id):
    candidate = dict(con.execute("SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,)).fetchone())
    if not candidate["finalized_at"]:
        cur = con.execute(
            """INSERT INTO memory_records(project_id,kind,title,content,source_document_ids,source_candidate_ids,status)
               VALUES(?,?,?,?,?,?, 'approved')""",
            (candidate["project_id"], "approved_context", candidate["title"],
             candidate["user_edited_text"] or candidate["summary"], "[]", json.dumps([candidate_id])),
        )
        con.execute("INSERT INTO memory_search_index VALUES(?,?,?,?,?)", (
            cur.lastrowid, candidate["project_id"], "approved_context", candidate["title"],
            candidate["user_edited_text"] or candidate["summary"],
        ))
        con.execute("UPDATE memory_candidates SET finalized_at=CURRENT_TIMESTAMP WHERE candidate_id=?", (candidate_id,))
        con.commit()
    try:
        file_id, _ = create_google_doc(con, candidate["project_id"], f"{candidate['project_id']} 조직 기억 카드 - {candidate['title']}", _card_text(candidate))
        con.execute("UPDATE memory_candidates SET drive_file_id=?,drive_status='uploaded',drive_error='' WHERE candidate_id=?", (file_id, candidate_id))
    except Exception as exc:
        con.execute("UPDATE memory_candidates SET drive_status='error',drive_error=? WHERE candidate_id=?", (str(exc), candidate_id))
    con.commit()
    return dict(con.execute("SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,)).fetchone())


def close_project_memories(con, project_id):
    candidates = [dict(row) for row in con.execute(
        "SELECT * FROM memory_candidates WHERE project_id=? AND status='pending_review' AND finalized_at='' ORDER BY candidate_id",
        (project_id,),
    )]
    results = [publish_candidate(con, item["candidate_id"]) for item in candidates]
    publish_retrospective(con, project_id)
    return results


def publish_retrospective(con, project_id):
    candidates = [dict(row) for row in con.execute(
        "SELECT * FROM memory_candidates WHERE project_id=? AND status='pending_review' ORDER BY candidate_id", (project_id,)
    )]
    retrospective = [f"{project_id} 회고록", ""]
    for item in candidates:
        retrospective.extend([item["title"], item["user_edited_text"] or item["summary"], ""])
    title = f"{project_id} 프로젝트 회고록"
    try:
        file_id, _ = create_google_doc(con, project_id, title, "\n".join(retrospective).strip())
        con.execute(
            "INSERT INTO drive_exports(project_id,export_type,title,drive_file_id,status) VALUES(?, 'retrospective', ?, ?, 'uploaded')",
            (project_id, title, file_id),
        )
    except Exception as exc:
        con.execute(
            "INSERT INTO drive_exports(project_id,export_type,title,status,error) VALUES(?, 'retrospective', ?, 'error', ?)",
            (project_id, title, str(exc)),
        )
    con.commit()


def failed_retrospective_exports(con):
    return [dict(row) for row in con.execute(
        """SELECT e.* FROM drive_exports e
           WHERE e.export_type='retrospective' AND e.status='error'
             AND NOT EXISTS(SELECT 1 FROM drive_exports ok WHERE ok.project_id=e.project_id AND ok.export_type='retrospective' AND ok.status='uploaded')
           ORDER BY e.export_id DESC"""
    )]


def finalize_records(con, project_id, items, document_ids, candidate_ids, approver):
    ids = []
    for item in items:
        if item.get("exclude") or not item.get("content", "").strip():
            continue
        cur = con.execute("""INSERT INTO memory_records(project_id,kind,title,content,source_document_ids,source_candidate_ids,approver,status)
          VALUES(?,?,?,?,?,?,?,'approved')""", (project_id, item["kind"], item["title"], item["content"], json.dumps(document_ids), json.dumps(candidate_ids), approver))
        mid = cur.lastrowid
        con.execute("INSERT INTO memory_search_index VALUES(?,?,?,?,?)", (mid, project_id, item["kind"], item["title"], item["content"]))
        ids.append(mid)
    con.execute("UPDATE projects SET status='closed', ended_at=date('now') WHERE project_id=?", (project_id,))
    con.commit()
    return ids
