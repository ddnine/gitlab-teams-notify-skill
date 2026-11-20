#!/usr/bin/env python3
"""Refresh projects[].member_ids from GitLab with read-only `glab api` calls. Sends no messages."""

import argparse
import copy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_flow  # noqa: E402


# Planner (15) and above can read confidential issues, so cards never reach a member who
# cannot open the item. Guests and minimal-access users are not notified.
MIN_ACCESS_LEVEL = 15
PAGE_SIZE = 100


def eligible(member, today):
    expires = member.get("expires_at")
    return (member.get("state") == "active" and member.get("access_level", 0) >= MIN_ACCESS_LEVEL
            and (not expires or expires >= today.isoformat()))


def plan(config, fetched, today):
    """Pure sync step. fetched maps project ID to {"path": ..., "members": [GitLab member objects]}."""
    mapped = {member["id"] for member in config["members"]}
    updated, report, unmapped = copy.deepcopy(config), [], {}
    for project in updated["projects"]:
        source = fetched[project["id"]]
        if source["path"] != project["path"]:
            raise ValueError(f"project {project['id']} path changed in GitLab; update projects[].path and re-run")
        members = {m["id"]: m for m in source["members"] if eligible(m, today)}
        new_ids = sorted(i for i in members if i in mapped)
        report.append({"project": project["path"],
            "added": sorted(set(new_ids) - set(project["member_ids"])),
            "removed": sorted(set(project["member_ids"]) - set(new_ids)),
            "unmapped": sorted(members[i]["username"] for i in members if i not in mapped)})
        project["member_ids"] = new_ids
        for i in members:
            if i not in mapped:
                entry = unmapped.setdefault(i, {"id": i, "username": members[i]["username"],
                    "name": members[i].get("name", ""), "projects": [], "email": ""})
                entry["projects"].append(project["path"])
    template = sorted(unmapped.values(), key=lambda m: m["username"].casefold())
    return updated, report, template


def glab_get(hostname, path):
    result = subprocess.run(["glab", "api", "--hostname", hostname, path], check=True, capture_output=True)
    return json.loads(result.stdout)


def fetch(config):
    hostname = urlsplit(config["gitlab_url"]).hostname
    fetched = {}
    for project in config["projects"]:
        members, page = [], 1
        while True:
            batch = glab_get(hostname, f"projects/{project['id']}/members/all?per_page={PAGE_SIZE}&page={page}")
            members.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        path = glab_get(hostname, f"projects/{project['id']}")["path_with_namespace"]
        fetched[project["id"]] = {"path": path, "members": members}
    return fetched


def replace_private(path, value):
    # mkstemp creates the file with mode 600; os.replace keeps the swap atomic.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--write", action="store_true", help="update member_ids in the config")
    parser.add_argument("--template", type=Path, help="private directory for members-to-fill.json")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
        build_flow.safe_output(config_path.parent)
        config = build_flow.validate_config(json.loads(config_path.read_text(encoding="utf-8")))
        updated, report, template = plan(config, fetch(config), date.today())
        for entry in report:
            print(f"{entry['project']}: +{entry['added']} -{entry['removed']} no email: {entry['unmapped']}")
        if args.template:
            out = build_flow.safe_output(args.template)
            build_flow.write_private(out / "members-to-fill.json", template)
            print(f"Wrote {len(template)} unmapped members to fill in.")
        if args.write and updated != config:
            replace_private(config_path, build_flow.validate_config(updated))
            print("Updated member_ids in config.")
        elif updated != config:
            print("member_ids are out of date; re-run with --write to update the config.")
        return 0
    except ValueError as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        return 1
    except (OSError, KeyError, subprocess.CalledProcessError):
        print("Sync failed: check glab authentication, project access and the private config path.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
