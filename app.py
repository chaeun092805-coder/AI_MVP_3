from pathlib import Path

import streamlit as st

from db import init_db
from importers import import_all
from services.ollama_client import expand_query, generate, predict_search_terms
from services.memory_flow import (
    assess_candidate, close_project_memories, discard_candidate, pending_candidates,
    failed_retrospective_exports, project_is_complete, publish_candidate, publish_retrospective,
    save_candidate, update_candidate,
)
from services.public_drive_sync import start_background_sync
from services.retrieval import highlight_html, search_documents


st.set_page_config(page_title="MomentLab 조직 기억", page_icon="🧠", layout="centered")
con = init_db()
if not con.execute("SELECT 1 FROM documents LIMIT 1").fetchone() and Path("data/drive_manifest.csv").exists():
    import_all()
    con = init_db()
start_background_sync()

st.title("MomentLab 조직 기억")
st.caption("연결된 Google Drive 문서를 바탕으로 답합니다.")

if "history" not in st.session_state:
    st.session_state.history = []


def save_from_chat(exchange, candidate, summary, edited=False):
    candidate_id = save_candidate(
        con, candidate["project_id"], candidate["original_text"], summary,
        candidate["candidate_type"], exchange.get("conversation_ids", ()),
        edited_text=summary if edited else "", conflict_note=candidate.get("conflict_note", ""),
        title=candidate["title"],
    )
    result = publish_candidate(con, candidate_id) if project_is_complete(con, candidate["project_id"]) else None
    exchange["candidate_action"] = "saved"
    exchange["drive_result"] = result


def render_memory_inbox():
    items = pending_candidates(con)
    failed_exports = failed_retrospective_exports(con)
    with st.expander(f"조직 기억 검토함 · {len(items) + len(failed_exports)}건", expanded=False):
        if not items and not failed_exports:
            st.caption("검토하거나 Drive에 반영할 기억이 없습니다.")
            return
        projects = {}
        for item in items:
            projects.setdefault(item["project_id"], []).append(item)
        for project_id, project_items in projects.items():
            complete = project_is_complete(con, project_id)
            st.markdown(f"#### {project_id} · {'최종 보고서 확인됨' if complete else '진행 중'}")
            for item in project_items:
                st.markdown(f"**{item['title']}**")
                title = st.text_input("제목", item["title"], key=f"inbox-title-{item['candidate_id']}")
                summary = st.text_area("내용", item["user_edited_text"] or item["summary"], key=f"inbox-summary-{item['candidate_id']}")
                if item["conflict_note"]:
                    st.warning("기존 기록과의 차이: " + item["conflict_note"])
                if item["drive_status"] == "error":
                    st.error("Drive 반영 실패: " + item["drive_error"])
                save_col, discard_col = st.columns(2)
                if save_col.button("수정 저장", key=f"inbox-save-{item['candidate_id']}"):
                    update_candidate(con, item["candidate_id"], title, summary)
                    st.rerun()
                if discard_col.button("저장하지 않음", key=f"inbox-discard-{item['candidate_id']}", disabled=bool(item["finalized_at"])):
                    discard_candidate(con, item["candidate_id"])
                    st.rerun()
            if complete:
                captured_while_active = any(not item["project_complete_at_capture"] for item in project_items)
                label = "최종 확인 후 회고록·기억 카드 Drive 반영" if captured_while_active else "Drive 반영 재시도"
                if st.button(label, key=f"publish-project-{project_id}", type="primary"):
                    if captured_while_active:
                        close_project_memories(con, project_id)
                    else:
                        for item in project_items:
                            publish_candidate(con, item["candidate_id"])
                    st.rerun()
            else:
                st.caption("최종 성과보고서가 승인 문서로 동기화되면 최종 확인과 Drive 반영이 활성화됩니다.")
        for export in failed_exports:
            st.markdown(f"#### {export['project_id']} · 회고록 Drive 반영 실패")
            st.error(export["error"])
            if st.button("회고록 Drive 반영 재시도", key=f"retry-export-{export['export_id']}"):
                publish_retrospective(con, export["project_id"])
                st.rerun()


render_memory_inbox()


def render_candidate(exchange, index):
    candidate = exchange.get("candidate")
    if not candidate or exchange.get("candidate_action"):
        if exchange.get("candidate_action") == "saved":
            drive_result = exchange.get("drive_result")
            if drive_result and drive_result["drive_status"] == "uploaded":
                st.caption("완료 프로젝트의 공식 기억으로 확정하고 Google Drive에 반영했습니다.")
            elif drive_result:
                st.caption("공식 기억으로 확정했지만 Drive 반영에 실패했습니다. 조직 기억 검토함에서 오류를 확인하고 재시도할 수 있습니다.")
            else:
                st.caption("진행 중 프로젝트의 검토 대기 기억으로 저장했습니다. 최종 보고서 확인 전에는 답변 근거로 사용하지 않습니다.")
        elif exchange.get("candidate_action") == "discarded":
            st.caption("이 기억 후보는 저장하지 않았습니다.")
        return
    st.info(candidate["reason"] or "기존 Context에서 확인되지 않는 재사용 가능한 업무 정보가 포함되어 있습니다.")
    st.markdown(f"**{candidate['title']}**")
    st.write(candidate["summary"])
    if candidate.get("conflict_note"):
        st.warning("기존 기록과의 차이: " + candidate["conflict_note"])
    if exchange.get("editing_candidate"):
        edited = st.text_area("기억 후보 내용", candidate["summary"], key=f"candidate-edit-{index}")
        save_col, cancel_col = st.columns(2)
        if save_col.button("수정 후 저장", key=f"candidate-edit-save-{index}", type="primary"):
            save_from_chat(exchange, candidate, edited.strip(), edited=True)
            st.rerun()
        if cancel_col.button("수정 취소", key=f"candidate-edit-cancel-{index}"):
            exchange["editing_candidate"] = False
            st.rerun()
        return
    save_col, edit_col, discard_col = st.columns(3)
    if save_col.button("저장", key=f"candidate-save-{index}", type="primary"):
        save_from_chat(exchange, candidate, candidate["summary"])
        st.rerun()
    if edit_col.button("내용 수정", key=f"candidate-edit-{index}"):
        exchange["editing_candidate"] = True
        st.rerun()
    if discard_col.button("저장하지 않음", key=f"candidate-discard-{index}"):
        exchange["candidate_action"] = "discarded"
        st.rerun()


def render_exchange(exchange, index):
    with st.chat_message("user"):
        st.write(exchange["question"])
    with st.chat_message("assistant"):
        st.write(exchange["answer"]["answer"])
        for caveat in exchange["answer"].get("caveats", []):
            st.caption(caveat)
        cited = set(exchange["answer"].get("citations", []))
        sources = [item for item in exchange["documents"] if item["document_id"] in cited]
        if sources:
            st.markdown("##### 근거 문서")
            for item in sources:
                with st.expander(item["title"]):
                    st.markdown(highlight_html(item["excerpt"], exchange["terms"]), unsafe_allow_html=True)
                    if item["source_drive_url"]:
                        st.link_button("Google Drive에서 원문 보기", item["source_drive_url"])
        render_candidate(exchange, index)


for index, exchange in enumerate(st.session_state.history):
    render_exchange(exchange, index)

question = st.chat_input("궁금한 내용을 질문하세요")
if question and question.strip():
    question = question.strip()
    with st.spinner("관련 문서를 확인하고 있습니다…"):
        terms = expand_query(question)
        documents = search_documents(con, question, terms=terms)
        if not documents or documents[0]["score"] < 0.50:
            terms = predict_search_terms(question, terms)
            documents = search_documents(con, question, terms=terms)
        answer = generate(question, documents)
        project_id = next((item.get("project_id") for item in documents if item.get("project_id")), "")
        user_message = con.execute(
            "INSERT INTO conversations(project_id,role,speaker,message) VALUES(?,?,'user',?)",
            (project_id, "member", question),
        ).lastrowid
        assistant_message = con.execute(
            "INSERT INTO conversations(project_id,role,speaker,message) VALUES(?,?,'assistant',?)",
            (project_id, "assistant", answer["answer"]),
        ).lastrowid
        con.commit()
        candidate = assess_candidate(question, documents)
        if candidate and not candidate["project_id"]:
            candidate = None
    st.session_state.history.append({
        "question": question, "terms": terms, "documents": documents, "answer": answer,
        "candidate": candidate, "conversation_ids": (user_message, assistant_message),
    })
    st.rerun()
