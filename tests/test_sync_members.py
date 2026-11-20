import copy
from datetime import date
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "gitlab-teams-notify"
spec = importlib.util.spec_from_file_location("sync_members", SKILL / "scripts" / "sync_members.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

TODAY = date(2026, 1, 15)


def member(id, username, access_level=30, state="active", expires_at=None):
    return {"id": id, "username": username, "name": username.title(), "access_level": access_level,
            "state": state, "expires_at": expires_at}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.config = sync.build_flow.validate_config(
            json.loads((SKILL / "assets" / "config.example.json").read_text()))
        self.fetched = {
            12345: {"path": "demo/example-project", "members": [
                member(10001, "example-owner", 40), member(10002, "example-member", 15),
                member(10004, "new-person", 30)]},
            12346: {"path": "demo/second-project", "members": [
                member(10001, "example-owner"), member(10002, "example-member"), member(10004, "new-person")]},
        }

    def test_updates_member_ids_without_mutating_input(self):
        before = copy.deepcopy(self.config)
        updated, report, _ = sync.plan(self.config, self.fetched, TODAY)
        self.assertEqual(self.config, before)
        self.assertEqual([p["member_ids"] for p in updated["projects"]], [[10001, 10002], [10001, 10002]])
        self.assertEqual(report[0], {"project": "demo/example-project", "added": [], "removed": [10003],
                                     "unmapped": ["new-person"]})
        self.assertEqual(report[1]["added"], [10002])
        sync.build_flow.validate_config(updated)

    def test_ineligible_members_are_not_notified(self):
        self.fetched[12345]["members"] = [
            member(10001, "example-owner", 10), member(10002, "example-member", state="blocked"),
            member(10003, "example-member-alias", expires_at="2026-01-14"),
            member(10004, "new-person", 5)]
        updated, report, template = sync.plan(self.config, self.fetched, TODAY)
        self.assertEqual(updated["projects"][0]["member_ids"], [])
        self.assertEqual(report[0]["unmapped"], [])
        self.fetched[12345]["members"] = [member(10003, "example-member-alias", expires_at="2026-01-15")]
        self.assertEqual(sync.plan(self.config, self.fetched, TODAY)[0]["projects"][0]["member_ids"], [10003])

    def test_template_lists_unmapped_members_once(self):
        _, _, template = sync.plan(self.config, self.fetched, TODAY)
        self.assertEqual(template, [{"id": 10004, "username": "new-person", "name": "New-Person",
            "projects": ["demo/example-project", "demo/second-project"], "email": ""}])

    def test_moved_project_is_an_error(self):
        self.fetched[12346]["path"] = "demo/renamed-project"
        with self.assertRaises(ValueError):
            sync.plan(self.config, self.fetched, TODAY)


if __name__ == "__main__":
    unittest.main()
