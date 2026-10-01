import tempfile
import unittest
from pathlib import Path

from db import init_db
from services.project_context import (
    active_project_answer, explicit_project, is_active_project_list_question,
    is_current_project_question, is_followup_question, load_active_projects,
    new_session_context, project_ids, select_entity_from_results,
)
from services.retrieval import clear_search_cache, search_documents


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = init_db(Path(self.temp.name) / "test.db")
        clear_search_cache()
        self._doc(
            "REG", "REG", "문서관리대장", "문서관리대장",
            "프로젝트명|프로젝트 ID|진행상황|문서 ID\n"
            "A 프로젝트|U-01|진행중|A-DOC\n"
            "B 프로젝트|U-02|완료|B-DOC\n"
            "C 프로젝트|U-03|진행중|C-DOC",
        )
        self._doc("A-DOC", "U-01", "A 프로젝트 기획안", "기획안", "담당자 김하나 일정 11월 1일 예산 100만원")
        self._doc("B-DOC", "U-02", "B 프로젝트 완료 보고", "성과보고서", "담당자 박완료 일정 10월 1일 예산 50만원")
        self._doc("C-DOC", "U-03", "C 프로젝트 기획안", "기획안", "담당자 이다음 일정 12월 1일 예산 200만원")

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _doc(self, doc_id, project_id, title, doc_type, content):
        self.con.execute(
            """INSERT INTO documents(
              document_id,project_id,document_family_id,title,document_type,version,status,visibility,
              effective_start,source_path,source_name,source_drive_url,content
            ) VALUES(?,?,?,?,?,'v1','approved','all','2026-01-01','x','x','',?)""",
            (doc_id, project_id, doc_id, title, doc_type, content),
        )
        self.con.commit()

    def test_01_active_project_list_excludes_completed(self):
        projects = load_active_projects(self.con)
        self.assertEqual({"A 프로젝트", "C 프로젝트"}, {item["name"] for item in projects})
        self.assertNotIn("B 프로젝트", active_project_answer(projects))

    def test_02_short_followup_uses_scope_ids(self):
        scope = load_active_projects(self.con)
        rows = search_documents(self.con, "담당자는?", allowed_project_ids=project_ids(scope))
        self.assertTrue(rows)
        self.assertLessEqual({row["project_id"] for row in rows}, {"U-01", "U-03"})

    def test_03_geujung_is_followup(self):
        self.assertTrue(is_followup_question("그중 일정 잡힌 거 있어?"))

    def test_04_explicit_project_becomes_entity(self):
        entity = explicit_project(load_active_projects(self.con), "A 프로젝트 자세히 알려줘")
        self.assertEqual("U-01", entity["project_id"])
        rows = search_documents(self.con, "그거 예산은?", allowed_project_ids=[entity["project_id"]])
        self.assertEqual({"U-01"}, {row["project_id"] for row in rows})

    def test_05_current_project_question_detected_without_list(self):
        self.assertTrue(is_current_project_question("이번 행사 담당자가 누구야?"))

    def test_06_active_scope_excludes_past_project(self):
        rows = search_documents(
            self.con, "프로젝트 일정", allowed_project_ids=project_ids(load_active_projects(self.con))
        )
        self.assertNotIn("U-02", {row["project_id"] for row in rows})

    def test_07_clear_result_selects_active_entity(self):
        projects = load_active_projects(self.con)
        results = [{"project_id": "U-01", "score": 0.88}, {"project_id": "U-03", "score": 0.52}]
        entity, score, ambiguous = select_entity_from_results(projects, results)
        self.assertEqual("U-01", entity["project_id"])
        self.assertFalse(ambiguous)

    def test_08_close_scores_do_not_select_arbitrarily(self):
        projects = load_active_projects(self.con)
        results = [{"project_id": "U-01", "score": 0.68}, {"project_id": "U-03", "score": 0.65}]
        entity, _, ambiguous = select_entity_from_results(projects, results)
        self.assertIsNone(entity)
        self.assertTrue(ambiguous)

    def test_09_selected_entity_can_be_reused(self):
        context = new_session_context()
        context["active_entity"] = explicit_project(load_active_projects(self.con), "C 프로젝트")
        rows = search_documents(self.con, "장소는?", allowed_project_ids=[context["active_entity"]["project_id"]])
        self.assertLessEqual({row["project_id"] for row in rows}, {"U-03"})

    def test_10_new_session_clears_only_conversation_context(self):
        context = new_session_context()
        self.assertIsNone(context["active_project_scope"])
        self.assertIsNone(context["active_entity"])
        self.assertEqual([], context["recent_document_ids"])

    def test_11_new_session_reloads_changed_registry_status(self):
        content = self.con.execute("SELECT content FROM documents WHERE document_id='REG'").fetchone()[0]
        self.con.execute(
            "UPDATE documents SET content=? WHERE document_id='REG'",
            (content.replace("A 프로젝트|U-01|진행중", "A 프로젝트|U-01|완료"),),
        )
        self.con.commit()
        self.assertNotIn("A 프로젝트", {item["name"] for item in load_active_projects(self.con)})

    def test_list_intent_is_rule_based(self):
        self.assertTrue(is_active_project_list_question("새 프로젝트 뭐 있어?"))


if __name__ == "__main__":
    unittest.main()
