import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b"
_START_ATTEMPTED = False


def status():
    try:
        with urllib.request.urlopen(BASE_URL + "/api/tags", timeout=1.5) as response:
            models = [item["name"] for item in json.load(response).get("models", [])]
        preferred = os.getenv("MOMENTLAB_MODEL", DEFAULT_MODEL)
        ready = any(name == preferred or name.startswith(preferred + ":") for name in models)
        return {"running": True, "model_ready": ready, "models": models, "model": preferred}
    except (OSError, ValueError, urllib.error.URLError):
        return {"running": False, "model_ready": False, "models": [], "model": DEFAULT_MODEL}


def ensure_started():
    global _START_ATTEMPTED
    state = status()
    if state["running"] or _START_ATTEMPTED or not shutil.which("ollama"):
        return state
    _START_ATTEMPTED = True
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(20):
            time.sleep(0.25)
            state = status()
            if state["running"]:
                return state
    except OSError:
        pass
    return status()


def expand_query(question):
    """Return cheap domain synonyms; typo and spacing tolerance live in retrieval."""
    terms = [question]
    groups = {
        ("비", "날씨", "우천", "기상", "강수", "폭우", "소나기", "악천후"):
            ("우천", "강수", "기상 대응", "실내 전환", "강풍", "폭우", "소나기", "악천후"),
        ("반려", "거절", "승인 안", "왜 안"): ("반려 사유", "검토 의견", "보완", "승인 조건"),
        ("명단", "개인정보", "정보 요청"): ("개인정보", "전체 명단", "다운로드", "동의 범위", "권한"),
        ("업체", "선정", "공급사"): ("업체 선정", "선정 근거", "계약 조건", "비교"),
    }
    for triggers, additions in groups.items():
        if any(trigger in question for trigger in triggers):
            terms.extend(additions)
    return list(dict.fromkeys(terms))


def predict_search_terms(question, existing=()):
    """Use Qwen only when hybrid retrieval is uncertain."""
    state = ensure_started()
    if not state["running"] or not state["model_ready"]:
        return list(dict.fromkeys([question, *existing]))
    prompt = f"""다음 한국어 질문의 맞춤법과 오타를 교정하고 문서 검색에 유용한 동의어·유사어·상위개념을 예측하세요.
질문의 의미, 프로젝트 ID, 숫자는 바꾸지 마세요. 검색 표현만 4~8개 반환하세요.
질문: {question}"""
    schema = {
        "type": "object",
        "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
        "required": ["terms"],
    }
    payload = {
        "model": state["model"],
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 100},
    }
    request = urllib.request.Request(
        BASE_URL + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(json.load(response).get("response", "{}"))
        predicted = [
            term.strip() for term in result.get("terms", [])
            if isinstance(term, str) and term.strip()
        ]
        return list(dict.fromkeys([question, *existing, *predicted]))[:12]
    except Exception:
        return list(dict.fromkeys([question, *existing]))


def generate(question, evidence):
    if not evidence:
        return {"answer": "연결된 문서에서 질문과 관련된 내용을 찾지 못했습니다.", "citations": [], "caveats": []}
    state = ensure_started()
    if not state["running"] or not state["model_ready"]:
        top = evidence[0]
        return {
            "answer": top["context"],
            "citations": [top["document_id"]],
            "caveats": ["로컬 모델을 사용할 수 없어 가장 관련도 높은 원문을 표시했습니다."],
        }

    packed = "\n\n".join(
        f"[{item['document_id']}] 문서={item['title']} / 상태={item['status']}\n{item['context']}"
        for item in evidence
    )
    prompt = f"""당신은 조직 문서 질의응답 도우미입니다. 한국어로 질문에 바로 답하세요.
제공된 근거만 사용하고, 근거가 부족한 내용은 부족하다고 밝히세요.
질문이 과거의 반려·변경·실패를 묻는다면 rejected 문서를 과거 사실로 사용할 수 있지만 현재 규칙처럼 표현하지 마세요.
결론을 먼저 말하고, 필요한 경우 이유·실제 조치·결과를 구분해 설명하세요.
citations에는 답변 작성에 실제로 사용한 document_id만 넣으세요. 관련성이 약한 문서는 인용하지 마세요.

질문: {question}

근거:
{packed}"""
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["answer", "citations"],
    }
    payload = {
        "model": state["model"],
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 300},
    }
    request = urllib.request.Request(
        BASE_URL + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(json.load(response).get("response", "{}"))
        allowed = {item["document_id"] for item in evidence}
        citations = [item for item in result.get("citations", []) if item in allowed]
        if not citations:
            citations = [evidence[0]["document_id"]]
        answer = result.get("answer", "").strip() or evidence[0]["context"]
        return {"answer": answer, "citations": citations, "caveats": []}
    except Exception:
        top = evidence[0]
        return {
            "answer": top["context"],
            "citations": [top["document_id"]],
            "caveats": ["답변 생성이 지연되어 가장 관련도 높은 원문을 표시했습니다."],
        }
