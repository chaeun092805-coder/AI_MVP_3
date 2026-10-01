import sqlite3
from pathlib import Path

DB_PATH = Path("data/momentlab.db")


def connect(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(path=DB_PATH):
    con = connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS projects(
      project_id TEXT PRIMARY KEY, project_name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','closed')),
      started_at TEXT NOT NULL, ended_at TEXT, description TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS documents(
      document_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_family_id TEXT NOT NULL,
      title TEXT NOT NULL, document_type TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
      owner TEXT NOT NULL DEFAULT '', reviewer TEXT NOT NULL DEFAULT '', approver TEXT NOT NULL DEFAULT '',
      visibility TEXT NOT NULL CHECK(visibility IN ('all','marketing','finance','admin_only')),
      effective_start TEXT, effective_end TEXT, supersedes_document_id TEXT,
      source_path TEXT NOT NULL, source_name TEXT NOT NULL, source_drive_url TEXT NOT NULL,
      content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS document_search_index USING fts5(document_id UNINDEXED, title, content, tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS document_chunks(
      chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      text TEXT NOT NULL, embedding BLOB NOT NULL, dimensions INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS conversations(
      conversation_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, role TEXT NOT NULL,
      speaker TEXT NOT NULL CHECK(speaker IN ('user','assistant')), message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_sessions(
      session_id TEXT PRIMARY KEY, title TEXT NOT NULL,
      context_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_exchanges(
      exchange_id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL, position INTEGER NOT NULL,
      exchange_json TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(session_id, position),
      FOREIGN KEY(session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS memory_candidates(
      candidate_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, source_conversation_ids TEXT NOT NULL DEFAULT '',
      original_text TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL, candidate_type TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      status TEXT NOT NULL CHECK(status IN ('pending_review','discarded')), user_edited_text TEXT NOT NULL DEFAULT '',
      conflict_note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS memory_records(
      memory_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('approved_context','retrospective')), title TEXT NOT NULL, content TEXT NOT NULL,
      source_document_ids TEXT NOT NULL, source_candidate_ids TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
      reviewer TEXT NOT NULL DEFAULT '', approver TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status='approved'),
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_search_index USING fts5(memory_id UNINDEXED, project_id UNINDEXED, kind UNINDEXED, title, content, tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS closeout_drafts(
      draft_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, reviewed_document_ids TEXT NOT NULL,
      gap_questions TEXT NOT NULL, user_answers TEXT NOT NULL DEFAULT '', draft_content TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL CHECK(status IN ('draft','finalized')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      evidence_conflicts TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS drive_exports(
      export_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, export_type TEXT NOT NULL,
      source_id INTEGER, title TEXT NOT NULL, drive_file_id TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL CHECK(status IN ('uploaded','error')), error TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    candidate_columns = {row[1] for row in con.execute("PRAGMA table_info(memory_candidates)")}
    candidate_migrations = {
        "title": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "finalized_at": "TEXT NOT NULL DEFAULT ''",
        "drive_file_id": "TEXT NOT NULL DEFAULT ''",
        "drive_status": "TEXT NOT NULL DEFAULT 'queued'",
        "drive_error": "TEXT NOT NULL DEFAULT ''",
        "project_complete_at_capture": "INTEGER NOT NULL DEFAULT 0",
        "source_document_id": "TEXT NOT NULL DEFAULT ''",
        "source_drive_file_id": "TEXT NOT NULL DEFAULT ''",
        "draft_status": "TEXT NOT NULL DEFAULT 'pending'",
        "draft_id": "INTEGER",
    }
    for name, definition in candidate_migrations.items():
        if name not in candidate_columns:
            con.execute(f"ALTER TABLE memory_candidates ADD COLUMN {name} {definition}")
    draft_columns = {row[1] for row in con.execute("PRAGMA table_info(closeout_drafts)")}
    draft_migrations = {
        "selected_candidate_ids": "TEXT NOT NULL DEFAULT '[]'",
        "draft_json": "TEXT NOT NULL DEFAULT '{}'",
        "field_sources": "TEXT NOT NULL DEFAULT '{}'",
        "candidate_placements": "TEXT NOT NULL DEFAULT '{}'",
        "template_file_id": "TEXT NOT NULL DEFAULT ''",
        "template_path": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in draft_migrations.items():
        if name not in draft_columns:
            con.execute(f"ALTER TABLE closeout_drafts ADD COLUMN {name} {definition}")
    conversation_columns = {row[1] for row in con.execute("PRAGMA table_info(conversations)")}
    if "session_id" not in conversation_columns:
        con.execute("ALTER TABLE conversations ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
    for pid in ("U-02", "U-03", "U-04", "U-05"):
        con.execute("INSERT OR IGNORE INTO projects VALUES(?,?, 'closed', ?, ?, ?)", (pid, pid + " 기존 프로젝트", "2026-01-01", "2026-12-31", "초기 Context"))
    con.commit()
    return con
