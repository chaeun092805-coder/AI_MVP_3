import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from db import init_db
from services.memory_flow import save_candidate
from services.retrospective_draft import (
    IN_PROGRESS, INPUT_NEEDED, TEMPLATE_FILE_ID, TEMPLATE_PATH,
    build_draft, completion, create_draft, grouped_candidates, load_draft, render_docx,
)


class RetrospectiveDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.con = init_db(Path(self.temp.name) / "draft.db")
        self.con.execute(
            "INSERT OR REPLACE INTO projects VALUES('U-06','CREWAVE','active','2026-09-01',NULL,'행사')"
        )
        self.con.execute(
            """INSERT INTO documents
               (document_id,project_id,document_family_id,title,document_type,version,status,owner,reviewer,approver,
                visibility,effective_start,source_path,source_name,source_drive_url,content)
               VALUES('DOC-U06','U-06','DOC-U06','CREWAVE 기획안','기획안','v1.0','approved','','','','all',
                      '2026-09-01','x','x','https://drive.google.com/file/d/DRIVE-U06/view',
                      '프로젝트명\nCREWAVE\n목표\n안정적인 행사 운영')"""
        )
        self.ids = [
            save_candidate(self.con, "U-06", text, text, title=title, source_document_id="DOC-U06", source_drive_file_id="DRIVE-U06")
            for title, text in (
                ("MC 초청", "전문 MC 1명을 초청함"),
                ("회선 분리", "네트워크 저하 위험 때문에 QR 체크인과 송출 회선을 분리함"),
                ("추가 인력", "장애 발생 시 인력 5명을 추가 배치함"),
                ("5G 전환", "별도 5G망으로 전환함"),
            )
        ]

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_01_same_document_is_one_group(self):
        groups = grouped_candidates(self.con)
        self.assertEqual(1, len(groups))
        self.assertEqual(4, len(groups[0]["items"]))

    def test_02_only_checked_candidates_are_in_section_08(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:3])
        self.assertEqual(3, len(draft["sections"]["08 조직기억 후보 - 임시 저장"]))

    def test_03_drive_template_metadata_is_saved(self):
        draft_id = create_draft(self.con, "U-06", self.ids[:1])
        record = load_draft(self.con, draft_id)
        self.assertEqual(TEMPLATE_FILE_ID, record["template_file_id"])
        self.assertEqual(str(TEMPLATE_PATH), record["template_path"])

    def test_04_document_information_is_autofilled(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        info = draft["sections"]["00 문서 정보"]
        self.assertEqual("U-06", info["프로젝트 ID"])
        self.assertEqual("CREWAVE", info["프로젝트명"])

    def test_05_unknown_owner_is_not_invented(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        self.assertEqual(INPUT_NEEDED, draft["sections"]["00 문서 정보"]["작성자"])

    def test_06_active_project_has_no_invented_end_result(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        self.assertEqual(IN_PROGRESS, draft["sections"]["01 프로젝트 종료 기준정보"]["종료일"])
        self.assertEqual(IN_PROGRESS, draft["sections"]["09 최종 조직기억 카드"]["결과"])

    def test_07_problem_and_action_are_structured(self):
        draft, _, _ = build_draft(self.con, "U-06", [self.ids[1]])
        issues = draft["sections"]["05 시행착오 및 문제 발생 기록"]
        self.assertEqual(1, len(issues))
        self.assertIn("회선을 분리", issues[0]["action"])

    def test_08_missing_reason_creates_gap(self):
        draft, _, _ = build_draft(self.con, "U-06", [self.ids[3]])
        self.assertEqual(1, len(draft["sections"]["06 판단 이유 누락 점검"]))

    def test_09_user_input_improves_completion(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        before, _ = completion(draft)
        draft["sections"]["00 문서 정보"]["작성자"] = "홍길동"
        after, _ = completion(draft)
        self.assertGreater(after, before)

    def test_10_docx_copy_keeps_original_unchanged(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        before = hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest()
        result = render_docx(draft)
        after = hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertNotEqual(hashlib.sha256(result).hexdigest(), before)
        with zipfile.ZipFile(Path(TEMPLATE_PATH)) as original, zipfile.ZipFile(__import__("io").BytesIO(result)) as generated:
            self.assertEqual(set(original.namelist()), set(generated.namelist()))

    def test_11_incomplete_draft_can_still_render(self):
        draft, _, _ = build_draft(self.con, "U-06", self.ids[:1])
        self.assertIn(INPUT_NEEDED, str(draft))
        self.assertGreater(len(render_docx(draft)), 0)

    def test_12_candidate_field_placements_are_tracked(self):
        _, _, placements = build_draft(self.con, "U-06", [self.ids[1]])
        self.assertIn("05 시행착오", placements[str(self.ids[1])])
        self.assertIn("09 해결방법", placements[str(self.ids[1])])


if __name__ == "__main__":
    unittest.main()
