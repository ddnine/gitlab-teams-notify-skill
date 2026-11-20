import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil
import xml.etree.ElementTree as ET


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

    def merge_request(self, reviewer_ids, action="open", changes=None):
        return {
            "object_kind": "merge_request",
            "project": {"id": 12345},
            "user": {"id": 10001, "name": "Example actor"},
            "object_attributes": {
                "action": action,
                "iid": 7,
                "title": 'Review "example"',
                "url": "https://gitlab.example.com/demo/example-project/-/merge_requests/7",
                "reviewer_ids": reviewer_ids,
            },
            "changes": changes or {},
        }

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

    def test_mentioned_non_assignee_receives_new_reply(self):
        self.assertEqual(self.reply(issue_assignee_ids=[], note_body="Please review @example-member."),
                         ["owner@example.com", "member@example.com"])
        self.assertEqual(self.reply(issue_assignee_ids=[], issue_author_id=10998,
                                   note_body="@example-member @EXAMPLE-MEMBER-ALIAS"), ["member@example.com"])
        self.assertEqual(self.reply(issue_assignee_ids=[], issue_author_id=10998,
                                   actor_id=10003, note_body="@example-member"), [])

    def test_mentions_do_not_expand_event_scope(self):
        for changes in ({"action": "update"}, {"system": True}, {"noteable_type": "MergeRequest"}):
            self.assertEqual(self.reply(issue_assignee_ids=[], issue_author_id=10998,
                                       note_body="@example-member", **changes), [])
        self.assertEqual(self.recipients(assignee_ids=[], note_body="@example-member"), [])

    def test_native_gitlab_payload_matches_legacy(self):
        events = [self.event, dict(self.event, action="update", previous_assignees=[{"id":10002}]),
            dict(self.event, event_kind="note", action="create", issue_author_id=10998,
                 issue_assignee_ids=[], issue_title="Native reply", issue_iid=7,
                 system=False, noteable_type="Issue", note_body="长回复 " * 2000 + " @example-member")]
        for event in events:
            native = {}
            for key, value in event.items():
                parts = flow.NATIVE_EVENT_PATHS[key]
                current = native
                for part in parts[:-1]:
                    current = current.setdefault(part, {})
                current[parts[-1]] = value
            with self.subTest(kind=event["event_kind"]):
                self.assertEqual(flow.simulate(self.config, native), flow.simulate(self.config, event))
        self.assertEqual(flow.simulate(self.config, native)["recipients"], ["member@example.com"])
        self.assertNotIn("长回复", json.dumps(flow.simulate(self.config, native)["card"], ensure_ascii=False))

    def test_mention_boundaries_and_code(self):
        cases = {
            "@example-member": True,
            "请看 @EXAMPLE-MEMBER，谢谢": True,
            "(@example-member).": True,
            "@example-member. Next": True,
            "@example-member\nnext": True,
            "name@example-member": False,
            "@example-member-extra": False,
            "@example-member.alias": False,
            "@example-member/group": False,
            "https://example.com/@example-member": False,
            "\\@example-member": False,
            "`@example-member`": False,
            "``@example-member``": False,
            "```python\nprint('`')\n@example-member\n```": False,
            "~~~\n@example-member\n~~~": False,
            "`@example-member` and @example-member": True,
            "@unknown-person @all": False,
            "A&B <text> \"quote\" @example-member": True,
        }
        for note, expected in cases.items():
            with self.subTest(note=note):
                self.assertEqual(flow.has_mention(note, "example-member"), expected)
                # Exercise the exact XPath shipped to the cloud when libxml is available.
                if shutil.which("xmllint"):
                    root = ET.Element("root")
                    ET.SubElement(root, "username").text = "example-member"
                    for part in flow.mention_text(note).split("@"):
                        ET.SubElement(root, "part").text = part
                    result = subprocess.run(["xmllint", "--xpath", flow.MENTION_XPATH, "-"],
                        input=ET.tostring(root), capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip() == b"true", expected)

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

    def test_mr_reviewer_notifications_are_opt_in(self):
        event = self.merge_request([10001])
        self.assertEqual(flow.simulate(self.config, event)["recipients"], [])
        legacy_config = self.config.copy()
        legacy_config.pop("notify_mr_reviewers")
        self.assertEqual(flow.simulate(flow.validate_config(legacy_config), event)["recipients"], [])
        with self.assertRaises(ValueError):
            flow.validate_config(dict(self.config, notify_mr_reviewers="true"))

    def test_new_mr_uses_mapped_reviewers_and_links_to_mr(self):
        self.config["notify_mr_reviewers"] = True
        event = self.merge_request([10001, 10002, 10003, 10999])
        result = flow.simulate(self.config, event)
        self.assertEqual(result["recipients"], ["owner@example.com", "member@example.com"])
        card = result["card"]
        self.assertEqual(card["body"][1]["inlines"][0]["text"], event["object_attributes"]["title"])
        self.assertEqual(card["body"][2]["inlines"][0]["text"], "Example project !7")
        self.assertEqual(card["actions"][0], {"type": "Action.OpenUrl", "title": "查看 MR",
                                             "url": event["object_attributes"]["url"]})
        self.assertEqual(flow.simulate(self.config, self.merge_request([]))["recipients"], [])
        event["project"]["id"] = 54321
        self.assertEqual(flow.simulate(self.config, event)["recipients"], [])
        self.assertEqual(self.recipients(), ["owner@example.com"])

    def test_mr_updates_only_notify_new_reviewer_ids(self):
        self.config["notify_mr_reviewers"] = True
        cases = [
            ("append", "update", [10001, 10002], {"reviewers": {"previous": [{"id": 10001}]}},
             ["member@example.com"]),
            ("numeric history", "update", [10001, 10002], {"reviewer_ids": {"previous": [10002]}},
             ["owner@example.com"]),
            ("first reviewer", "update", [10002], {"reviewers": {"previous": []}}, ["member@example.com"]),
            ("re-request", "update", [10001], {"reviewers": {
                "previous": [{"id": 10001, "state": "approved"}],
                "current": [{"id": 10001, "state": "unreviewed", "re_requested": True}],
            }}, []),
            ("remove", "update", [10001], {"reviewer_ids": {"previous": [10001, 10002]}}, []),
            ("no history", "update", [10001], {"title": {"previous": "Old title"}}, []),
            ("reopen", "reopen", [10001], {"reviewers": {"previous": []}}, []),
        ]
        for scenario, action, reviewers, changes, expected in cases:
            with self.subTest(scenario=scenario):
                event = self.merge_request(reviewers, action, changes)
                self.assertEqual(flow.simulate(self.config, event)["recipients"], expected)

    def mr_comment(self, actor_id=10999, note="Looks good", project=None, **merge_request):
        """Shaped like a real GitLab 19 comment event on a merge request."""
        project = project or {"id": 12345, "path": "demo/example-project"}
        mr = {"iid": 7, "title": 'Review "example"', "author_id": 10001, "assignee_ids": [], "reviewer_ids": [10002]}
        mr.update(merge_request)
        return {"object_kind": "note", "event_type": "note", "project_id": project["id"], "project": {"id": project["id"]},
                "user": {"id": actor_id, "name": "Example actor"},
                "object_attributes": {"action": "create", "system": False, "noteable_type": "MergeRequest", "note": note,
                    "url": f"https://gitlab.example.com/{project['path']}/-/merge_requests/7#note_1"},
                "merge_request": mr}

    def test_mr_comments_are_opt_in(self):
        self.assertEqual(flow.simulate(self.config, self.mr_comment())["recipients"], [])
        self.config["notify_mr_reviewers"] = True
        self.assertEqual(flow.simulate(self.config, self.mr_comment())["recipients"], [])
        with self.assertRaises(ValueError):
            flow.validate_config(dict(self.config, notify_mr_comments="true"))

    def test_mr_comment_notifies_reviewers_author_assignees_and_mentions(self):
        self.config["notify_mr_comments"] = True
        cases = [
            ("author and reviewer", {}, ["owner@example.com", "member@example.com"]),
            ("reviewer comments", {"actor_id": 10002}, ["owner@example.com"]),
            ("reviewer alias comments", {"actor_id": 10003}, ["owner@example.com"]),
            ("author comments", {"actor_id": 10001}, ["member@example.com"]),
            ("assignee only", {"author_id": 10998, "reviewer_ids": [], "assignee_ids": [10002]}, ["member@example.com"]),
            ("mention only", {"author_id": 10998, "reviewer_ids": [], "note": "@example-owner please look"},
             ["owner@example.com"]),
            ("unrelated, no mention", {"author_id": 10998, "reviewer_ids": []}, []),
            ("missing lists", {"author_id": None, "reviewer_ids": None, "assignee_ids": None}, []),
        ]
        for scenario, changes, expected in cases:
            with self.subTest(scenario=scenario):
                self.assertEqual(flow.simulate(self.config, self.mr_comment(**changes))["recipients"], expected)
        for attribute in ({"system": True}, {"action": "update"}):
            event = self.mr_comment()
            event["object_attributes"].update(attribute)
            with self.subTest(attribute=attribute):
                self.assertEqual(flow.simulate(self.config, event)["recipients"], [])
        second = self.mr_comment(project={"id": 12346, "path": "demo/second-project"})
        self.assertEqual(flow.simulate(self.config, second)["recipients"], ["owner@example.com"])
        self.assertEqual(self.reply(), ["owner@example.com", "member@example.com"])

    def test_mr_comment_card_links_to_the_comment(self):
        self.config["notify_mr_comments"] = True
        event = self.mr_comment()
        card = flow.simulate(self.config, event)["card"]
        self.assertEqual([card["body"][i].get("text") or card["body"][i]["inlines"][0]["text"] for i in range(3)],
                         ["GitLab MR 有新回复", 'Review "example"', "Example project !7"])
        self.assertEqual(card["actions"][0], {"type": "Action.OpenUrl", "title": "查看 MR",
                                             "url": event["object_attributes"]["url"]})
        reply = flow.simulate(self.config, dict(self.event, event_kind="note", action="create", issue_title="Issue",
                                                issue_iid=5))["card"]
        self.assertEqual([reply["body"][0]["text"], reply["body"][2]["inlines"][0]["text"]],
                         ["GitLab 工作项有新回复", "Example project #5"])
        self.assertEqual(reply["actions"][0]["title"], "打开工作项")

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
        for field, value in (("id", True), ("id", "12345"), ("id", 12346), ("path", "demo/../project"),
                             ("member_ids", [10001, 10001]), ("member_ids", ["10001"]), ("member_ids", None)):
            raw = copy.deepcopy(self.config)
            raw["projects"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                flow.validate_config(raw)
        for value in ("http://gitlab.example.com", "https://name:password@gitlab.example.com"):
            with self.subTest(gitlab_url=value), self.assertRaises(ValueError):
                flow.validate_config(dict(self.config, gitlab_url=value))
        for raw in (dict(self.config, projects=[]), {"project": self.config["projects"][0], **self.config}):
            with self.assertRaises(ValueError):
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

    def second_project(self, **changes):
        return dict(self.event, project_id=12346, url="https://gitlab.example.com/demo/second-project/-/issues/1",
                    **changes)

    def test_recipients_must_be_members_of_the_event_project(self):
        self.assertEqual(flow.simulate(self.config, self.second_project(assignee_ids=[10001, 10002]))["recipients"],
                         ["owner@example.com"])
        reply = self.second_project(event_kind="note", action="create", system=False, noteable_type="Issue",
            actor_id=10999, issue_author_id=10998, issue_assignee_ids=[], note_body="@example-owner @example-member")
        self.assertEqual(flow.simulate(self.config, reply)["recipients"], ["owner@example.com"])
        self.config["projects"][1]["member_ids"] = []
        self.assertEqual(flow.simulate(self.config, reply)["recipients"], [])

    def test_project_id_and_url_must_name_the_same_project(self):
        for changes in ({"project_id": 12346}, {"url": "https://gitlab.example.com/demo/second-project/-/issues/1"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.recipients(**changes), [])

    def test_legacy_single_project_config_is_upgraded(self):
        legacy = {key: copy.deepcopy(value) for key, value in self.config.items() if key not in ("gitlab_url", "projects")}
        legacy["project"] = {"id": 12345, "gitlab_url": "https://gitlab.example.com/", "path": "demo/example-project",
                             "display_name": "Example project"}
        config = flow.validate_config(legacy)
        self.assertEqual((config["gitlab_url"], config["projects"]), ("https://gitlab.example.com", [
            {"id": 12345, "path": "demo/example-project", "display_name": "Example project",
             "member_ids": [10001, 10002, 10003]}]))
        self.assertEqual(flow.simulate(config, self.event)["recipients"], ["owner@example.com"])
        self.assertEqual(flow.simulate(config, dict(self.event, assignee_ids=[10002]))["recipients"],
                         ["member@example.com"])
        legacy["members"].append(copy.deepcopy(legacy["members"][0]))
        with self.assertRaises(ValueError):
            flow.validate_config(legacy)

    def test_single_project_config_renders(self):
        self.config["projects"] = self.config["projects"][:1]
        self.assertTrue(flow.recipient_rule(self.config).code().startswith("@and(and(equals("))
        self.assertEqual(self.recipients(), ["owner@example.com"])

    def test_card_uses_event_project_label(self):
        card = flow.simulate(self.config, self.second_project(item_iid=3))["card"]
        self.assertEqual(card["body"][2]["inlines"][0]["text"], "Second project #3")
        card = flow.simulate(self.config, dict(self.event, project_id=None))["card"]
        self.assertEqual(card["body"][2]["inlines"][0]["text"], "GitLab #1")

    def test_source_check_fails_closed(self):
        secret = "s" * flow.SECRET_MIN_LENGTH
        cases = [
            ({"X-Gitlab-Token": secret}, secret, True),
            ({"X-Gitlab-Token": secret + "x"}, secret, False),
            ({}, secret, False),
            ({"X-Gitlab-Token": None}, secret, False),
            ({"X-Gitlab-Token": flow.SECRET_PLACEHOLDER}, flow.SECRET_PLACEHOLDER, False),
            ({"X-Gitlab-Token": "short"}, "short", False),
            ({"X-Gitlab-Token": ""}, "", False),
        ]
        for headers, configured, expected in cases:
            with self.subTest(headers=headers, configured=configured):
                self.assertEqual(flow.verify_source(headers, configured), expected)

    def test_source_gate_runs_before_member_map(self):
        actions = flow.build_actions(self.config)
        self.assertEqual(list(actions)[:3], ["WebhookSecret", "VerifySource", "MemberMap"])
        self.assertEqual(actions["WebhookSecret"]["inputs"], flow.SECRET_PLACEHOLDER)
        self.assertEqual(actions["WebhookSecret"]["runtimeConfiguration"]["secureData"]["properties"],
                         ["inputs"])
        self.assertEqual(actions["VerifySource"]["expression"], flow.f("not", flow.source_check()).code())
        reject = actions["VerifySource"]["actions"]["RejectUnverifiedRequest"]
        self.assertEqual((reject["type"], reject["inputs"]["runStatus"]), ("Terminate", "Failed"))
        self.assertEqual(actions["VerifySource"]["else"], {"actions": {}})
        self.assertEqual(actions["MemberMap"]["runAfter"], {"VerifySource": ["Succeeded"]})

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
