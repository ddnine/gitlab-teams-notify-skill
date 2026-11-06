#!/usr/bin/env python3
"""Check a skill package for common private data; findings never echo matched values."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


EXCLUDED_DIRS = {".git", "__pycache__", ".venv"}
PRIVATE_NAMES = {"config.local.json", "project-notify.json", "power-automate-actions.json",
                 "ui-values.json", "simulation.json", "deployment-notes.json"}
PRIVATE_DIRS = {".private", "private", "generated", "runtime", "logs"}
EMAIL = re.compile(r"[A-Za-z0-9_.+\-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
PATTERNS = {
    "credential-like value": re.compile(r"\b(?:glpat-[A-Za-z0-9_-]{15,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{25,})"),
    "signed URL": re.compile(r"[?&](?:sig|signature|access_token|token)=[A-Za-z0-9_%+/-]{8,}", re.I),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "tenant or flow identifier": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "personal home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
}


def example_domain(domain):
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d)
               for d in ("example.com", "example.net", "example.org", "example.invalid"))


def scan_content(content, deny_terms=()):
    findings = set()
    if any(not example_domain(match.group(1)) for match in EMAIL.finditer(content)):
        findings.add("non-example email")
    for category, pattern in PATTERNS.items():
        if pattern.search(content):
            findings.add(category)
    for term in deny_terms:
        # Boundaries keep short usernames from matching unrelated prose substrings.
        if term and re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", content, re.I):
            findings.add("private deny-list value")
    return findings


def candidate_files(root, staged):
    if staged:
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "-z"],
                                 check=True, capture_output=True).stdout
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            header, encoded_path = entry.split(b"\t", 1)
            mode, sha, stage = header.decode("ascii").split()
            name = encoded_path.decode("utf-8")
            if mode not in ("100644", "100755") or stage != "0":
                yield name, None, {"non-regular or unresolved index entry"}
                continue
            size = int(subprocess.run(["git", "-C", str(root), "cat-file", "-s", sha],
                                      check=True, capture_output=True).stdout)
            if size > 2_000_000:
                yield name, None, {"oversized artifact"}
                continue
            blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", sha],
                                  check=True, capture_output=True).stdout
            yield name, blob, set()
    else:
        def walk(directory):
            for path in sorted(directory.iterdir()):
                rel = path.relative_to(root).as_posix()
                if path.is_symlink():
                    yield rel, None, {"symlink"}
                elif path.is_dir():
                    if path.name not in EXCLUDED_DIRS:
                        yield from walk(path)
                elif path.is_file():
                    if path.stat().st_size > 2_000_000:
                        yield rel, None, {"oversized artifact"}
                    else:
                        yield rel, path.read_bytes(), set()
                else:
                    yield rel, None, {"non-regular file"}
        yield from walk(root)


def audit(root, deny_terms=(), staged=False):
    findings, count = [], 0
    for name, raw, categories in candidate_files(root, staged):
        count += 1
        path = Path(name)
        if (path.name in PRIVATE_NAMES or path.name == ".env" or path.name.startswith(".env.")
                or PRIVATE_DIRS.intersection(path.parts)
                or path.suffix.lower() in {".log", ".pem", ".key", ".zip", ".png", ".jpg", ".pdf"}):
            categories.add("private or binary artifact filename")
        categories.update(scan_content(name, deny_terms))
        if raw is not None:
            try:
                content = raw.decode("utf-8")
                if "\0" in content:
                    categories.add("binary content")
                categories.update(scan_content(content, deny_terms))
            except UnicodeDecodeError:
                categories.add("non-UTF-8 content")
        if categories:
            # Ordinals avoid leaking private values contained in filenames, too.
            findings.append({"file_number": count, "categories": sorted(categories)})
    return count, findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--deny-file", type=Path)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        terms = []
        if args.deny_file:
            deny = args.deny_file.resolve()
            if deny == root or root in deny.parents:
                raise ValueError("deny-list must be outside the published package")
            terms = [line.strip() for line in deny.read_text(encoding="utf-8").splitlines() if line.strip()]
        count, findings = audit(root, terms, args.staged)
        print(f"Scanned {count} files; flagged {len(findings)} files.")
        for finding in findings:
            print(f"File #{finding['file_number']}: {', '.join(finding['categories'])}")
        return 1 if findings else 0
    except (ValueError, OSError, subprocess.CalledProcessError):
        print("Audit failed: check package path, external deny-list and Git index.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
