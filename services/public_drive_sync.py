import csv
import hashlib
import io
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import certifi

from importers import MANIFEST, SOURCE_ROOT, import_all


STATE_PATH = Path("data/public_sync_state.json")
_started = False
_lock = threading.Lock()
_status = {"running": False, "last_sync": None, "changed": 0, "errors": []}
_ssl_context = ssl.create_default_context(cafile=certifi.where())


def _download_url(url):
    sheets = re.search(r"docs\.google\.com/spreadsheets/d/([^/]+)", url)
    if sheets:
        return f"https://docs.google.com/spreadsheets/d/{sheets.group(1)}/export?format=csv"
    docs = re.search(r"docs\.google\.com/document/d/([^/]+)", url)
    if docs:
        return f"https://docs.google.com/document/d/{docs.group(1)}/export?format=txt"
    drive = re.search(r"drive\.google\.com/file/d/([^/]+)", url)
    if drive:
        return f"https://drive.usercontent.google.com/download?id={drive.group(1)}&export=download&confirm=t"
    raise ValueError("지원하지 않는 공개 Google Drive 링크입니다.")


def _read_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _target(file_name):
    existing = next(SOURCE_ROOT.glob(f"U-*/{file_name}"), None)
    if existing:
        return existing
    project = re.match(r"(U-\d{2})", file_name)
    if "문서관리대장" in file_name:
        return SOURCE_ROOT / "_registry" / file_name
    if not project:
        raise ValueError(f"프로젝트 ID를 알 수 없는 파일명: {file_name}")
    return SOURCE_ROOT / project.group(1) / file_name


def _valid_content(file_name, content):
    if file_name.lower().endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return "word/document.xml" in archive.namelist()
        except zipfile.BadZipFile:
            return False
    try:
        return bool(content.decode("utf-8-sig").strip())
    except UnicodeDecodeError:
        return False


def sync_once(timeout=25):
    """Refresh public files without Drive API; unchanged files are never rewritten."""
    if not _lock.acquire(blocking=False):
        return dict(_status)
    try:
        _status.update(running=True, errors=[])
        state = _read_state()
        changed = 0
        with MANIFEST.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            name = row["file_name"]
            previous = state.get(name, {})
            headers = {"User-Agent": "MomentLab/1.0"}
            if previous.get("etag"):
                headers["If-None-Match"] = previous["etag"]
            if previous.get("last_modified"):
                headers["If-Modified-Since"] = previous["last_modified"]
            request = urllib.request.Request(_download_url(row["source_drive_url"]), headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context) as response:
                    content = response.read()
                    metadata = {
                        "etag": response.headers.get("ETag", ""),
                        "last_modified": response.headers.get("Last-Modified", ""),
                    }
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    continue
                _status["errors"].append(f"{name}: HTTP {exc.code}")
                continue
            except (OSError, urllib.error.URLError) as exc:
                _status["errors"].append(f"{name}: {exc}")
                continue

            digest = hashlib.sha256(content).hexdigest()
            if digest == previous.get("sha256"):
                state[name] = {**previous, **metadata, "sha256": digest}
                continue
            if not _valid_content(name, content):
                _status["errors"].append(f"{name}: 공개 링크 응답이 문서 형식과 다릅니다.")
                continue
            target = _target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".sync")
            temporary.write_bytes(content)
            temporary.replace(target)
            state[name] = {**metadata, "sha256": digest}
            changed += 1

        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if changed:
            import_all()
        _status.update(running=False, last_sync=time.time(), changed=changed)
        return dict(_status)
    finally:
        _status["running"] = False
        _lock.release()


def start_background_sync(interval=60):
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            sync_once()
            time.sleep(interval)

    threading.Thread(target=loop, name="public-drive-sync", daemon=True).start()


def status():
    return dict(_status)
