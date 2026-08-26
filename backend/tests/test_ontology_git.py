import unittest

from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.git_repository import (
    OntologyGitError,
    build_commit_id,
    validate_ref_name,
)


class OntologyGitTests(unittest.TestCase):
    def test_validates_branch_and_tag_names(self):
        self.assertEqual(validate_ref_name("feature/risk-rules", "分支"), "feature/risk-rules")
        self.assertEqual(validate_ref_name("release-1.2.0", "标签"), "release-1.2.0")
        for invalid in ("", "../main", "bad branch", "/main", "main/"):
            with self.subTest(invalid=invalid), self.assertRaises(OntologyGitError):
                validate_ref_name(invalid)

    def test_commit_id_is_content_addressed_and_deterministic(self):
        proposal = build_personal_credit_baseline()
        values = (proposal, ["a" * 64], "更新风险规则", "tester", "2026-08-26T00:00:00+00:00")
        first = build_commit_id(*values)
        self.assertEqual(first, build_commit_id(*values))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, build_commit_id(proposal, values[1], "另一项修改", values[3], values[4]))

    def test_parent_changes_commit_identity(self):
        proposal = build_personal_credit_baseline()
        common = (proposal, "更新本体", "tester", "2026-08-26T00:00:00+00:00")
        root = build_commit_id(common[0], [], common[1], common[2], common[3])
        child = build_commit_id(common[0], ["a" * 64], common[1], common[2], common[3])
        self.assertNotEqual(root, child)

