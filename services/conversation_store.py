import json

from services.project_context import new_session_context


TITLE_LIMIT = 42


def make_title(question, limit=TITLE_LIMIT):
    """Create a deterministic sidebar title without calling the LLM."""
    title = " ".join((question or "").split())
    if len(title) <= limit:
        return title
    return title[: max(limit - 1, 1)].rstrip() + "…"


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value, fallback):
    try:
        loaded = json.loads(value or "")
        return loaded
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def session_exists(con, session_id):
    if not session_id:
        return False
    return con.execute(
        "SELECT 1 FROM chat_sessions WHERE session_id=?", (session_id,)
    ).fetchone() is not None


def create_session(con, session_id, first_question, context):
    """Persist a session only when its first user message is submitted."""
    con.execute(
        """INSERT OR IGNORE INTO chat_sessions(session_id,title,context_json)
           VALUES(?,?,?)""",
        (session_id, make_title(first_question), _json(context or new_session_context())),
    )
    con.commit()


def save_exchange(con, session_id, position, exchange, context):
    con.execute(
        """INSERT INTO chat_exchanges(session_id,position,exchange_json)
           VALUES(?,?,?)
           ON CONFLICT(session_id,position) DO UPDATE SET
             exchange_json=excluded.exchange_json,
             updated_at=CURRENT_TIMESTAMP""",
        (session_id, position, _json(exchange)),
    )
    con.execute(
        """UPDATE chat_sessions SET context_json=?, updated_at=CURRENT_TIMESTAMP
           WHERE session_id=?""",
        (_json(context or new_session_context()), session_id),
    )
    con.commit()


def list_sessions(con):
    return [dict(row) for row in con.execute(
        """SELECT session_id,title,created_at,updated_at
           FROM chat_sessions
           ORDER BY updated_at DESC, created_at DESC, session_id DESC"""
    )]


def load_session(con, session_id):
    row = con.execute(
        "SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if not row:
        return None
    history = [
        _loads(item["exchange_json"], {})
        for item in con.execute(
            """SELECT exchange_json FROM chat_exchanges
               WHERE session_id=? ORDER BY position, exchange_id""",
            (session_id,),
        )
    ]
    history = [item for item in history if item]
    return {
        "session_id": row["session_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "history": history,
        "context": _loads(row["context_json"], new_session_context()),
    }


def backfill_legacy_sessions(con):
    """Expose older logged chats without inventing context or rerunning search."""
    session_ids = [row[0] for row in con.execute(
        """SELECT DISTINCT session_id FROM conversations
           WHERE session_id<>'' AND session_id NOT IN (SELECT session_id FROM chat_sessions)"""
    )]
    for session_id in session_ids:
        rows = list(con.execute(
            """SELECT conversation_id,speaker,message,created_at FROM conversations
               WHERE session_id=? ORDER BY conversation_id""",
            (session_id,),
        ))
        first_user = next((row for row in rows if row["speaker"] == "user"), None)
        if not first_user:
            continue
        con.execute(
            """INSERT OR IGNORE INTO chat_sessions
               (session_id,title,context_json,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (session_id, make_title(first_user["message"]), _json(new_session_context()),
             rows[0]["created_at"], rows[-1]["created_at"]),
        )
        position = 0
        pending = None
        for row in rows:
            if row["speaker"] == "user":
                pending = row
            elif pending:
                exchange = {
                    "question": pending["message"], "terms": [], "documents": [],
                    "answer": {"answer": row["message"], "citations": [], "caveats": []},
                    "candidate": None,
                    "conversation_ids": [pending["conversation_id"], row["conversation_id"]],
                }
                con.execute(
                    """INSERT OR IGNORE INTO chat_exchanges
                       (session_id,position,exchange_json,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (session_id, position, _json(exchange), row["created_at"], row["created_at"]),
                )
                position += 1
                pending = None
    con.commit()
