import sqlite3

from db import init_db
from services.conversation_store import (
    backfill_legacy_sessions, create_session, list_sessions, load_session,
    make_title, save_exchange, session_exists,
)
from services.project_context import new_session_context


def database(tmp_path):
    return init_db(tmp_path / "chat.db")


def sample_exchange(question="질문", answer="답변", candidate=None):
    return {
        "question": question, "terms": ["검색어"], "documents": [],
        "answer": {"answer": answer, "citations": [], "caveats": []},
        "candidate": candidate, "conversation_ids": [1, 2],
    }


def test_01_new_empty_chat_is_not_stored(tmp_path):
    con = database(tmp_path)
    assert list_sessions(con) == []


def test_02_first_question_creates_title_without_llm(tmp_path):
    con = database(tmp_path)
    create_session(con, "s1", "  첫 번째   사용자 질문입니다  ", new_session_context())
    assert list_sessions(con)[0]["title"] == "첫 번째 사용자 질문입니다"


def test_03_title_is_truncated(tmp_path):
    assert make_title("가" * 100).endswith("…")
    assert len(make_title("가" * 100)) == 42


def test_04_exchange_and_answer_are_restored(tmp_path):
    con = database(tmp_path)
    create_session(con, "s1", "질문", new_session_context())
    save_exchange(con, "s1", 0, sample_exchange(), new_session_context())
    restored = load_session(con, "s1")
    assert restored["history"][0]["answer"]["answer"] == "답변"


def test_05_project_context_is_restored(tmp_path):
    con = database(tmp_path)
    context = new_session_context()
    context["active_entity"] = {"project_id": "U-06", "name": "행사"}
    create_session(con, "s1", "질문", context)
    save_exchange(con, "s1", 0, sample_exchange(), context)
    assert load_session(con, "s1")["context"]["active_entity"]["project_id"] == "U-06"


def test_06_sessions_keep_context_isolated(tmp_path):
    con = database(tmp_path)
    for sid, pid in (("s1", "U-01"), ("s2", "U-02")):
        context = new_session_context()
        context["active_entity"] = {"project_id": pid}
        create_session(con, sid, sid, context)
        save_exchange(con, sid, 0, sample_exchange(sid), context)
    assert load_session(con, "s1")["context"]["active_entity"]["project_id"] == "U-01"
    assert load_session(con, "s2")["context"]["active_entity"]["project_id"] == "U-02"


def test_07_candidate_status_is_restored(tmp_path):
    con = database(tmp_path)
    create_session(con, "s1", "사실", new_session_context())
    exchange = sample_exchange(candidate={"title": "일정", "summary": "변경"})
    exchange["candidate_action"] = "discarded"
    save_exchange(con, "s1", 0, exchange, new_session_context())
    assert load_session(con, "s1")["history"][0]["candidate_action"] == "discarded"


def test_08_updated_session_sorts_first(tmp_path):
    con = database(tmp_path)
    create_session(con, "s1", "먼저", new_session_context())
    create_session(con, "s2", "나중", new_session_context())
    con.execute("UPDATE chat_sessions SET updated_at='2026-01-01 00:00:00' WHERE session_id='s1'")
    con.execute("UPDATE chat_sessions SET updated_at='2026-01-02 00:00:00' WHERE session_id='s2'")
    con.commit()
    assert [row["session_id"] for row in list_sessions(con)] == ["s2", "s1"]


def test_09_existing_position_is_updated_not_duplicated(tmp_path):
    con = database(tmp_path)
    create_session(con, "s1", "질문", new_session_context())
    save_exchange(con, "s1", 0, sample_exchange(answer="전"), new_session_context())
    save_exchange(con, "s1", 0, sample_exchange(answer="후"), new_session_context())
    assert len(load_session(con, "s1")["history"]) == 1
    assert load_session(con, "s1")["history"][0]["answer"]["answer"] == "후"


def test_10_legacy_conversations_are_backfilled(tmp_path):
    con = database(tmp_path)
    con.execute(
        "INSERT INTO conversations(project_id,role,speaker,message,session_id) VALUES('','member','user','옛 질문','old')"
    )
    con.execute(
        "INSERT INTO conversations(project_id,role,speaker,message,session_id) VALUES('','assistant','assistant','옛 답변','old')"
    )
    con.commit()
    backfill_legacy_sessions(con)
    assert session_exists(con, "old")
    assert load_session(con, "old")["history"][0]["answer"]["answer"] == "옛 답변"
