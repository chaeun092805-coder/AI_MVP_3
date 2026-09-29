import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import init_db
from services.ollama_client import expand_query, generate, predict_search_terms
from services.public_drive_sync import _download_url
from services.retrieval import search_documents


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = init_db(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def add_doc(self, doc_id, project_id, content, *, status="approved", version="v1.0", family=None, title=None):
        family = family or doc_id
        title = title or doc_id
        self.con.execute(
            """INSERT INTO documents(
              document_id,project_id,document_family_id,title,document_type,version,status,visibility,
              effective_start,source_path,source_name,source_drive_url,content
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, project_id, family, title, "회의록", version, status, "all",
             "2026-01-01", "x.txt", "x.txt", "https://drive.google.com/x", content),
        )
        self.con.commit()

    def test_project_id_with_korean_particle_limits_scope(self):
        self.add_doc("U02", "U-02", "설치 업체는 차량 두 대와 현장 관리자 기준으로 선정했다")
        self.add_doc("U03", "U-03", "설치 업체와 현장 운영 내용을 검토했다")
        rows = search_documents(self.con, "U-02에서 설치 업체를 어떻게 선정했어?", terms=["업체 선정"])
        self.assertEqual({"U02"}, {row["document_id"] for row in rows})

    def test_spacing_difference_still_matches(self):
        self.add_doc("PRIVACY", "U-04", "개인정보 전체 명단은 제공하지 않고 동의 범위에 따라 열람한다")
        rows = search_documents(self.con, "개인 정보 명단 요청 대응", terms=expand_query("개인 정보 명단 요청 대응"))
        self.assertEqual("PRIVACY", rows[0]["document_id"])

    def test_small_typo_still_matches(self):
        self.add_doc("PRIVACY", "U-04", "개인정보 명단 요청은 권한표를 근거로 거절한다")
        rows = search_documents(self.con, "개인정도 명단 요청 대응", terms=expand_query("개인정도 명단 요청 대응"))
        self.assertEqual("PRIVACY", rows[0]["document_id"])

    def test_history_intent_includes_rejected_automatically(self):
        self.add_doc("OLD", "U-03", "예산 근거와 우천 변경비가 없어 기획안이 반려됐다", status="rejected")
        self.add_doc("NEW", "U-03", "예산과 우천 변경비를 보완해 최종 승인했다", status="approved")
        rows = search_documents(self.con, "U-03 기획안이 왜 반려됐어?", terms=expand_query("U-03 기획안이 왜 반려됐어?"))
        self.assertIn("OLD", {row["document_id"] for row in rows})

    def test_weakly_related_document_is_not_returned(self):
        self.add_doc("RAIN", "U-03", "강수 확률과 풍속 기준에 따라 설치를 당일 새벽으로 변경했다")
        self.add_doc("OTHER", "U-04", "프로젝트 운영 내용을 검토했다")
        rows = search_documents(self.con, "비가 오면 설치를 어떻게 변경했어?", terms=expand_query("비가 오면 설치를 어떻게 변경했어?"))
        self.assertEqual({"RAIN"}, {row["document_id"] for row in rows})

    def test_single_character_rain_query_expands_to_weather_terms(self):
        self.add_doc("RAIN", "U-03", "우천과 강수 위험이 있으면 실내 전환 기준을 적용한다")
        terms = expand_query("비")
        rows = search_documents(self.con, "비", terms=terms)
        self.assertIn("우천", terms)
        self.assertEqual("RAIN", rows[0]["document_id"])

    @patch("services.ollama_client.ensure_started", return_value={"running": False, "model_ready": False, "models": [], "model": "qwen2.5:3b"})
    def test_llm_search_prediction_falls_back_without_breaking_terms(self, _):
        self.assertEqual(
            ["개인정도", "개인정보"],
            predict_search_terms("개인정도", ["개인정보"]),
        )

    @patch("services.ollama_client.ensure_started", return_value={"running": False, "model_ready": False, "models": [], "model": "qwen2.5:3b"})
    def test_model_failure_falls_back_to_best_source(self, _):
        evidence = [{"document_id": "DOC", "context": "검증된 원문", "title": "문서", "status": "approved"}]
        result = generate("질문", evidence)
        self.assertEqual("검증된 원문", result["answer"])
        self.assertEqual(["DOC"], result["citations"])

    def test_public_drive_links_need_no_api(self):
        docs = _download_url("https://docs.google.com/document/d/DOC_ID/edit")
        drive = _download_url("https://drive.google.com/file/d/FILE_ID")
        self.assertEqual("https://docs.google.com/document/d/DOC_ID/export?format=txt", docs)
        self.assertIn("id=FILE_ID", drive)


if __name__ == "__main__":
    unittest.main()
