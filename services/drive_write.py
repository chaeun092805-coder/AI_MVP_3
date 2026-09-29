import json
import os
import re
import urllib.parse
import urllib.request


DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"


def _token():
    token = os.getenv("MOMENTLAB_GOOGLE_ACCESS_TOKEN", "").strip()
    if token:
        return token
    refresh = os.getenv("MOMENTLAB_GOOGLE_REFRESH_TOKEN", "").strip()
    client_id = os.getenv("MOMENTLAB_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("MOMENTLAB_GOOGLE_CLIENT_SECRET", "").strip()
    if not all((refresh, client_id, client_secret)):
        raise RuntimeError("Google Drive 쓰기 인증이 설정되지 않았습니다.")
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as response:
        return json.load(response)["access_token"]


def _request(url, token, *, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def _file_id(url):
    match = re.search(r"/(?:d|folders)/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else ""


def project_folder_id(con, project_id, token):
    override = os.getenv(f"MOMENTLAB_DRIVE_FOLDER_{project_id.replace('-', '_')}", "").strip()
    if override:
        return override
    rows = con.execute(
        "SELECT source_drive_url FROM documents WHERE project_id=? AND source_drive_url<>'' ORDER BY status='approved' DESC",
        (project_id,),
    )
    for row in rows:
        source_id = _file_id(row[0])
        if not source_id:
            continue
        result = _request(f"{DRIVE_API}/files/{source_id}?fields=parents", token)
        parents = result.get("parents", [])
        if parents:
            return parents[0]
    raise RuntimeError(f"{project_id} 프로젝트의 Google Drive 폴더를 찾지 못했습니다.")


def create_google_doc(con, project_id, title, content):
    token = _token()
    folder_id = project_folder_id(con, project_id, token)
    created = _request(
        f"{DRIVE_API}/files?fields=id,webViewLink", token, method="POST",
        payload={"name": title, "mimeType": "application/vnd.google-apps.document", "parents": [folder_id]},
    )
    file_id = created["id"]
    try:
        _request(
            f"{DOCS_API}/documents/{file_id}:batchUpdate", token, method="POST",
            payload={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        )
    except Exception:
        try:
            _request(f"{DRIVE_API}/files/{file_id}", token, method="DELETE")
        except Exception:
            pass
        raise
    return file_id, f"https://docs.google.com/document/d/{file_id}/edit"
