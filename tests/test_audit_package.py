import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "gitlab-teams-notify" / "scripts" / "audit_package.py"
spec = importlib.util.spec_from_file_location("audit_package", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def test_flags_private_values_without_echoing_them(self):
        private_email = "member@" + "private.invalid"
        token = "glpat-" + "A" * 30
        signed = "https://hook.example.com/invoke?" + "sig=" + "B" * 32
        content = "\n".join([private_email, token, signed])
        findings = audit.scan_content(content)
        self.assertEqual(findings, {"non-example email", "credential-like value", "signed URL"})
        for value in (private_email, token, signed):
            self.assertNotIn(value, str(findings))

    def test_example_domains_and_deny_list_boundaries(self):
        self.assertEqual(audit.scan_content("owner@example.com member@sub.example.org"), set())
        self.assertEqual(audit.scan_content("renamed user", ["name"]), set())
        self.assertEqual(audit.scan_content("Name", ["name"]), {"private deny-list value"})

    def test_private_artifact_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.local.json").write_text("{}")
            (root / "alias.txt").symlink_to(root / "config.local.json")
            count, findings = audit.audit(root)
            self.assertEqual(count, 2)
            self.assertEqual(len(findings), 2)

    def test_staged_scan_checks_index_not_clean_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            path = root / "example.txt"
            path.write_text("member@" + "private.invalid")
            git("add", "example.txt")
            path.write_text("member@example.com")
            self.assertEqual(audit.audit(root)[1], [])
            self.assertEqual(len(audit.audit(root, staged=True)[1]), 1)


if __name__ == "__main__":
    unittest.main()
