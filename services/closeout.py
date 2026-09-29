import json


def build_questions(final_text, conversation_text, candidates_text):
    questions = []
    combined = conversation_text + "\n" + candidates_text
    if combined.strip() and final_text.strip() and combined.strip() != final_text.strip():
        questions.append("최종 파일과 진행 중 기록에서 계획·결정·실행 결과의 차이가 보입니다. 변경 이유를 회고에 추가할까요?")
    for topic in ("변경", "예산", "실내", "취소", "지연"):
        if topic in final_text and ("이유" not in final_text[max(0, final_text.find(topic)-80):final_text.find(topic)+160]):
            questions.append(f"최종 파일의 ‘{topic}’ 결과에 판단 이유가 충분하지 않습니다. 고려한 대안과 선택 기준을 기록할까요?")
    return list(dict.fromkeys(questions))[:3]


def create_draft(con, project_id, document_ids, final_text, conversation_text, candidates_text):
    questions = build_questions(final_text, conversation_text, candidates_text)
    conflicts = [] if not conversation_text.strip() or conversation_text.strip() == final_text.strip() else ["최종 파일과 대화 기록의 내용 차이 — 최종 사실은 파일 우선, 변경 이유는 사용자 확인 필요"]
    cur = con.execute("""INSERT INTO closeout_drafts(project_id,reviewed_document_ids,gap_questions,status,evidence_conflicts)
      VALUES(?,?,?,'draft',?)""", (project_id, json.dumps(document_ids), json.dumps(questions, ensure_ascii=False), json.dumps(conflicts, ensure_ascii=False)))
    con.commit()
    return cur.lastrowid, questions, conflicts

