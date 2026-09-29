import csv
import hashlib
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from db import init_db

SOURCE_ROOT = Path("data/source_docs")
MANIFEST = Path("data/drive_manifest.csv")


def extract_text(path):
    path = Path(path)
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        return "\n".join("".join(node.text or "" for node in p.iter(ns + "t")) for p in root.iter(ns + "p"))
    raise ValueError("지원 형식은 TXT, MD, DOCX입니다.")


def _field(text, label):
    match = re.search(rf"(?m)^\s*{re.escape(label)}\s*$\n\s*([^\n]+)", text)
    return match.group(1).strip() if match else ""


def _version(name, text):
    match = re.search(r"v(\d+(?:\.\d+)?)", name, re.I)
    return "v" + match.group(1) if match else (_field(text, "버전") or "v1.0")


def _status(name, text):
    declared = _field(text, "처리상태")
    if declared:
        if "반려" in declared:
            return "rejected"
        if "대체" in declared or "폐기" in declared:
            return "superseded"
        if "초안" in declared:
            return "draft"
        if "승인" in declared:
            return "approved"
    raw = name
    if "반려" in raw:
        return "rejected"
    if "대체" in raw or "폐기" in raw:
        return "superseded"
    if "초안" in raw:
        return "draft"
    return "approved"


def _doc_type(name):
    if "성과" in name or "RPT" in name:
        return "성과보고서"
    if "기획" in name or "PLN" in name or "_PP_" in name:
        return "기획안"
    if "회고" in name:
        return "회고록"
    return "회의록"


def import_all(db_path="data/momentlab.db"):
    con = init_db(db_path)
    links = {}
    if MANIFEST.exists():
        with MANIFEST.open(encoding="utf-8") as handle:
            links = {row["file_name"]: row for row in csv.DictReader(handle)}
    count = 0
    # 로컬 사본 전체를 진실 원본으로 삼아 문서 색인을 원자적으로 다시 만든다.
    con.execute("DELETE FROM document_search_index")
    con.execute("DELETE FROM documents")
    for path in sorted(SOURCE_ROOT.glob("U-*/*")):
        if path.suffix.lower() not in {".txt", ".md", ".docx"}:
            continue
        text = extract_text(path)
        project_id = path.parent.name
        version = _version(path.name, text)
        base_id = _field(text, "문서 ID") or re.sub(r"[^A-Z0-9]+", "-", path.stem.upper()).strip("-")
        document_id = base_id if version.lower() in base_id.lower() else f"{base_id}-{version}"
        if con.execute("SELECT 1 FROM documents WHERE document_id=?", (document_id,)).fetchone():
            document_id += "-" + hashlib.sha1(path.name.encode()).hexdigest()[:6]
        family = re.sub(r"-v?\d+(?:\.\d+)?$", "", document_id, flags=re.I)
        row = links.get(path.name, {})
        status = _status(path.name, text)
        doc_type = _doc_type(path.name)
        con.execute("DELETE FROM document_search_index WHERE document_id=?", (document_id,))
        con.execute("""INSERT OR REPLACE INTO documents
          (document_id,project_id,document_family_id,title,document_type,version,status,owner,reviewer,approver,
           visibility,effective_start,effective_end,supersedes_document_id,source_path,source_name,source_drive_url,content)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (document_id, project_id, family, path.stem, doc_type, version, status,
           _field(text, "작성자"), _field(text, "검토자"), _field(text, "승인자"), "all",
           _field(text, "작성일")[:10] or "2026-01-01", None, None, str(path), path.name,
           row.get("source_drive_url", ""), text))
        con.execute("INSERT INTO document_search_index VALUES(?,?,?)", (document_id, path.stem, text))
        count += 1
    con.commit()
    columns = [row[1] for row in con.execute("PRAGMA table_info(documents)")]
    with Path("data/documents.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(con.execute("SELECT * FROM documents ORDER BY project_id,title"))
    return count


if __name__ == "__main__":
    print(f"{import_all()}개 문서를 등록했습니다.")
