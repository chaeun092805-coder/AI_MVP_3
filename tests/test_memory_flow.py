import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import init_db
from services.memory_flow import (
    assess_candidate, pending_candidates, project_is_complete, publish_candidate,
    save_candidate, update_candidate,
)
from services.retrieval import search_documents


class MemoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = init_db(Path(self.temp.name) / "test.db")
        self.context = [{
            "document_id": "U03-RULE", "project_id": "U-03", "title": "기상 운영 기준",
            "status": "approved", "context": "강수확률 80%이면 당일 새벽 설치로 바꾸고 예비 인력 18%를 운영한다.",
        }]

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    @patch("services.memory_flow._ask_model")
    def test_question_answered_by_context_is_not_suggested(self, model):
        model.return_value = {"is_candidate": False, "title": "", "summary": "", "candidate_type": "new_fact", "reason": "", "conflict_note": ""}
        self.assertIsNone(assess_candidate("U-03은 비가 오면 어떻게 대응했어?", self.context))

    @patch("services.memory_flow._ask_model")
    def test_new_fact_without_trigger_keyword_is_suggested(self, model):
        model.return_value = {
            "is_candidate": True, "title": "U-03 우천 시 실내 전환 기준",
            "summary": "강수확률 60% 이상이면 취소하지 않고 실내로 전환한다. 장비 침수 경험이 근거다.",
            "candidate_type": "changed_fact", "reason": "새 기준과 근거가 Context에 없다.",
            "conflict_note": "기존 80% 새벽 설치 기준과 임계값 및 대응 방식이 다르다.",
        }
        candidate = assess_candidate("U-03은 비 올 확률이 60%를 넘으면 실내로 옮겨. 지난번 장비가 젖었거든.", self.context)
        self.assertEqual("changed_fact", candidate["candidate_type"])
        self.assertIn("80%", candidate["conflict_note"])

    @patch("services.memory_flow._ask_model")
    def test_existing_information_with_change_word_is_not_suggested(self, model):
        model.return_value = {"is_candidate": False, "title": "", "summary": "", "candidate_type": "new_fact", "reason": "기존 내용과 동일", "conflict_note": ""}
        self.assertIsNone(assess_candidate("U-03 설치를 강수확률 80%에 새벽 일정으로 변경했어.", self.context))

    @patch("services.memory_flow._ask_model", return_value=None)
    def test_model_unavailable_is_safe_and_writes_nothing(self, _):
        self.assertIsNone(assess_candidate("U-03의 새로운 현장 운영 방식이야.", self.context))
        count = self.con.execute("SELECT count(*) FROM memory_candidates").fetchone()[0]
        self.assertEqual(0, count)

    def test_save_preserves_original_and_edited_text_as_pending(self):
        save_candidate(
            self.con, "U-03", "원래 말한 내용", "수정한 요약", "rationale", (1, 2),
            edited_text="수정한 요약", conflict_note="기존 기준을 보완함", title="운영 판단 근거",
        )
        row = self.con.execute("SELECT * FROM memory_candidates").fetchone()
        self.assertEqual("원래 말한 내용", row["original_text"])
        self.assertEqual("운영 판단 근거", row["title"])
        self.assertEqual("수정한 요약", row["user_edited_text"])
        self.assertEqual("pending_review", row["status"])

    def test_pending_candidate_is_not_searchable_but_approved_memory_is(self):
        save_candidate(self.con, "U-03", "비밀 새 기준", "강수 55%에 지하 행사장으로 이동", "new_fact")
        self.assertEqual([], search_documents(self.con, "지하 행사장 강수 55%", terms=["지하 행사장"]))
        cur = self.con.execute(
            """INSERT INTO memory_records(project_id,kind,title,content,source_document_ids,source_candidate_ids,status)
               VALUES('U-03','approved_context','승인된 우천 기준','강수 55%에 지하 행사장으로 이동','[]','[]','approved')"""
        )
        self.con.commit()
        rows = search_documents(self.con, "U-03 지하 행사장 강수 55%", terms=["지하 행사장"])
        self.assertEqual(f"memory:{cur.lastrowid}", rows[0]["document_id"])

    def test_project_completes_only_with_approved_final_report(self):
        self.assertFalse(project_is_complete(self.con, "U-03"))
        self.con.execute(
            """INSERT INTO documents(document_id,project_id,document_family_id,title,document_type,version,status,visibility,source_path,source_name,source_drive_url,content)
               VALUES('RPT','U-03','RPT','최종 보고서','성과보고서','v1','approved','all','x','x','https://drive.google.com/file/d/X','완료')"""
        )
        self.con.commit()
        self.assertTrue(project_is_complete(self.con, "U-03"))

    def test_active_project_candidate_stays_editable(self):
        candidate_id = save_candidate(self.con, "U-03", "원문", "초안", title="카드")
        update_candidate(self.con, candidate_id, "수정 제목", "수정 내용")
        item = pending_candidates(self.con)[0]
        self.assertEqual("수정 제목", item["title"])
        self.assertEqual("수정 내용", item["user_edited_text"])
        self.assertEqual("", item["finalized_at"])

    @patch("services.memory_flow.create_google_doc", return_value=("DRIVE_ID", "https://docs.google.com/document/d/DRIVE_ID/edit"))
    def test_completed_project_candidate_becomes_approved_and_uploads(self, _):
        self.con.execute(
            """INSERT INTO documents(document_id,project_id,document_family_id,title,document_type,version,status,visibility,source_path,source_name,source_drive_url,content)
               VALUES('RPT','U-03','RPT','최종 보고서','성과보고서','v1','approved','all','x','x','https://drive.google.com/file/d/X','완료')"""
        )
        self.con.commit()
        candidate_id = save_candidate(self.con, "U-03", "원문", "공식 내용", title="카드")
        result = publish_candidate(self.con, candidate_id)
        self.assertEqual("uploaded", result["drive_status"])
        self.assertTrue(result["finalized_at"])
        self.assertEqual(1, self.con.execute("SELECT count(*) FROM memory_records").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
