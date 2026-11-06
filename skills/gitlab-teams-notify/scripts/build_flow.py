#!/usr/bin/env python3
"""Offline Power Automate action builder. No network access or credentials required."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def equal(left, right):
    """JSON equality without Python's True == 1 coercion."""
    return type(left) is type(right) and left == right


def unique(values):
    result = []
    for value in values:
        if not any(equal(value, other) for other in result):
            result.append(value)
    return result


def as_string(value):
    return "" if value is None else value if isinstance(value, str) else compact(value)


class Expr:
    """Small expression tree shared by the renderer and offline routing simulator.

    This evaluates only the WDL subset used below, not arbitrary Power Automate code.
    """

    def __init__(self, kind, value, args=()):
        self.kind, self.value, self.args = kind, value, args

    def __getitem__(self, key):
        return Expr("get", key, (self,))

    def render(self):
        if self.kind == "literal":
            if isinstance(self.value, str):
                return "'" + self.value.replace("'", "''") + "'"
            if isinstance(self.value, (dict, list)):
                return "json(" + literal(compact(self.value)).render() + ")"
            return compact(self.value)
        if self.kind == "get":
            return self.args[0].render() + "?[" + literal(self.value).render() + "]"
        return self.value + "(" + ",".join(arg.render() for arg in self.args) + ")"

    def code(self):
        return "@" + self.render()

    def eval(self, context):
        if self.kind == "literal":
            return copy.deepcopy(self.value)
        if self.kind == "get":
            source = self.args[0].eval(context)
            return source.get(self.value) if isinstance(source, dict) else None
        name = self.value
        values = [arg.eval(context) for arg in self.args]
        if name in ("triggerBody", "item", "workflow"):
            return context[name]
        if name in ("body", "outputs", "items"):
            return context[name][values[0]]
        if name == "equals":
            return equal(*values)
        if name == "not":
            return not values[0]
        if name == "and":
            return all(values)
        if name == "or":
            return any(values)
        if name == "if":
            return values[1] if values[0] else values[2]
        if name == "coalesce":
            return next((v for v in values if v is not None), None)
        if name == "contains":
            haystack, needle = values
            if isinstance(haystack, list):
                return any(equal(needle, x) for x in haystack)
            if isinstance(haystack, str) and isinstance(needle, str):
                return needle in haystack
            raise ValueError("contains input has an invalid type")
        if name == "startsWith":
            return values[0].casefold().startswith(values[1].casefold())
        if name == "createArray":
            return values
        if name == "union":
            return unique([item for group in values for item in group])
        if name == "first":
            return values[0][0]
        if name in ("addProperty", "setProperty"):
            obj, key, value = values
            return dict(obj, **{key: value})
        if name == "string":
            return as_string(values[0])
        if name == "concat":
            return "".join(as_string(v) for v in values)
        if name == "take":
            return values[0][:values[1]]
        raise ValueError("unsupported offline expression")


def literal(value):
    return Expr("literal", value)


def f(name, *values):
    return Expr("call", name, tuple(v if isinstance(v, Expr) else literal(v) for v in values))


NATIVE_EVENT_PATHS = {
    "event_kind": ("object_kind",), "project_id": ("project", "id"),
    "action": ("object_attributes", "action"), "actor_id": ("user", "id"),
    "actor_name": ("user", "name"), "url": ("object_attributes", "url"),
    "item_id": ("object_attributes", "id"), "item_iid": ("object_attributes", "iid"),
    "item_title": ("object_attributes", "title"),
    "assignee_ids": ("object_attributes", "assignee_ids"),
    "previous_assignees": ("changes", "assignees", "previous"),
    "issue_author_id": ("issue", "author_id"), "issue_assignee_ids": ("issue", "assignee_ids"),
    "issue_iid": ("issue", "iid"), "issue_title": ("issue", "title"),
    "note_body": ("object_attributes", "note"), "system": ("object_attributes", "system"),
    "noteable_type": ("object_attributes", "noteable_type"),
}


def event_field(key):
    """Accept native GitLab events and the legacy flattened custom template."""
    native = f("triggerBody")
    for part in NATIVE_EVENT_PATHS[key]:
        native = native[part]
    return f("coalesce", f("triggerBody")[key], native)


class EventFields:
    def __getitem__(self, key):
        return event_field(key)


# Keep matching within one @ segment. The previous segment supplies the left
# boundary; the username is XML data, never interpolated into the XPath query.
MENTION_WORD = "abcdefghijklmnopqrstuvwxyz0123456789_.-/"
MENTION_LEFT = MENTION_WORD + "\\@"
MENTION_NEXT = "substring(.,string-length(../username)+1,1)"
MENTION_AFTER_DOT = "substring(.,string-length(../username)+2,1)"
MENTION_PREV = "preceding-sibling::part[1]"
MENTION_XPATH = (
    "boolean(/root/part[position()>1 and string-length(../username)>0"
    " and ((position()=2 and string-length(" + MENTION_PREV + ")=0)"
    " or (string-length(" + MENTION_PREV + ")>0 and not(contains('" + MENTION_LEFT
    + "',substring(" + MENTION_PREV + ",string-length(" + MENTION_PREV + "),1)))))"
    " and starts-with(.,../username)"
    " and (string-length(.)=string-length(../username)"
    " or not(contains('" + MENTION_WORD + "'," + MENTION_NEXT + "))"
    " or (" + MENTION_NEXT + "='.' and (string-length(.)=string-length(../username)+1"
    " or not(contains('" + MENTION_WORD + "'," + MENTION_AFTER_DOT + ")))))])"
)
CODE_DELIMITERS = ("```", "~~~", "``", "`")


def mention_text(text):
    """Conservative plain-text mentions, excluding common Markdown code forms.

    This is not a full GitLab Markdown renderer; group mentions are not expanded.
    """
    text = as_string(text).lower().replace("\\@", " ").replace("\\`", " ")
    for delimiter in CODE_DELIMITERS:
        text = " ".join(text.split(delimiter)[::2])
    return text


def has_mention(text, username):
    username = username.lower()
    if not username:
        return False
    parts = mention_text(text).split("@")
    for index, part in enumerate(parts[1:], 1):
        previous = parts[index - 1]
        left_ok = (index == 1 and not previous) or (bool(previous) and previous[-1] not in MENTION_LEFT)
        if not left_ok or not part.startswith(username):
            continue
        rest = part[len(username):]
        if (not rest or rest[0] not in MENTION_WORD
                or (rest[0] == "." and (len(rest) == 1 or rest[1] not in MENTION_WORD))):
            return True
    return False


def mention_condition():
    text = f("replace", f("replace", f("toLower", f("string", f("coalesce",
        event_field("note_body"), ""))), "\\@", " "), "\\`", " ")
    for delimiter in CODE_DELIMITERS:
        chunks = f("xml", f("addProperty", {}, "root", f("addProperty", {}, "part", f("split", text, delimiter))))
        text = f("join", f("xpath", chunks, "/root/part[position() mod 2 = 1]/text()"), " ")
    document = f("xml", f("addProperty", {}, "root", f("addProperty",
        f("addProperty", {}, "username", f("toLower", f("coalesce", f("item")["username"], ""))),
        "part", f("split", text, "@"))))
    return f("xpath", document, MENTION_XPATH)


class MentionExpr(Expr):
    def render(self):
        return mention_condition().render()

    def eval(self, context):
        return has_mention(event_field("note_body").eval(context), context["item"].get("username", ""))


def positive_id(value):
    return type(value) is int and 0 < value <= 9007199254740991


def https_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("URL must be an HTTPS string without whitespace")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("URL must use HTTPS and contain no credentials, query or fragment")
    return value.rstrip("/")


def validate_config(raw):
    config = copy.deepcopy(raw)
    required = {"project", "members", "teams_connection_reference", "flow_management_url"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("config must contain exactly the documented fields")
    project = config["project"]
    if not isinstance(project, dict) or set(project) != {"id", "gitlab_url", "path", "display_name"}:
        raise ValueError("project fields do not match the example schema")
    if not positive_id(project["id"]):
        raise ValueError("project.id must be a positive numeric ID")
    project["gitlab_url"] = https_url(project["gitlab_url"])
    path = project["path"]
    if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("project.path must be a namespace/project path")
    label = project["display_name"]
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise ValueError("project.display_name must contain 1 to 120 characters")
    members = config["members"]
    if not isinstance(members, list) or not members:
        raise ValueError("members must be a nonempty array")
    seen = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"id", "username", "email"}:
            raise ValueError("member fields must be id, username and email")
        if not positive_id(member["id"]) or member["id"] in seen:
            raise ValueError("member IDs must be positive, numeric and unique")
        seen.add(member["id"])
        if not isinstance(member["username"], str) or not member["username"].strip():
            raise ValueError("member.username must be a nonempty GitLab username for mention matching")
        email = member["email"]
        if not isinstance(email, str) or not re.fullmatch(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", email):
            raise ValueError("member.email must contain a single plain email address")
        member["email"] = email.lower()
    ref = config["teams_connection_reference"]
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", ref):
        raise ValueError("teams_connection_reference must be a connection reference name")
    config["flow_management_url"] = https_url(config["flow_management_url"])
    if not re.fullmatch(r"/environments/[^/]+/flows/[^/]+/details", urlsplit(config["flow_management_url"]).path):
        raise ValueError("flow_management_url must be the flow details page, not a callback")
    return config


def recipient_rule(config):
    event, member = EventFields(), f("item")
    assignees = f("coalesce", event["assignee_ids"], [])
    previous = f("body", "PreviousAssignees")
    actor_email = f("first", f("union", f("body", "Actor"), [{"email": ""}]))["email"]
    assigned = f("and",
        f("contains", ["issue", "work_item"], event["event_kind"]),
        f("contains", assignees, member["id"]),
        f("or", f("equals", event["action"], "open"), f("and",
            f("equals", event["action"], "update"),
            f("not", f("equals", event["previous_assignees"], None)),
            f("not", f("contains", previous, f("addProperty", {}, "id", member["id"]))))))
    replied = f("and",
        f("equals", event["event_kind"], "note"),
        f("equals", event["action"], "create"),
        f("equals", event["system"], False),
        f("contains", ["Issue", "WorkItem"], event["noteable_type"]),
        f("or", f("contains", f("coalesce", event["issue_assignee_ids"], []), member["id"]),
            f("equals", event["issue_author_id"], member["id"]), MentionExpr("mention", None)),
        f("not", f("equals", member["email"], actor_email)))
    project = config["project"]
    return f("and", f("equals", event["project_id"], project["id"]),
        f("startsWith", f("coalesce", event["url"], ""), project["gitlab_url"] + "/" + project["path"] + "/"),
        f("or", assigned, replied))


def card_expression(config):
    event = EventFields()
    note = f("equals", event["event_kind"], "note")
    title = f("if", note, event["issue_title"], event["item_title"])
    iid = f("if", note, event["issue_iid"], event["item_iid"])
    def rich(text):
        return f("setProperty", {"type": "RichTextBlock"}, "inlines", f("createArray",
            f("setProperty", {"type": "TextRun"}, "text", f("string", text))))
    body = f("createArray",
        f("setProperty", {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "wrap": True},
            "text", f("if", note, "GitLab 工作项有新回复", "GitLab 工作项分配提醒")),
        rich(f("take", f("string", title), 1000)),
        rich(f("concat", config["project"]["display_name"], " #", f("string", iid))),
        rich(f("concat", "操作人：", f("take", f("string", event["actor_name"]), 120))))
    card = f("setProperty", {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.2"}, "body", body)
    return f("setProperty", card, "actions", f("createArray",
        f("setProperty", {"type": "Action.OpenUrl", "title": "打开工作项"}, "url", event["url"])))


def build_actions(config):
    actions = {}
    def add(name, action):
        action["runAfter"] = {next(reversed(actions)): ["Succeeded"]} if actions else {}
        actions[name] = action
    add("MemberMap", {"type": "Compose", "inputs": literal(config["members"]).code()})
    add("PreviousAssignees", {"type": "Select", "inputs": {
        "from": f("coalesce", event_field("previous_assignees"), []).code(),
        "select": {"id": f("item")["id"].code()}}})
    add("Actor", {"type": "Query", "inputs": {"from": f("outputs", "MemberMap").code(),
        "where": f("equals", f("item")["id"], event_field("actor_id")).code()}})
    add("Recipients", {"type": "Query", "inputs": {"from": f("outputs", "MemberMap").code(),
        "where": recipient_rule(config).code()}})
    add("RecipientEmails", {"type": "Select", "inputs": {"from": f("body", "Recipients").code(),
        "select": {"email": f("item")["email"].code()}}})
    def connector(operation, params):
        return {"type": "OpenApiConnection", "inputs": {"host": {
            "apiId": "/providers/Microsoft.PowerApps/apis/shared_teams",
            "connectionReferenceName": config["teams_connection_reference"],
            "operationId": operation}, "parameters": params}}
    post = connector("PostCardToConversation", {
        "poster": "Flow bot", "location": "Chat with Flow bot",
        "body/recipient": f("items", "SendNotifications")["email"].code(),
        "body/messageBody": f("string", card_expression(config)).code()})
    post["runAfter"] = {}
    add("SendNotifications", {"type": "Foreach", "foreach": f("union",
        f("body", "RecipientEmails"), f("body", "RecipientEmails")).code(), "actions": {"PostCard": post}})
    run_url = f("concat", config["flow_management_url"].removesuffix("/details") + "/runs/", f("workflow")["run"]["name"])
    alert = connector("PostMessageToSelf", {"body/body/contentType": "text", "body/body/content": f("concat",
        "GitLab → Teams 通知失败\n项目：", config["project"]["display_name"], "\n检查运行：", run_url,
        "\n可能已有部分消息送达，请检查失败收件人后再重试。").code()})
    alert["runAfter"] = {"SendNotifications": ["Failed", "TimedOut", "Skipped"]}
    actions["FailureAlert"] = alert
    actions["MarkFailed"] = {"type": "Terminate", "runAfter": {"FailureAlert": ["Succeeded"]},
        "inputs": {"runStatus": "Failed", "runError": {"code": "GitLabNotificationFailed",
            "message": "GitLab notification processing failed; inspect the failed action."}}}
    return actions


def simulate(config, event):
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    previous = event_field("previous_assignees").eval({"triggerBody": event})
    previous = [] if previous is None else previous
    if not isinstance(previous, list) or any(not isinstance(p, dict) for p in previous):
        raise ValueError("previous_assignees must be an array of objects or null")
    context = {"triggerBody": event, "body": {
        "PreviousAssignees": [{"id": p.get("id")} for p in previous],
        "Actor": [m for m in config["members"] if equal(m["id"], event_field("actor_id").eval({"triggerBody": event}))]}}
    rule = recipient_rule(config)
    recipients = [m["email"] for m in config["members"] if rule.eval(dict(context, item=m))]
    return {"recipients": unique(recipients), "card": card_expression(config).eval(context),
        "limitations": ["offline simulation only", "no runtime permission check", "no cross-run deduplication"]}


def expression_fields(value, path=""):
    result = []
    if isinstance(value, str) and value.startswith("@"):
        result.append({"path": path, "code": value, "expression_editor": value[1:],
            "text_interpolation": "@{" + value[1:] + "}"})
    elif isinstance(value, dict):
        for key, child in value.items():
            result.extend(expression_fields(child, path + "/" + key))
    return result


def safe_output(path):
    skill = Path(__file__).resolve().parents[1]
    # In the published layout: repository/skills/skill-name. Also protect installed skills.
    protected = [skill]
    if skill.parent.name == "skills":
        protected.append(skill.parent.parent)
    protected.extend(p for p in skill.parents if (p / ".git").exists())
    out = path.resolve()
    if any(out == p or p in out.parents for p in protected):
        raise ValueError("output directory must be outside the skill and source repository")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.stat().st_mode & 0o077:
        raise ValueError("output directory must be private (permission 700)")
    return out


def write_private(path, value):
    # O_EXCL avoids overwriting another deployment and refuses existing symlinks.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--event", type=Path)
    args = parser.parse_args()
    try:
        config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
        actions = build_actions(config)
        files = {"power-automate-actions.json": actions, "ui-values.json": {
            "format": "actions fragment, not an importable flow package",
            "expression_fields": expression_fields(actions)}}
        if args.event:
            files["simulation.json"] = simulate(config, json.loads(args.event.read_text(encoding="utf-8")))
        out = safe_output(args.out)
        if any((out / name).exists() or (out / name).is_symlink() for name in files):
            raise ValueError("output files already exist; choose a new private directory")
        for name, value in files.items():
            write_private(out / name, value)
        print(f"Generated {len(files)} private files. No network requests or messages were sent.")
        return 0
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        # Do not echo configuration values, credentials or personal paths in error output.
        print("Generation failed: check config/event schema and use a new private output directory outside the repository.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
