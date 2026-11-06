import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "gitlab-teams-notify"
spec = importlib.util.spec_from_file_location("build_flow", SKILL / "scripts" / "build_flow.py")
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.config = flow.validate_config(json.loads((SKILL / "assets" / "config.example.json").read_text()))
        self.event = {"project_id": 12345, "url": "https://gitlab.example.com/demo/example-project/-/issues/1",
            "event_kind": "issue", "action": "open", "actor_id": 10999, "actor_name": "Example actor",
            "item_iid": 1, "item_title": "Example title", "assignee_ids": [10001], "previous_assignees": None}

    def recipients(self, **changes):
        return flow.simulate(self.config, dict(self.event, **changes))["recipients"]

    def reply(self, **changes):
        event = dict(event_kind="note", action="create", system=False, noteable_type="Issue",
                     issue_author_id=10001, issue_assignee_ids=[10001, 10002, 10003])
        event.update(changes)
        return self.recipients(**event)

    def test_new_assignment_and_unassigned(self):
        self.assertEqual(self.recipients(), ["owner@example.com"])
        self.assertEqual(self.recipients(assignee_ids=[]), [])
        self.assertEqual(self.recipients(assignee_ids=[10998]), [])

    def test_update_only_new_ids(self):
        previous = [{"id": 10001, "username": "old-label", "name": "Old label"}]
        self.assertEqual(self.recipients(action="update", previous_assignees=previous), [])
        self.assertEqual(self.recipients(action="update", previous_assignees=previous,
            assignee_ids=[10001, 10002]), ["member@example.com"])
        self.assertEqual(self.recipients(action="update", previous_assignees=None), [])
        self.assertEqual(self.recipients(action="update", previous_assignees=[]), ["owner@example.com"])

    def test_same_username_different_id(self):
        self.assertEqual(self.recipients(action="update", previous_assignees=[
            {"id": 10998, "username": "example-owner"}]), ["owner@example.com"])

    def test_reply_deduplication_and_alias_self_exclusion(self):
        self.assertEqual(self.reply(), ["owner@example.com", "member@example.com"])
        self.assertEqual(self.reply(actor_id=10002), ["owner@example.com"])
        self.assertEqual(self.reply(actor_id=10003), ["owner@example.com"])
        self.assertEqual(self.reply(actor_id=10001), ["member@example.com"])
        self.assertEqual(self.reply(actor_id=10001, issue_assignee_ids=[10001]), [])

    def test_unknown_actor_and_unassigned_reply(self):
        self.assertEqual(self.reply(actor_id=None, issue_assignee_ids=[]), ["owner@example.com"])

    def test_irrelevant_events_are_filtered(self):
        for change in ({"system": True}, {"system": "false"}, {"action": "update"},
                       {"noteable_type": "MergeRequest"}, {"event_kind": "merge_request"},
                       {"project_id": 54321}, {"project_id": "12345"},
                       {"url": "https://gitlab.example.com/demo/example-project-other/-/issues/1"},
                       {"url": "https://other.example.com/demo/example-project/-/issues/1"}):
            with self.subTest(change=change):
                self.assertEqual(self.reply(**change), [])

    def test_work_item_event_variants(self):
        self.assertEqual(self.recipients(event_kind="work_item"), ["owner@example.com"])
        self.assertEqual(self.reply(noteable_type="WorkItem"), ["owner@example.com", "member@example.com"])

    def test_title_is_data_and_reply_uses_issue_iid(self):
        title = 'Quote " slash \\ newline\n@{triggerBody()} <b>literal</b>'
        result = flow.simulate(self.config, dict(self.event, event_kind="note", action="create",
            issue_title=title, issue_iid=5, item_iid=999))
        card = json.loads(json.dumps(result["card"]))
        self.assertEqual(card["body"][1]["inlines"][0]["text"], title)
        self.assertEqual(card["body"][2]["inlines"][0]["text"], "Example project #5")
        self.assertEqual(card["actions"][0]["url"], self.event["url"])

    def test_bad_previous_type_raises(self):
        for previous in ("not an array", {}, [1]):
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                self.recipients(previous_assignees=previous)

    def test_invalid_config_and_case_normalization(self):
        raw = copy.deepcopy(self.config)
        raw["members"][0]["email"] = "OWNER@EXAMPLE.COM"
        self.assertEqual(flow.validate_config(raw)["members"][0]["email"], "owner@example.com")
        for field, value in (("id", True), ("id", "12345"), ("path", "demo/../project"),
                             ("gitlab_url", "http://gitlab.example.com"),
                             ("gitlab_url", "https://name:password@gitlab.example.com")):
            raw = copy.deepcopy(self.config)
            raw["project"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                flow.validate_config(raw)
        raw = copy.deepcopy(self.config)
        raw["members"].append(copy.deepcopy(raw["members"][0]))
        with self.assertRaises(ValueError):
            flow.validate_config(raw)

    def test_action_graph_failure_path(self):
        actions = flow.build_actions(self.config)
        self.assertEqual(actions["Actor"]["inputs"]["from"], actions["Recipients"]["inputs"]["from"])
        self.assertEqual(actions["Recipients"]["inputs"]["where"], flow.recipient_rule(self.config).code())
        self.assertEqual(actions["FailureAlert"]["runAfter"], {"SendNotifications": ["Failed", "TimedOut", "Skipped"]})
        self.assertEqual(actions["MarkFailed"]["runAfter"], {"FailureAlert": ["Succeeded"]})
        self.assertEqual(actions["MarkFailed"]["inputs"]["runStatus"], "Failed")
        self.assertEqual(actions["FailureAlert"]["inputs"]["host"]["operationId"], "PostMessageToSelf")
        for status in ("Succeeded", "Failed", "TimedOut", "Skipped"):
            should_alert = status in actions["FailureAlert"]["runAfter"]["SendNotifications"]
            self.assertEqual(should_alert, status != "Succeeded")

    def test_expression_literal_escaping(self):
        self.assertEqual(flow.literal("it's safe").render(), "'it''s safe'")
        self.assertEqual(flow.literal({"label": "it's safe"}).render(), "json('{\"label\":\"it''s safe\"}')")

    def test_cli_private_outputs_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output"
            cmd = [sys.executable, str(SKILL / "scripts" / "build_flow.py"), "--config",
                   str(SKILL / "assets" / "config.example.json"), "--out", str(target)]
            first = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            for path in target.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                json.loads(path.read_text())
            before = (target / "power-automate-actions.json").read_bytes()
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual((target / "power-automate-actions.json").read_bytes(), before)

    def test_reject_repository_output_and_symlink_escape(self):
        with self.assertRaises(ValueError):
            flow.safe_output(ROOT / "private-output")
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaises(ValueError):
                flow.safe_output(link / "private-output")


if __name__ == "__main__":
    unittest.main()
