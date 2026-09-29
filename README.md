# MomentLab 조직 기억 MVP

Google Drive 원본의 로컬 캐시를 하이브리드 RAG로 검색하고, 관련 원문만 근거로 한국어 답변을 생성하는 Streamlit MVP입니다.

## 실행

```bash
python3 -m pip install -r requirements.txt
python3 importers.py
streamlit run app.py
```

Ollama는 선택 사항입니다. 없어도 가장 관련도 높은 원문을 답변으로 표시합니다. 기본 답변 모델은 `qwen2.5:3b`입니다.

```bash
ollama serve
ollama pull qwen2.5:3b
```

새 모델 다운로드 전에는 사용자의 확인을 받으세요.

## 문서 연동

- 지원 형식: `.txt`, `.md`, `.docx`
- Drive API와 OAuth를 사용하지 않습니다. 공개 Google Docs/DOCX 링크를 60초마다 백그라운드에서 조건부 확인합니다.
- 최초 한 번만 원문을 받고 이후에는 내용이 바뀐 문서만 로컬 캐시와 색인을 교체합니다.
- 질문은 동기화를 기다리지 않고 현재 로컬 RAG 색인으로 즉시 처리됩니다.
- `data/drive_manifest.csv`에 파일명과 공개 원본 링크를 등록합니다.
- PDF/HWP는 DOCX 또는 TXT로 내보낸 뒤 등록합니다.

## 검색·답변 방식

- 화면에는 질문 입력, 답변, 실제 인용한 Google Drive 근거 문서만 표시합니다.
- 다국어 의미 벡터와 띄어쓰기·오타에 강한 키워드 점수를 결합합니다.
- 검색 확신도가 낮을 때만 Qwen이 맞춤법·오타를 교정하고 동의어·유사어를 확장한 뒤 한 번 더 검색합니다.
- 문서 조각과 벡터는 SQLite에 저장하며, 내용 해시가 바뀐 문서만 다시 색인합니다.
- 최초 색인 뒤에는 질문할 때 Google Drive 문서나 임베딩 모델을 다시 다운로드하지 않습니다.
- 질문에 프로젝트 ID가 있으면 해당 프로젝트 문서만 검색합니다.
- 평소에는 최신 승인 문서를 사용하고, 반려·실패·변경 이유를 묻는 질문은 과거 이력을 자동으로 포함합니다.
- 상위 3개 관련 원문 문맥만 Qwen에 전달하고 모델을 30분간 메모리에 유지합니다.
- AI가 반환한 출처 ID는 실제 검색 결과 집합과 대조합니다.

## 대화 중 조직 기억 후보

- 질문할 때 Google Drive 문서와 승인된 조직 기억을 함께 검색합니다.
- Qwen이 사용자 발화와 검색된 Context의 의미를 비교해 새 사실, 변경, 보완 근거, 충돌, 재사용 가능한 경험만 후보로 제안합니다.
- 사용자는 대화 안에서 `저장`, `내용 수정`, `저장하지 않음`을 선택할 수 있습니다.
- `저장` 또는 `수정 후 저장`을 선택하기 전에는 후보가 데이터베이스에 기록되지 않습니다.
- 저장된 후보의 상태는 `pending_review`이며, 별도 검토로 공식 기억이 되기 전에는 검색과 답변 근거에서 제외됩니다.
- Qwen을 사용할 수 없거나 판단 결과가 불완전하면 후보를 만들지 않고 기존 답변 흐름을 유지합니다.

### 프로젝트 완료와 Google Drive 반영

- 승인된 `성과보고서`가 동기화된 프로젝트를 완료 상태로 판정합니다.
- 진행 중 프로젝트의 후보는 화면 상단 `조직 기억 검토함`에 쌓이며 제목과 내용을 열어 수정하거나 폐기할 수 있습니다.
- 성과보고서가 확인되면 검토함에서 최종 확인 후 회고록과 기억 카드를 해당 프로젝트 Drive 폴더에 작성할 수 있습니다.
- 이미 완료된 프로젝트에서 저장한 기억 카드는 즉시 공식 기억으로 확정하고 Drive 반영을 시도합니다.
- 업로드 오류는 후보와 공식 기억을 삭제하지 않으며 검토함에서 확인하고 재시도할 수 있습니다.

Drive 쓰기에는 Google Drive API와 Google Docs API 범위를 가진 OAuth 인증이 필요합니다. 다음 중 하나를 실행 환경에 설정하세요.

```bash
# 짧은 테스트용
export MOMENTLAB_GOOGLE_ACCESS_TOKEN="..."

# 지속 실행용
export MOMENTLAB_GOOGLE_CLIENT_ID="..."
export MOMENTLAB_GOOGLE_CLIENT_SECRET="..."
export MOMENTLAB_GOOGLE_REFRESH_TOKEN="..."
```

앱은 기존 프로젝트 문서의 부모 폴더를 조회해 같은 폴더에 새 Google 문서를 만듭니다. 자동 탐색이 어려운 경우 프로젝트별 폴더 ID를 지정할 수 있습니다.

```bash
export MOMENTLAB_DRIVE_FOLDER_U_03="Google Drive 폴더 ID"
```

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## MVP 한계

- 링크가 비공개로 바뀌면 직접 동기화할 수 없습니다.
- PDF/HWP 파싱과 배포는 포함하지 않습니다.
