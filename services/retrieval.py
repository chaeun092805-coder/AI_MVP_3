import hashlib
import html
import os
import re
import time
from collections import OrderedDict
from copy import deepcopy
from difflib import SequenceMatcher

import numpy as np

from config import SEARCH_CACHE_SIZE, SEARCH_CACHE_TTL, SEARCH_TOP_K


WORD_PATTERN = re.compile(r"[가-힣A-Za-z0-9_-]{2,}")
PROJECT_PATTERN = re.compile(r"(?<![A-Z0-9])U-?\d{2}(?!\d)", re.I)
STOPWORDS = {
    "관련", "대한", "무엇", "뭐야", "알려줘", "어떻게", "경우", "하는", "있는",
    "했어", "해야", "프로젝트", "에서는", "그리고", "하지만", "가장", "제일",
}
HISTORY_WORDS = ("반려", "거절", "과거", "이전", "당시", "변경", "실패", "회고", "왜")
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_embedding_model = None
_search_cache = OrderedDict()
TITLE_REQUEST_WORDS = (
    "보여줄래", "보여줘", "알려줘", "내용 알려줘", "정리해줘", "찾아줘", "검색해줘",
)


def _normalized_question(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def normalize_title(text):
    value = (text or "").lower()
    for word in TITLE_REQUEST_WORDS:
        value = value.replace(word, "")
    value = re.sub(r"\.(docx?|pdf|txt|md|csv)\b", "", value, flags=re.I)
    return re.sub(r"[\s_\-()\[\]{}·.,]+", "", value)


def find_documents_by_title(con, question):
    query = normalize_title(question)
    if len(query) < 5:
        return []
    matches = []
    for row in con.execute("SELECT * FROM documents"):
        document = dict(row)
        title = normalize_title(document["title"])
        exact = bool(title and (title == query or title in query))
        partial = bool(len(title) >= 8 and (query in title or SequenceMatcher(None, query, title).ratio() >= 0.84))
        if exact or partial:
            matches.append((2 if exact else 1, len(title), document))
    matches.sort(key=lambda item: (-item[0], -item[1], item[2]["document_id"]))
    return [item[2] for item in matches]


def _index_version(con):
    documents = con.execute(
        "SELECT count(*), coalesce(max(created_at), '') FROM documents"
    ).fetchone()
    memories = con.execute(
        "SELECT count(*), coalesce(max(created_at), '') FROM memory_records WHERE status='approved'"
    ).fetchone()
    return tuple(documents) + tuple(memories)


def clear_search_cache():
    _search_cache.clear()


def _compact(text):
    return re.sub(r"[^가-힣a-z0-9]", "", (text or "").lower())


def _words(text):
    return [word.lower() for word in WORD_PATTERN.findall(text or "") if word.lower() not in STOPWORDS]


def _passages(text, target=1200, overlap=180):
    paragraphs = [part.strip() for part in re.split(r"\n{2,}|(?<=\.)\s*\n", text or "") if part.strip()]
    passages = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 1 > target:
            passages.append(current)
            current = current[-overlap:] + "\n" + paragraph
        else:
            current = paragraph if not current else current + "\n" + paragraph
    if current:
        passages.append(current)
    return passages or [(text or "")[:target]]


def _score_passage(passage, query_words, phrases):
    compact = _compact(passage)
    document_words = set(_words(passage))
    score = 0.0
    matched = 0
    for word in query_words:
        key = _compact(word)
        if key and key in compact:
            score += 3.0 + min(len(key), 8) / 4
            matched += 1
            continue
        similarity = max((SequenceMatcher(None, key, candidate).ratio() for candidate in document_words), default=0)
        if similarity >= 0.72:
            score += 2.2 * similarity
            matched += 1
    for phrase in phrases:
        key = _compact(phrase)
        if len(key) >= 4 and key in compact:
            score += 5.0
    coverage = matched / max(len(query_words), 1)
    return score + coverage * 4, coverage


def _model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _embedding_model


def _ensure_embedding_index(con, documents):
    embeddings_disabled = os.getenv("MOMENTLAB_DISABLE_EMBEDDINGS") == "1"
    con.execute("""CREATE TABLE IF NOT EXISTS document_chunks(
      chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      text TEXT NOT NULL, embedding BLOB NOT NULL, dimensions INTEGER NOT NULL
    )""")
    model = None
    if not embeddings_disabled:
        try:
            model = _model()
        except Exception as exc:
            print(f"[SEARCH] embedding model unavailable; indexing text chunks only: {exc}")
    try:
        for document in documents:
            digest = hashlib.sha256(document["content"].encode()).hexdigest()
            current = con.execute(
                "SELECT content_hash,dimensions FROM document_chunks WHERE document_id=? LIMIT 1",
                (document["document_id"],),
            ).fetchone()
            if current and current[0] == digest and (not model or current[1] > 0):
                continue
            passages = _passages(document["content"])
            vectors = list(model.embed(passages)) if model else [None] * len(passages)
            con.execute("DELETE FROM document_chunks WHERE document_id=?", (document["document_id"],))
            for index, (passage, vector) in enumerate(zip(passages, vectors)):
                if vector is None:
                    blob, dimensions = b"", 0
                else:
                    array = np.asarray(vector, dtype=np.float32)
                    blob, dimensions = array.tobytes(), len(array)
                con.execute(
                    "INSERT INTO document_chunks VALUES(?,?,?,?,?,?)",
                    (f"{document['document_id']}::{index}", document["document_id"], digest,
                     passage, blob, dimensions),
                )
        con.commit()
        return model is not None
    except Exception as exc:
        print(f"[SEARCH] embedding index unavailable: {exc}")
        return False


def _eligible_documents(con, include_history):
    rows = [dict(row) for row in con.execute("SELECT * FROM documents")]
    if include_history:
        return [row for row in rows if row["status"] in {"approved", "rejected", "superseded"}]
    approved = [row for row in rows if row["status"] == "approved"]
    newest = {}
    for row in approved:
        version = float(re.sub(r"[^0-9.]", "", row["version"]) or 0)
        key = row["document_family_id"]
        if key not in newest or version > newest[key][0]:
            newest[key] = (version, row)
    return [item[1] for item in newest.values()]


def _approved_memories(con):
    return [
        {
            "document_id": f"memory:{row['memory_id']}",
            "project_id": row["project_id"],
            "document_family_id": f"memory:{row['memory_id']}",
            "title": row["title"],
            "document_type": "조직기억",
            "version": "v1.0",
            "status": "approved",
            "source_drive_url": "",
            "content": row["content"],
        }
        for row in con.execute("SELECT * FROM memory_records WHERE status='approved'")
    ]


def _semantic_passages(con, question, sources):
    if not sources:
        return {}
    if not _ensure_embedding_index(con, sources):
        return {}
    try:
        query = np.asarray(next(iter(_model().query_embed(question))), dtype=np.float32)
        allowed_ids = {row["document_id"] for row in sources}
        marks = ",".join("?" * len(allowed_ids))
        rows = con.execute(
            f"SELECT document_id,text,embedding,dimensions FROM document_chunks WHERE document_id IN ({marks})",
            tuple(allowed_ids),
        )
        scores = {}
        for row in rows:
            if row["dimensions"] <= 0:
                continue
            vector = np.frombuffer(row["embedding"], dtype=np.float32, count=row["dimensions"])
            score = float(np.dot(query, vector) / ((np.linalg.norm(query) * np.linalg.norm(vector)) or 1))
            if row["document_id"] not in scores or score > scores[row["document_id"]][0]:
                scores[row["document_id"]] = (score, row["text"])
        return scores
    except Exception:
        return {}


def search_documents(
    con, question, limit=None, terms=(), return_metadata=False,
    allowed_document_ids=None, allowed_project_ids=None, force_document_scope=False,
):
    """Hybrid RAG: combine typo-tolerant lexical relevance with local semantic vectors."""
    limit = SEARCH_TOP_K if limit is None else limit
    document_scope = tuple(sorted(allowed_document_ids or ()))
    project_scope = tuple(sorted(allowed_project_ids or ()))
    cache_key = (
        _index_version(con), _normalized_question(question), tuple(terms), limit,
        document_scope, project_scope, force_document_scope,
    )
    cached = _search_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] <= SEARCH_CACHE_TTL:
        _search_cache.move_to_end(cache_key)
        result = deepcopy(cached[1])
        return (result, True) if return_metadata else result
    if cached:
        del _search_cache[cache_key]
    include_history = any(word in question for word in HISTORY_WORDS)
    requested_projects = {
        value.upper().replace("U", "U-").replace("--", "-")
        for value in PROJECT_PATTERN.findall(question)
    }
    documents = _eligible_documents(con, include_history) + _approved_memories(con)
    if document_scope:
        documents = [row for row in documents if row["document_id"] in document_scope]
    if project_scope:
        documents = [row for row in documents if row.get("project_id") in project_scope]
    if requested_projects:
        documents = [row for row in documents if row["project_id"].upper() in requested_projects]

    phrases = list(dict.fromkeys([question, *terms]))
    query_words = list(dict.fromkeys(word for phrase in phrases for word in _words(phrase)))
    lexical = {}
    for row in documents:
        best = (0.0, 0.0, "")
        for passage in _passages(row["content"]):
            score, coverage = _score_passage(passage, query_words, phrases)
            if score > best[0]:
                best = (score, coverage, passage)
        title_score, _ = _score_passage(row["title"], query_words, phrases)
        lexical[row["document_id"]] = (best[0] + title_score * 0.45, best[1], best[2])

    semantic = _semantic_passages(con, question, documents)
    max_lexical = max((item[0] for item in lexical.values()), default=1) or 1
    ranked = []
    for row in documents:
        lexical_score, coverage, lexical_passage = lexical[row["document_id"]]
        semantic_score, semantic_passage = semantic.get(row["document_id"], (0.0, ""))
        normalized_lexical = lexical_score / max_lexical
        normalized_semantic = max(0.0, semantic_score)
        hybrid = normalized_lexical * 0.58 + normalized_semantic * 0.42
        if requested_projects:
            hybrid += 0.08
        if row["document_type"] in {"성과보고서", "회고록"}:
            hybrid += 0.02
        forced = force_document_scope and row["document_id"] in document_scope
        if forced or (hybrid >= 0.34 and (coverage >= 0.12 or semantic_score >= 0.35 or requested_projects)):
            passage = semantic_passage if semantic_score >= 0.48 else lexical_passage
            if not passage:
                passage = _passages(row["content"])[0]
            row.update(
                score=round(max(hybrid, 0.95) if forced else hybrid, 4),
                lexical_score=round(normalized_lexical, 4),
                semantic_score=round(semantic_score, 4),
                context=passage[:1800],
                excerpt=passage[:900],
            )
            ranked.append(row)

    ranked.sort(key=lambda item: (-item["score"], item["document_id"]))
    if not ranked:
        result = []
    else:
        cutoff = max(0.34, ranked[0]["score"] * 0.72)
        result = [item for item in ranked if item["score"] >= cutoff][:limit]
    _search_cache[cache_key] = (time.monotonic(), deepcopy(result))
    _search_cache.move_to_end(cache_key)
    while len(_search_cache) > SEARCH_CACHE_SIZE:
        _search_cache.popitem(last=False)
    return (result, False) if return_metadata else result


def highlight_html(text, terms):
    safe = html.escape(text or "")
    words = sorted(set(word for term in terms for word in _words(term)), key=len, reverse=True)
    if not words:
        return safe
    return re.sub("(" + "|".join(map(re.escape, words)) + ")", r"<mark>\1</mark>", safe, flags=re.I)
