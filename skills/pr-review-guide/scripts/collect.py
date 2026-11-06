#!/usr/bin/env python3
"""Collect a PR's or a branch's diff into the files the review guide is built from.

Usage:
  collect.py --pr N [--repo DIR] --out DIR
  collect.py [--base REF] [--head REF] [--repo DIR] --out DIR
  collect.py --self-test

Writes DIR/hunks.json, which the page embeds, DIR/review.diff, the reviewable
hunks annotated with their ids for the model to read, DIR/batches/*.diff, the
same text split into worker-sized pieces, and DIR/description.md, the PR
description and commit messages, kept apart so a checker can read the diff
without the author's account of it.

The diff is three-dot, as GitHub shows it: merge-base of base and head, to head.
Without --pr, the base is the branch's PR base when one exists, else the remote's
default branch (origin/HEAD), else origin/main.
"""
import argparse
import codecs
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

CONTEXT = 3
BATCH_LINES = 800
EMBED_MAX_BYTES = 300_000
DELETED_PREVIEW = 40

LOCKFILES = re.compile(
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|"
    r"uv\.lock|poetry\.lock|Pipfile\.lock|go\.sum|Cargo\.lock|Gemfile\.lock|composer\.lock)$"
)
GENERATED = re.compile(
    r"(\.gen\.[^/]+$|_pb2(_grpc)?\.pyi?$|\.pb\.go$|_generated\.[^/]+$|\.generated\.[^/]+$|"
    r"(^|/)__snapshots__/|\.snap$|\.min\.(js|css)$|^(dist|build|vendor)/|(^|/)node_modules/|\.js\.map$)"
)
GENERATED_MARKER = re.compile(
    r"@generated\b|code generated .* do not edit|generated .*do not edit|do not edit.*generated|"
    r"auto-?generated (by|from|file|code)|this file (is|was|has been) (automatically |auto-?)?generated", re.I)
TESTS = re.compile(
    r"(\.(test|spec)\.[^/]+$|(^|/)(__tests__|tests?|e2e|testdata|fixtures)/|_test\.go$|"
    r"(^|/)test_[^/]+\.py$|_test\.py$|(^|/)conftest\.py$|(^|/)spec/|_spec\.rb$)"
)
AGENT_GUIDANCE = re.compile(r"(^|/)(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.claude/|\.cursor/|\.github/copilot)")
DOCS = re.compile(r"(\.(md|mdx|rst|adoc)$|(^|/)docs?/)")
LOCALIZATION = re.compile(r"(^|/)(locales?|i18n|l10n|translations)/|\.(po|pot|xliff)$")
ASSETS = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|bmp|mp4|webm|mov|woff2?|ttf|otf|eot|pdf)$", re.I)

ATTR_CATEGORY = {
    "review-implementation": "implementation", "review-test": "test",
    "review-documentation": "docs", "linguist-documentation": "docs",
    "review-generated": "generated", "linguist-generated": "generated",
    "review-agent-guidance": "agent-guidance", "review-localization": "localization",
    "review-assets": "assets",
}
NOISE = {"lockfile", "generated"}


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args],
                       capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def gh_json(repo, *args):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=repo)
    except FileNotFoundError:
        return None  # no gh: a branch still works from git alone
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def remote_name(repo):
    """owner/repo from the origin URL, for hosts gh does not know; the folder name without one."""
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(\.git)?/?$", url)
    return m.group(1) if m else os.path.basename(repo)


PR_FIELDS = "number,title,url,body,author,baseRefName,headRefName,baseRefOid,headRefOid,closingIssuesReferences"

GH_ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
TRACKER_URL = re.compile(r"https?://(?:linear\.app/[\w-]+/issue|[\w.-]+/browse)/([A-Z][A-Z0-9]*-\d+)[^\s)>\]]*")
KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
BRANCH_KEY = re.compile(r"^([a-z][a-z0-9]{1,9}-\d{1,6})(?=$|[-_])", re.I)
# A key leading the title, in either case, as `bac-1555: adds the thing`.
TITLE_KEY = re.compile(r"^\W*([a-z][a-z0-9]{1,9}-\d{1,6})\b", re.I)
# The words GitHub and Linear read before an issue: the first set closes it on merge, Linear's extra pair
# closes only a Linear or Jira issue, the third only links, and skip or ignore stops a link being made.
CLOSING = r"close[sd]?|closing|fix(?:e[sd]|ing)?|resolve[sd]?|resolving"
LINEAR_CLOSING = r"complete[sd]?|completing|implement(?:s|ed|ing)?"
RELATED = r"refs?|references|part\s+of|relate[sd]\s+to|contributes\s+to|towards?"
REF = (r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+|https?://(?:linear\.app/[\w-]+/issue|[\w.-]+/browse)/"
       r"[A-Z][A-Z0-9]*-\d+[^\s,)>\]]*|(?:[\w.-]+/[\w.-]+)?#\d+|\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
MAGIC = re.compile(rf"(?i:\b({CLOSING}|{LINEAR_CLOSING}|{RELATED}|skip|ignore)\b):?\s+"
                   rf"((?:{REF})(?:(?:\s*,\s*|\s+(?i:and)\s+|\s+)(?:{REF}))*)")
# Things shaped like an issue key that are not one.
NOT_KEYS = {"UTF", "SHA", "ISO", "RFC", "CVE", "HTTP", "TLS", "GPT", "PEP", "RELEASE", "HOTFIX", "FIX", "FEATURE",
            "BUGFIX", "CHORE", "REVERT", "DEPS"}
MAX_ISSUES = 5


def same_owner(a, b):
    return bool(a and b) and a.split("/")[0].lower() == b.split("/")[0].lower()


def parse_ref(text, name):
    """("gh", (owner/repo, n), url) or ("key", KEY, url) for one issue reference."""
    m = GH_ISSUE_URL.match(text)
    if m:
        return "gh", (m.group(1), int(m.group(2))), text
    m = TRACKER_URL.match(text)
    if m:
        return "key", m.group(1), text
    if "#" in text:
        where, _, n = text.partition("#")
        return "gh", (where or name, int(n)), None
    return "key", text, None


def linked_issues(repo, name, pr, title, body, commits, branch):
    """The issues a change says it works on: GitHub ones with their text, Jira or Linear keys for the session
    to fetch with its own tracker tool. Returns (issues for the page, issue.md text).

    Each says whether the PR closes it: a closing reference, a closing word, or a key in the title or branch
    name, which is how Linear links a PR. Only issues under the repo's own owner are fetched. The PR's author
    writes the links, so one to another owner's repo would otherwise pull whatever the reviewer's gh login can
    read into the model's context."""
    found = {"gh": {}, "key": {}}

    def add(kind, ident, url, closes):
        x = found[kind].setdefault(ident, {"url": None, "closes": False})
        x["url"] = x["url"] or url
        x["closes"] = x["closes"] or closes

    for x in (pr or {}).get("closingIssuesReferences") or []:
        r = x.get("repository") or {}
        owner = (r.get("owner") or {}).get("login")
        add("gh", (f"{owner}/{r['name']}" if owner and r.get("name") else name, x["number"]), x.get("url"), True)
    text = "\n".join([title, body, *commits])
    skipped = set()
    for m in MAGIC.finditer(text):
        word = m.group(1).lower()
        for ref in re.finditer(REF, m.group(2)):
            kind, ident, url = parse_ref(ref.group(0), name)
            if word in ("skip", "ignore"):
                skipped.add(ident)
            else:
                add(kind, ident, url, bool(re.fullmatch(CLOSING, word) or
                                           (kind == "key" and re.fullmatch(LINEAR_CLOSING, word))))
    for where, n in GH_ISSUE_URL.findall(text):
        add("gh", (where, int(n)), None, False)
    for m in TRACKER_URL.finditer(text):
        add("key", m.group(1), m.group(0), False)
    tail = branch.rsplit("/", 1)[-1]
    for k in KEY.findall(title) + [m.upper() for m in TITLE_KEY.findall(title) + BRANCH_KEY.findall(tail)]:
        add("key", k, None, True)
    for k in KEY.findall("\n".join(c.split("\n", 1)[0] for c in commits)):
        add("key", k, None, False)
    issues, parts = [], []
    gh = [(k, v) for k, v in found["gh"].items() if k not in skipped]
    for (where, n), x in gh[:MAX_ISSUES]:
        ref = f"#{n}" if where == name else f"{where}#{n}"
        got = gh_json(repo, "issue", "view", str(n), "-R", where, "--json", "title,body,url") \
            if same_owner(where, name) else None
        issues.append({"ref": ref, "url": (got or {}).get("url") or x["url"], "fetched": bool(got), "closes": x["closes"]})
        if got:
            how = "This PR closes it." if x["closes"] else "Linked from this PR, which doesn't close it."
            parts.append(f"## {ref}: {got['title']}\n{got['url']}\n{how}\n\n"
                         f"{(got.get('body') or '').strip() or '(no description)'}")
    keys = [(k, v) for k, v in found["key"].items() if k not in skipped and k.split("-")[0] not in NOT_KEYS]
    for k, x in keys[:MAX_ISSUES]:
        issues.append({"ref": k, "url": x["url"], "fetched": False, "closes": x["closes"]})
    return issues, "\n\n".join(parts)




def resolve(a):
    repo = a.repo
    name = (gh_json(repo, "repo", "view", "--json", "nameWithOwner") or {}).get("nameWithOwner") or remote_name(repo)
    pr = None
    if a.pr:
        pr = gh_json(repo, "pr", "view", str(a.pr), "--json", PR_FIELDS)
        if not pr:
            sys.exit(f"gh pr view {a.pr} failed; is gh installed and logged in, and is {repo} the right clone?")
        git(repo, "fetch", "-q", "origin", f"refs/pull/{pr['number']}/head", pr["baseRefName"])
        head, base = pr["headRefOid"], pr["baseRefOid"]
        base_label, head_label = pr["baseRefName"], pr["headRefName"]
    else:
        head_label = a.head or git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head = git(repo, "rev-parse", a.head or "HEAD").strip()
        if a.base:
            base_label = a.base
        else:
            pr = gh_json(repo, "pr", "view", head_label, "--json", PR_FIELDS)
            if pr:
                base_label = "origin/" + pr["baseRefName"]
            else:
                default = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).strip()
                base_label = next((b for b in (default, "origin/main", "origin/master")
                                   if b and git(repo, "rev-parse", "-q", "--verify", b, check=False).strip()), None)
                if not base_label:
                    sys.exit("no --base given and no PR, origin/HEAD or origin/main to compare against")
        base = git(repo, "rev-parse", base_label).strip()
    merge_base = git(repo, "merge-base", base, head).strip()
    log = git(repo, "log", "--reverse", "--format=%H%x01%h %s%n%b%x00", f"{merge_base}..{head}")
    entries = [c.strip().split("\x01", 1) for c in log.split("\0") if c.strip()]
    shas, commits = [e[0] for e in entries], [e[1].strip() for e in entries]
    issues, issue_text = linked_issues(repo, name, pr, pr["title"] if pr else "", (pr or {}).get("body") or "",
                                       commits, head_label)
    checks, annotations = None, []
    if pr:
        rows = gh_json(repo, "pr", "checks", str(pr["number"]), "--json", "name,bucket,link")
        if rows is not None:
            checks = {k: sum(1 for r in rows if r["bucket"] == k) for k in ("pass", "fail", "pending", "cancel")}
            checks["failing"] = [{"name": r["name"], "link": r["link"]} for r in rows if r["bucket"] == "fail"]
            # Without --pr the diff is the local head, which may not be the commit CI ran on.
            checks["stale"] = pr["headRefOid"] != head
            if not checks["stale"]:
                annotations = ci_annotations(repo, name, head)
    return {
        "repo": name, "number": pr and pr["number"], "title": pr["title"] if pr else head_label,
        "url": pr and pr["url"], "body": (pr or {}).get("body") or "",
        "author": ((pr or {}).get("author") or {}).get("login") or git(repo, "log", "-1", "--format=%an", head).strip(),
        "base": base_label, "head": head_label, "baseSha": base, "headSha": head, "mergeBase": merge_base,
        "commits": commits, "shas": shas, "checks": checks, "issues": issues, "issueText": issue_text,
        "annotations": annotations,
    }


ANN_RUNS = 20
ANN_MAX = 200


def ci_annotations(repo, name, head):
    """The warnings and failures the head's check runs pinned to a line, such as lint and type errors."""
    runs = (gh_json(repo, "api", f"repos/{name}/commits/{head}/check-runs?per_page=100") or {}).get("check_runs") or []
    out = []
    for run in [r for r in runs if (r.get("output") or {}).get("annotations_count")][:ANN_RUNS]:
        for a in gh_json(repo, "api", f"repos/{name}/check-runs/{run['id']}/annotations?per_page=50") or []:
            # Actions pins its own job errors ("exit code 1") to .github, which is not a file.
            if a.get("annotation_level") in ("warning", "failure") and a.get("path") not in (None, "", ".github") \
                    and a.get("start_line"):
                out.append({"check": run.get("name") or "", "link": run.get("html_url"), "path": a["path"],
                            "line": a["start_line"], "end": a.get("end_line") or a["start_line"],
                            "level": a["annotation_level"], "title": (a.get("title") or "")[:200],
                            "message": (a.get("message") or "")[:600]})
    return out[:ANN_MAX]


def place_annotations(files, anns):
    """Each annotation with the file, hunk and new-side line it lands on in the diff, or none when it is outside."""
    by_path = {f["path"]: f for f in files if not f["noise"]}
    for a in anns:
        f = by_path.get(a["path"])
        rows = [(h, r) for h in (f or {}).get("hunks", []) for r in h["lines"]
                if r[0] != "-" and a["line"] <= r[2] <= a["end"]]
        rows.sort(key=lambda x: (x[1][0] != "+", x[1][2]))
        if rows:
            h, r = rows[0]
            a.update(file=f["id"], hunk=h["id"], row=f"R{r[2]}")
    return anns


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def _unquote(p):
    """A path as git prints it in a diff header: quoted with C escapes when it holds odd characters."""
    p = p.rstrip("\t")
    if len(p) >= 2 and p[0] == p[-1] == '"':
        return codecs.escape_decode(p[1:-1].encode())[0].decode("utf-8", "replace")
    return p


GIT_PATH = r'("(?:\\.|[^"\\])*"|\S+)'


def parse_diff(text):
    files, f, h = [], None, None
    old_left = new_left = 0
    for line in text.split("\n"):
        if h is not None and (old_left > 0 or new_left > 0):
            tag, body = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag == "-":
                h["lines"].append(["-", h["_o"], None, body]); h["_o"] += 1; old_left -= 1
            elif tag == "+":
                h["lines"].append(["+", None, h["_n"], body]); h["_n"] += 1; new_left -= 1
            else:
                h["lines"].append([" ", h["_o"], h["_n"], body]); h["_o"] += 1; h["_n"] += 1
                old_left -= 1; new_left -= 1
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            # The ---/+++ lines carry the paths unambiguously; the header is a fallback for binary files.
            m = re.match(rf"diff --git {GIT_PATH} {GIT_PATH}$", line) or re.match(r"diff --git (a/.*) (b/.*)$", line)
            a_path, b_path = (_unquote(m.group(1))[2:], _unquote(m.group(2))[2:]) if m else ("", "")
            f = {"path": b_path, "oldPath": a_path, "status": "modified", "hunks": []}
            files.append(f); h = None
        elif f is None:
            continue
        elif line.startswith("new file mode"):
            f["status"] = "added"
        elif line.startswith("deleted file mode"):
            f["status"] = "deleted"
        elif line.startswith("rename from "):
            f["status"], f["oldPath"] = "renamed", _unquote(line[len("rename from "):])
        elif line.startswith("rename to "):
            f["path"] = _unquote(line[len("rename to "):])
        elif line.startswith("Binary files "):
            f["binary"] = True
        elif line.startswith("--- "):
            if line != "--- /dev/null":
                f["oldPath"] = _unquote(line[4:])[2:]
        elif line.startswith("+++ "):
            if line != "+++ /dev/null":
                f["path"] = _unquote(line[4:])[2:]
            elif f["status"] == "deleted":
                f["path"] = f["oldPath"]
        elif (m := HUNK_RE.match(line)):
            o, oc, n, nc, ctx = m.groups()
            h = {"oldStart": int(o), "newStart": int(n), "header": ctx.strip(), "lines": [],
                 "_o": int(o), "_n": int(n)}
            old_left = int(oc) if oc is not None else 1
            new_left = int(nc) if nc is not None else 1
            f["hunks"].append(h)
    for f in files:
        if f["status"] == "deleted" and not f["path"]:
            f["path"] = f["oldPath"]
        for h in f["hunks"]:
            del h["_o"], h["_n"]
    return files


INDENT_SENSITIVE = re.compile(r"\.(py|pyi|ya?ml|sass|haml|slim|coffee|nim)$|(^|/)(Makefile|[^/]*\.mk)$", re.I)
JS_LIKE = re.compile(r"\.([mc]?[jt]sx?|mts|cts)$", re.I)
PYTHON = re.compile(r"\.pyi?$", re.I)
STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`")
IMPORT = re.compile(
    r"^\s*(import\b|from\s+\S+\s+import\b|export\s+(type\s+)?(\*|\{).*\bfrom\b|#\s*include\b|use\s+[\w:{}, ]+;|"
    r"(const|let|var)\s+\S+\s*=\s*require\(|\}\s*from\s+[\"'])")
# A line of a Go import block: a path, perhaps aliased. Go only, so `return "forbidden"` elsewhere is code.
GO_IMPORT = re.compile(r"^\s*(?!(?:return|case|go|defer|var|const|type)\b)(\w+\s+|\.\s+)?\"[\w./-]+\"\s*$")
# Where a space between two tokens is part of what the line says: shell, Dockerfiles, YAML and other config.
WS_SIGNIFICANT = re.compile(r"\.(sh|bash|zsh|fish|ya?ml|toml|ini|cfg|conf|env|properties)$|"
                            r"(^|/)(Dockerfile[^/]*|Makefile|[^/]*\.mk|\.env[^/]*)$", re.I)
IMPORT_MEMBER = re.compile(r"^\s*(type\s+)?[\w$]+(\s+as\s+[\w$]+)?,\s*$")


def _flat(text, quotes, punct, spaced=False):
    """Code with layout whitespace removed outside string literals, which are kept as they are. A space
    between two words is kept (`not ready` is not `notready`); with `spaced`, every gap is kept as one space.

    ponytail: in JS a line break after `return` changes meaning (ASI); joining lines here
    misses that, and no formatter produces it.
    """
    strings = []

    def keep(m):
        lit = m.group(0)
        if quotes and lit[0] in "'\"":
            lit = '"' + lit[1:-1] + '"'
        strings.append(lit)
        return "\0"

    code = STRING.sub(keep, text)
    if punct:
        # A semicolon ending a line goes before the gaps are settled, so `a;` then `b` reads like `a` then `b`.
        code = re.sub(r";[ \t]*(?=\n|$)", "", code)
    if spaced:
        code = " ".join(code.split())
    else:
        # With punct, a line break between two words stays one, so `return;` then `foo()` is not `return foo()`.
        code = re.sub(r"(?<=[\w$])(\s+)(?=[\w$])|\s+",
                      lambda m: ("\n" if punct and "\n" in m.group(1) else " ") if m.group(1) else "", code)
    if punct:
        code = code.replace(";", "")
        code = re.sub(r",(?=[)\]}>]|$)", "", code)
        code = re.sub(r"\((\w+)\)=>", r"\1=>", code)
    return code, tuple(strings)


def _canon(lines, path, fmt):
    """What a block says once layout is ignored. Indentation counts where the language makes it count."""
    quotes = fmt and bool(JS_LIKE.search(path) or PYTHON.search(path))
    punct = fmt and bool(JS_LIKE.search(path))
    spaced = bool(WS_SIGNIFICANT.search(path))
    if INDENT_SENSITIVE.search(path):
        return [(len(l) - len(l.lstrip()), _flat(l, quotes, punct, spaced)) for l in lines if l.strip()]
    return _flat("\n".join(lines), quotes, punct, spaced)


def _imports_only(lines, path):
    rows = [l for l in lines if l.strip()]
    hit = (lambda l: IMPORT.search(l) or GO_IMPORT.search(l)) if path.endswith(".go") else IMPORT.search
    return bool(rows) and any(hit(l) for l in rows) and all(hit(l) or IMPORT_MEMBER.match(l) for l in rows)


def _key(line):
    return _flat(line, False, False)


def block_kind(removed, added, path):
    """Why a change block is noise, or None when it is a real change."""
    if _canon(removed, path, False) == _canon(added, path, False):
        return "whitespace"
    if _canon(removed, path, True) == _canon(added, path, True):
        return "formatting"
    if _imports_only(removed, path) and _imports_only(added, path) and \
            sorted(_key(l) for l in removed if l.strip()) == sorted(_key(l) for l in added if l.strip()):
        return "reordered"
    return None


def whitespace_blind_changes(repo, base, head):
    """Per path, the old and new line numbers still changed once whitespace is ignored, as GitHub's
    hide-whitespace view shows them."""
    raw = git(repo, "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "-M", "-w", "-U0",
              base, head)
    out = {}
    for f in parse_diff(raw):
        old, new = out.setdefault(f["path"], (set(), set()))
        for h in f["hunks"]:
            for t, o, n, _body in h["lines"]:
                (old if t == "-" else new).add(o if t == "-" else n)
    return out


def mark_reindented(f, ws):
    """Mark the changed lines of each real block that only moved in or out a level, by `ws`, the lines a
    whitespace-blind diff still calls changed. They count as whitespace. Where indentation means something,
    only when the hunk also changes a line opening a level the moved line crossed, so a statement quietly
    moved out of a block stays a change."""
    old, new = ws
    sensitive = INDENT_SENSITIVE.search(f["path"])
    words = lambda s: " ".join(s.split())
    for h in f["hunks"]:
        changed = [t != " " and (o in old if t == "-" else n in new) for t, o, n, _b in h["lines"]]
        openers = {_indent(r[3]) for r, c in zip(h["lines"], changed) if c and r[3].rstrip().endswith(":")}
        # Where each line's words sit on the other side. -w also ignores a space dropped between words, so a
        # line has only moved if its words are there too.
        other = {"-": {}, "+": {}}
        for t, _o, _n, body in h["lines"]:
            if t != " ":
                other["+" if t == "-" else "-"].setdefault(words(body), set()).add(_indent(body))

        def crossed(row):
            d = _indent(row[3])
            return any(min(d, d2) <= o < max(d, d2) for d2 in other[row[0]][words(row[3])] for o in openers)

        for b in h["blocks"]:
            if b["noise"]:
                continue
            moved = [k for k in range(b["start"], b["end"]) if not changed[k] and words(h["lines"][k][3]) in other[h["lines"][k][0]]]
            real = [h["lines"][k] for k in moved if h["lines"][k][3].strip()]
            if moved and (not sensitive or not real or any(crossed(r) for r in real)):
                b["reindented"] = moved


def _indent(line):
    return len(line) - len(line.lstrip())


def mark_blocks(f):
    """Split each hunk into runs of changed lines and say which runs are noise."""
    pure = []
    for h in f["hunks"]:
        h["blocks"], start = [], None
        for i, (t, *_rest) in enumerate(h["lines"] + [[" "]]):
            if t != " " and start is None:
                start = i
            elif t == " " and start is not None:
                rows = h["lines"][start:i]
                rem = [r[3] for r in rows if r[0] == "-"]
                add = [r[3] for r in rows if r[0] == "+"]
                b = {"start": start, "end": i, "noise": block_kind(rem, add, f["path"])}
                h["blocks"].append(b)
                if b["noise"] is None and bool(rem) != bool(add) and _imports_only(rem or add, f["path"]):
                    pure.append((b, rem, add))
                start = None
    # Imports removed in one place and added unchanged in another have only moved. Code that moves
    # stays a change: the same line can mean something else in another function.
    gone = Counter(_key(x) for _b, rem, _a in pure for x in rem if x.strip())
    came = Counter(_key(x) for _b, _r, add in pure for x in add if x.strip())
    if gone and gone == came:
        for b, _r, _a in pure:
            b["noise"] = "moved"
    for h in f["hunks"]:
        kinds = {b["noise"] for b in h["blocks"]}
        h["noise"] = None if None in kinds or not kinds else (kinds.pop() if len(kinds) == 1 else "formatting")


def categorise(path, attrs, head_text):
    for attr, cat in ATTR_CATEGORY.items():
        if attrs.get(attr) == "set":
            return cat, f".gitattributes {attr}"
    if LOCKFILES.search(path):
        return "lockfile", "lock file"
    generated_off = "unset" in (attrs.get("linguist-generated"), attrs.get("review-generated"))
    if not generated_off and GENERATED.search(path):
        return "generated", "generated path"
    if not generated_off and head_text and GENERATED_MARKER.search("\n".join(head_text.split("\n")[:5])):
        return "generated", "generated-file header"
    unset = {ATTR_CATEGORY[k] for k, v in attrs.items() if v == "unset"}
    for rx, cat in ((TESTS, "test"), (AGENT_GUIDANCE, "agent-guidance"), (DOCS, "docs"),
                    (LOCALIZATION, "localization"), (ASSETS, "assets")):
        if rx.search(path) and cat not in unset:
            return cat, None
    return "implementation", None


RISK_HINTS = [(label, re.compile(rx, re.I)) for label, rx in (
    ("migration", r"(^|/)(migrations?|alembic|migrate)/|\.sql$"),
    ("dependencies", r"(^|/)(package\.json|go\.mod|pyproject\.toml|requirements[^/]*\.txt|Cargo\.toml|Gemfile|"
                     r"composer\.json|setup\.py|setup\.cfg)$"),
    ("auth", r"auth|permission|rbac|\bacl|oauth|jwt|session|password|credential|secret|crypt|token"),
    ("deploy or CI config", r"(^|/)(\.github/workflows/|helm/|charts/|terraform/|k8s/|Dockerfile|docker-compose)"),
)]


def risk_hints(path):
    return [label for label, rx in RISK_HINTS if rx.search(path)]


ASSERTION = re.compile(
    r"\b(assert\w*|expect\(|self\.assert\w+|t\.(Error|Errorf|Fatal|Fatalf)\(|require\.\w+\(|assert\.\w+\(|"
    r"\.should\b|\.to(Be|Equal|Have|Throw|Contain|Match)\w*\()")
TEST_DEF = re.compile(r"^\s*(def test_\w+|func Test\w+|(it|test)\(\s*[\"'`]|(it|specify|test)\s+[\"']|#\[test\]|@Test\b)")
# (kind, label, side, tests only, JS/TS only, pattern). Narrow on purpose: a noisy list stops being read.
TELL_RULES = [(k, label, side, tests, js, re.compile(rx)) for k, label, side, tests, js, rx in (
    ("skip", "test skipped", "+", True, False,
     r"\b(it|test|describe|context)\.(skip|todo)\(|(?<![.\w])x(it|describe|context|specify|test)\s*\(?\s*[\"'`]|"
     r"@pytest\.mark\.(skip|xfail)\b|@unittest\.skip|\bpytest\.skip\(|\bt\.Skip(Now|f)?\(|#\[ignore\b|"
     r"@(Disabled|Ignore)\b|^\s*(skip|pending)\s*\(?\s*[\"']"),
    # Only with a test name after it, so `fit(x, y)` or `model.fit(x)` in a test is not a focused test.
    ("focus", "focused test left in", "+", True, False,
     r"\b(it|test|describe|context)\.only\(|(?<![.\w])f(it|describe|context)\s*\(?\s*[\"'`]"),
    ("debug", "debug output left in", "+", False, False,
     r"\bconsole\.(log|debug|trace|dir)\(|\bdebugger\b|\bbreakpoint\(\)|\bpdb\.set_trace\(|^\s*import pdb\b|\bdbg!\(|"
     r"\bbinding\.(pry|irb)\b|\bbyebug\b"),
    ("silenced", "check silenced", "+", False, False,
     r"@ts-(ignore|nocheck|expect-error)|eslint-disable|biome-ignore|#\s*noqa\b|#\s*type:\s*ignore|//\s*nolint|"
     r"#\s*pylint:\s*disable|#\[allow\(|#\s*rubocop:\s*disable|@SuppressWarnings\b"),
    ("any", "typed as any", "+", False, True, r"\bas\s+any\b|:\s*any\b|<any>"),
    ("todo", "new TODO", "+", False, False, r"(//|#|/\*|^\s*\*)\s*(TODO|FIXME|XXX|HACK)\b"),
    ("commented-code", "commented-out code", "+", False, False,
     r"^\s*(//|#)\s*(const|let|var|return|if|for|await|import|def|class|func|print|console\.)\b.*([;{}(=:]\s*$)"),
)]


def find_tells(files):
    """Lines a reviewer should not have to find: weakened tests, debug leftovers, silenced checks."""
    tells = []

    def add(kind, label, f, h, row):
        side, n = ("L", row[1]) if row[0] == "-" else ("R", row[2])
        tells.append({"kind": kind, "label": label, "file": f["id"], "hunk": h["id"], "line": f"{side}{n}",
                      "text": row[3].strip()[:160]})

    for f in files:
        if f["noise"] or f["category"] not in ("implementation", "test"):
            continue
        is_test = f["category"] == "test"
        if is_test and f["status"] == "deleted" and f["hunks"]:
            add("test-removed", "test file deleted", f, f["hunks"][0], f["hunks"][0]["lines"][0])
            continue
        added = {_key(r[3]) for h in f["hunks"] for r in h["lines"] if r[0] == "+"}
        js = bool(JS_LIKE.search(f["path"]))
        for h in f["hunks"]:
            for b in h["blocks"]:
                if b["noise"]:
                    continue
                # Lines that only moved were already there, so they say nothing new.
                moved = set(b.get("reindented", ())) | moved_rows(h)
                rows = [r for k, r in enumerate(h["lines"][b["start"]:b["end"]], b["start"]) if k not in moved]
                block_adds_assert = any(r[0] == "+" and ASSERTION.search(r[3]) for r in rows)
                for row in rows:
                    for kind, label, side, tests, js_only, rx in TELL_RULES:
                        if row[0] == side and (is_test or not tests) and (js or not js_only) and rx.search(row[3]):
                            add(kind, label, f, h, row)
                            break
                    if is_test and row[0] == "-" and _key(row[3]) not in added:
                        if TEST_DEF.search(row[3]):
                            add("test-removed", "test removed", f, h, row)
                        elif ASSERTION.search(row[3]):
                            add("assertion", "assertion changed" if block_adds_assert else "assertion removed", f, h, row)
    return tells


def check_attrs(repo, head, paths):
    if not paths:
        return {}
    r = subprocess.run(["git", "-C", repo, "check-attr", "--source", head, "-z", "--stdin", *ATTR_CATEGORY],
                       input="\0".join(paths) + "\0", capture_output=True, text=True)
    out, parts = {}, r.stdout.split("\0")
    for i in range(0, len(parts) - 2, 3):
        p, attr, val = parts[i:i + 3]
        if val in ("set", "true"):
            out.setdefault(p, {})[attr] = "set"
        elif val in ("unset", "false"):
            out.setdefault(p, {})[attr] = "unset"
    return out


def read_blobs(repo, specs):
    """Contents of `rev:path` specs through one cat-file process, None for missing or binary."""
    if not specs:
        return {}
    r = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="\n".join(specs).encode() + b"\n",
                       capture_output=True)
    out, data, pos = {}, r.stdout, 0
    for spec in specs:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode(errors="replace").split()
        pos = nl + 1
        if len(header) < 3:
            out[spec] = None
            continue
        size = int(header[2])
        blob = data[pos:pos + size]
        pos += size + 1
        out[spec] = None if header[1] != "blob" or b"\0" in blob[:8000] else blob.decode("utf-8", errors="replace")
    return out


DEF_PATTERNS = [re.compile(x) for x in (
    r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)",
    r"\b(?:const|let|var|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    r"^\s*(?:(?:export|public|private|protected|static|readonly|async)\s+)*([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>",
    r"^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    r"^\s*(?:(?:public|private|protected|static|async)\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^{]+)?\{\s*$",
    r"^\s*(?:async\s+)?def\s+(\w+)", r"^\s*class\s+(\w+)",
    r"\bfunc\s+(?:\([^)]*\)\s*)?(\w+)", r"^\s*type\s+(\w+)",
)]
NOT_NAMES = {"if", "for", "while", "switch", "catch", "return", "function", "else", "constructor", "super",
             "async", "await", "new", "typeof", "with", "try", "do", "get", "set", "default", "export"}
IDENT = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*")
# camelCase, snake_case or Capitalised: one-word locals like `token` or `data` tie unrelated hunks together.
DISTINCT = re.compile(r"[a-z][A-Z]|[A-Za-z]_[A-Za-z]|^[A-Z][a-z]")
COMMENT_OR_STRING = re.compile(r"//.*$|/\*.*?\*/|\s#.*$|^\s*(?:\*|#).*$|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")


def code_only(text):
    return COMMENT_OR_STRING.sub(" ", text)


def defined_names(lines):
    names = set()
    for text in lines:
        text = code_only(text)
        for rx in DEF_PATTERNS:
            for m in rx.finditer(text):
                if m.group(1) not in NOT_NAMES and DISTINCT.search(m.group(1)):
                    names.add(m.group(1))
    return names


def connect(files):
    """Link hunks through the names one defines (or removes) and another uses.

    Only names defined in exactly one hunk count, so a renamed or moved symbol, or a
    common name like `data`, never ties two hunks together.
    """
    live = [h for f in files if not f["noise"] for h in f["hunks"] if not h["noise"]]
    added = {h["id"]: defined_names(r[3] for r in h["lines"] if r[0] == "+") for h in live}
    removed = {h["id"]: defined_names(r[3] for r in h["lines"] if r[0] == "-") - added[h["id"]] for h in live}
    owners = {}
    for hid in added:
        for name in added[hid]:
            owners.setdefault(name, set()).add(hid)
    added_anywhere = set(owners)
    for hid in removed:
        for name in removed[hid] - added_anywhere:
            owners.setdefault(name, set()).add(hid)
    unique = {name: next(iter(hs)) for name, hs in owners.items() if len(hs) == 1}
    for h in live:
        h["uses"], h["usedBy"] = {}, {}
        # Kept for the split check: a type declared in one hunk and implemented in another must ship together.
        h["defines"] = sorted(added[h["id"]])
    by_id = {h["id"]: h for h in live}
    for h in live:
        tokens = {t for r in h["lines"] if r[0] != " " for t in IDENT.findall(code_only(r[3]))}
        for name in sorted(tokens & unique.keys()):
            src = unique[name]
            if src != h["id"]:
                h["uses"][name] = src
                by_id[src]["usedBy"].setdefault(h["id"], []).append(name)


MOVE_MIN_LINES = 3
MOVE_MIN_ALNUM = 40


def find_moves(files):
    """Runs of removed lines that come back unchanged somewhere else in the diff: code moved, not written.

    Lines match once their indentation is ignored; where indentation means something they must also keep it
    relative to each other. A move is still a change, since the same lines can mean something else in their
    new place, so the page shows the new copy marked as moved and folds the old one into a pointer to it."""
    def runs(tag):
        out = []
        for f in files:
            if f["noise"]:
                continue
            for h in f["hunks"]:
                h.setdefault("moves", [])
                skip = {i for b in h["blocks"] if b["noise"] for i in range(b["start"], b["end"])}
                skip |= {i for b in h["blocks"] for i in b.get("reindented", ())}
                run = []
                for i, r in enumerate(h["lines"] + [[" "]]):
                    if r[0] == tag and i not in skip:
                        run.append(i)
                    elif run:
                        out.append((f, h, run))
                        run = []
        return out

    removed, added = runs("-"), runs("+")
    text = lambda h, i: h["lines"][i][3].strip()
    index = {}
    for rn, (_f, h, idx) in enumerate(removed):
        for k, i in enumerate(idx):
            if sum(c.isalnum() for c in text(h, i)) >= 4:
                index.setdefault(text(h, i), []).append((rn, k))
    used = set()
    for f2, h2, idx2 in added:
        k = 0
        while k < len(idx2):
            best = (0, None)
            for rn, rk in index.get(text(h2, idx2[k]), [])[:50]:
                f1, h1, idx1 = removed[rn]
                sensitive = INDENT_SENSITIVE.search(f1["path"]) or INDENT_SENSITIVE.search(f2["path"])
                d1, d2 = _indent(h1["lines"][idx1[rk]][3]), _indent(h2["lines"][idx2[k]][3])
                n = 0
                while k + n < len(idx2) and rk + n < len(idx1) and (rn, rk + n) not in used:
                    a, b = h2["lines"][idx2[k + n]][3], h1["lines"][idx1[rk + n]][3]
                    if a.strip() != b.strip() or (sensitive and a.strip() and _indent(a) - d2 != _indent(b) - d1):
                        break
                    n += 1
                while n and not text(h2, idx2[k + n - 1]):
                    n -= 1
                if n > best[0]:
                    best = (n, (rn, rk))
            n, src = best
            body = [text(h2, idx2[k + j]) for j in range(n)]
            if src and sum(1 for x in body if x) >= MOVE_MIN_LINES and sum(c.isalnum() for x in body for c in x) >= MOVE_MIN_ALNUM:
                rn, rk = src
                _f1, h1, idx1 = removed[rn]
                used.update((rn, rk + j) for j in range(n))
                h1["moves"].append({"side": "-", "start": idx1[rk], "end": idx1[rk] + n, "to": h2["id"],
                                    "line": f"R{h2['lines'][idx2[k]][2]}"})
                h2["moves"].append({"side": "+", "start": idx2[k], "end": idx2[k] + n, "from": h1["id"],
                                    "line": f"L{h1['lines'][idx1[rk]][1]}"})
                k += n
            else:
                k += 1
    for f in files:
        for h in f["hunks"]:
            h["moves"] = sorted(h.get("moves", []), key=lambda m: m["start"])


def moved_rows(h, side=None):
    return {i for m in h.get("moves", ()) if side in (None, m["side"]) for i in range(m["start"], m["end"])}


def hunk_sig(f, h):
    """What a hunk changes, without where: the same edit after a rebase or a regrouping keeps its signature."""
    body = "\n".join(r[0] + r[3] for r in h["lines"] if r[0] != " ")
    return hashlib.sha1(f"{f['path']}\n{body}".encode()).hexdigest()[:12]


def blame(repo, rng, path, lines, reverse=False):
    """{line: sha} for the given lines: the commit in the range that wrote each, or with reverse, the last one
    it still existed in. Lines older than the range come back as the range's own start."""
    spans, out = [], {}
    for n in sorted(lines):
        if spans and n == spans[-1][1] + 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    args = ["blame", "--porcelain", *(["--reverse"] if reverse else []),
            *(x for a, b in spans for x in ("-L", f"{a},{b}")), rng, "--", path]
    for m in re.finditer(r"^([0-9a-f]{40}) \d+ (\d+)", git(repo, *args, check=False), re.M):
        out[int(m.group(2))] = m.group(1)
    return out


COMMIT_MAP_MAX_FILES = 150


def map_commits(repo, meta, files):
    """Give each hunk the commits that wrote its added lines or removed its removed ones, as indexes into
    meta["commits"], so the page can show one commit's part of the change."""
    shas = meta["shas"]
    work = [f for f in files if not f["noise"] and f["hunks"]]
    if len(shas) < 2 or len(work) > COMMIT_MAP_MAX_FILES:
        return False
    at = {s: i for i, s in enumerate(shas)}
    rng = f"{meta['mergeBase']}..{meta['headSha']}"
    children = {}
    for line in git(repo, "rev-list", "--reverse", "--parents", rng).split("\n"):
        sha, *parents = line.split() or [""]
        for p in parents:
            children.setdefault(p, []).append(sha)
    for f in work:
        new = {r[2] for h in f["hunks"] for r in h["lines"] if r[0] == "+"}
        old = {r[1] for h in f["hunks"] for r in h["lines"] if r[0] == "-"}
        wrote = blame(repo, rng, f["path"], new) if new and f["status"] != "deleted" else {}
        kept = blame(repo, rng, f["oldPath"] or f["path"], old, reverse=True) if old else {}
        for h in f["hunks"]:
            got = {at[wrote[r[2]]] for r in h["lines"] if r[0] == "+" and wrote.get(r[2]) in at}
            for r in h["lines"]:
                # A line last seen in a commit was removed by the first commit after it.
                nxt = [at[c] for c in children.get(kept.get(r[1]), []) if c in at] if r[0] == "-" else []
                got.update(nxt[:1])
            h["commits"] = sorted(got)
    return True


def pass_path(repo, meta):
    """Where this clone remembers the heads it built a page for, per PR or branch, inside its git directory."""
    common = git(repo, "rev-parse", "--git-common-dir").strip()
    key = f"pr-{meta['number']}" if meta["number"] else "branch-" + re.sub(r"[^\w.-]", "_", meta["head"])
    return os.path.join(repo, common, "pr-review-guide", "passes", key + ".json")


def read_passes(path):
    try:
        with open(path) as fh:
            return json.load(fh).get("passes") or []
    except (OSError, ValueError, AttributeError):
        return []


def mark_since(data, passes):
    """Mark the hunks that are new since the last page built at another head. A hunk counts as new when its
    signature is, so a rebase that only moves the change leaves it as it was."""
    head = data["meta"]["headSha"]
    prev = next((p for p in reversed(passes) if p.get("head") != head), None)
    if not prev:
        return
    seen, now = set(prev.get("sigs") or []), set()
    fresh = 0
    for f in data["files"]:
        for h in f["hunks"]:
            now.add(h["sig"])
            h["fresh"] = h["sig"] not in seen
            fresh += h["fresh"] and not f["noise"] and not h["noise"]
    data["meta"]["lastPass"] = {"headSha": prev["head"], "at": prev.get("at"), "fresh": fresh, "gone": len(seen - now)}


def record_pass(path, data, keep=5):
    passes = [p for p in read_passes(path) if p.get("head") != data["meta"]["headSha"]]
    passes.append({"head": data["meta"]["headSha"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "sigs": [h["sig"] for f in data["files"] for h in f["hunks"]]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"passes": passes[-keep:]}, fh)


def collect(a):
    meta = resolve(a)
    raw = git(a.repo, "diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "-M",
              f"-U{CONTEXT}", meta["mergeBase"], meta["headSha"])
    files = parse_diff(raw)
    ws = whitespace_blind_changes(a.repo, meta["mergeBase"], meta["headSha"])
    attrs = check_attrs(a.repo, meta["headSha"], [f["path"] for f in files])
    blobs = read_blobs(a.repo, [f"{meta['headSha']}:{f['path']}" for f in files if f["status"] != "deleted"])
    stats, noise_lines = {}, {"add": 0, "del": 0}
    for i, f in enumerate(files, 1):
        f["id"] = f"f{i}"
        text = blobs.get(f"{meta['headSha']}:{f['path']}")
        f["category"], f["why"] = categorise(f["path"], attrs.get(f["path"], {}), text)
        f["noise"] = f["category"] in NOISE
        f["risk"] = [] if f["noise"] else risk_hints(f["path"])
        if f["status"] in ("modified", "renamed") and text is not None and not f["noise"] \
                and len(text) <= EMBED_MAX_BYTES:
            f["newText"] = text.split("\n")
        mark_blocks(f)
        if not f["noise"]:
            mark_reindented(f, ws.get(f["path"], (set(), set())))
        f["additions"] = f["deletions"] = 0
        for j, h in enumerate(f["hunks"], 1):
            h["id"] = f"{f['id']}h{j}"
            h["sig"] = hunk_sig(f, h)
            h["oldLines"] = sum(1 for r in h["lines"] if r[0] != "+")
            h["newLines"] = sum(1 for r in h["lines"] if r[0] != "-")
            for b in h["blocks"]:
                rows = h["lines"][b["start"]:b["end"]]
                add = sum(1 for r in rows if r[0] == "+")
                dele = len(rows) - add
                f["additions"] += add
                f["deletions"] += dele
                if b["noise"] and not f["noise"]:
                    noise_lines["add"] += add
                    noise_lines["del"] += dele
                else:
                    moved = [] if f["noise"] else b.get("reindented", [])
                    moved_add = sum(1 for k in moved if h["lines"][k][0] == "+")
                    noise_lines["add"] += moved_add
                    noise_lines["del"] += len(moved) - moved_add
                    s = stats.setdefault(f["category"], {"files": set(), "add": 0, "del": 0})
                    s["add"] += add - moved_add
                    s["del"] += dele - (len(moved) - moved_add)
            for b in h["blocks"]:
                if not b["noise"] or f["noise"]:
                    stats.setdefault(f["category"], {"files": set(), "add": 0, "del": 0})["files"].add(f["id"])
    for s in stats.values():
        s["files"] = len(s["files"])
    find_moves(files)
    connect(files)
    tells = find_tells(files)
    meta["commitMap"] = map_commits(a.repo, meta, files)
    annotations = place_annotations(files, meta.pop("annotations"))
    total = {"files": len(files), "add": sum(f["additions"] for f in files), "del": sum(f["deletions"] for f in files)}
    moved = sum(m["end"] - m["start"] for f in files for h in f["hunks"] for m in h["moves"] if m["side"] == "+")
    data = {"meta": meta, "stats": {"categories": stats, "noise": noise_lines, "total": total, "moved": moved},
            "files": files, "tells": tells, "annotations": annotations}
    mark_since(data, read_passes(pass_path(a.repo, meta)))
    return data


def reviewable_text(data):
    """The annotated diff the model reads, plus how many changed lines it holds."""
    m = data["meta"]
    out = [f"# {m['repo']}{'#' + str(m['number']) if m['number'] else ''}",
           f"# base {m['base']} ({m['mergeBase'][:9]}), head {m['head']} ({m['headSha'][:9]})", "", ""]
    chunks, noise_hunks, noise_files, count = [], [], [], 0
    for f in data["files"]:
        if f["noise"]:
            noise_files.append(f"{f['id']} {f['path']} ({f['why']})")
            continue
        head = f"=== {f['id']} {f['path']} ({f['status']}{', from ' + f['oldPath'] if f['status'] == 'renamed' else ''}"
        head += f", {f['category']}, +{f['additions']} -{f['deletions']})"
        if f.get("risk"):
            head += f"  risk hint: {', '.join(f['risk'])}"
        if f.get("binary"):
            chunks.append((f, [head, "(binary file)"], 1))
            continue
        if f["hunks"] and all(h["noise"] for h in f["hunks"]):
            noise_hunks += [f"{h['id']} ({h['noise']}) in {f['path']}" for h in f["hunks"]]
            continue
        lines, n = [head], 0
        for h in f["hunks"]:
            if h["noise"]:
                noise_hunks.append(f"{h['id']} ({h['noise']}) in {f['path']}")
                continue
            lines.append(f"--- {h['id']} new {h['newStart']}-{h['newStart'] + max(h['newLines'] - 1, 0)}"
                         f", old {h['oldStart']}-{h['oldStart'] + max(h['oldLines'] - 1, 0)}"
                         + (f"  [{h['header']}]" if h["header"] else ""))
            if h.get("uses"):
                lines.append("    uses: " + ", ".join(f"{n} ({src})" for n, src in h["uses"].items()))
            if h.get("usedBy"):
                lines.append("    used by: " + ", ".join(f"{hid} ({', '.join(ns)})" for hid, ns in h["usedBy"].items()))
            noisy = {i for b in h["blocks"] if b["noise"] for i in range(b["start"], b["end"])}
            moved = {i for b in h["blocks"] for i in b.get("reindented", ())}
            gone = moved_rows(h, "-")
            moves = {m["start"]: m for m in h.get("moves", ())}
            shown = 0
            for i, (t, _o, _n, body) in enumerate(h["lines"]):
                if i in moves:
                    mv = moves[i]
                    lines.append(f"  ({mv['end'] - mv['start']} lines moved unchanged to {mv['to']})" if t == "-" else
                                 f"  (the next {mv['end'] - mv['start']} lines moved here unchanged from {mv['from']})")
                if i in gone:
                    continue
                if i in moved:
                    # shown once, as context, the way a whitespace-blind diff shows it
                    if t == "+":
                        lines.append(" " + body)
                    continue
                if i in noisy:
                    if i - 1 not in noisy:
                        lines.append("  (formatting-only lines elided)")
                    continue
                if f["status"] == "deleted" and shown >= DELETED_PREVIEW:
                    lines.append(f"  ({len(h['lines']) - i} more removed lines)")
                    break
                lines.append(t + body)
                shown += 1
                n += t != " "
        chunks.append((f, lines, n))
        count += n
    for _f, lines, _n in chunks:
        out += lines + [""]
    if noise_hunks:
        out += ["# Formatting-only hunks, kept out of the sections:", *("#   " + x for x in noise_hunks)]
    if noise_files:
        out += ["# Lock and generated files, kept out of the sections:", *("#   " + x for x in noise_files)]
    out[2] = f"# reviewable changed lines: {count} ({size_band(count)[0]})"
    return "\n".join(out) + "\n", chunks, count


def description_text(m):
    out = [f"# {m['title']}", *([m["url"]] if m.get("url") else []), "", "## PR description", "",
           m["body"].strip() or "(none)", "", "## Commits", ""]
    for c in m.get("commits") or []:
        subject, _, body = c.partition("\n")
        out.append(f"- {subject}")
        out += [f"  {l}" for l in body.strip().split("\n") if l.strip()]
    return "\n".join(out) + "\n"


# Reviewable lines per band. Unguided review studies put one sitting at about 400 lines; the guide splits the
# change into sections and drops the noise, so a sitting holds more. A split is suggested from the second band.
SIZE_BANDS = ((800, "fits one sitting", "good"), (2000, "long for one review", "warn"),
              (float("inf"), "too big for one review", "bad"))


def size_band(count):
    """The band's label and its tone on the page."""
    return next((label, tone) for top, label, tone in SIZE_BANDS if count <= top)


def ready_check(data, count):
    impl = data["stats"]["categories"].get("implementation", {})
    tests = data["stats"]["categories"].get("test", {})
    files = sum(1 for f in data["files"] if not f["noise"] and any(not h["noise"] for h in f["hunks"]))
    band, tone = size_band(count)
    return {"lines": count, "files": files, "band": band, "tone": tone,
            "implChanged": bool(impl.get("add") or impl.get("del")),
            "testsChanged": bool(tests.get("add") or tests.get("del")),
            "checks": data["meta"].get("checks")}


def write(data, out_dir):
    os.makedirs(os.path.join(out_dir, "batches"), exist_ok=True)
    text, chunks, count = reviewable_text(data)
    data["stats"]["reviewable"] = count
    data["ready"] = ready_check(data, count)
    issue_text = data["meta"].pop("issueText", "")
    issue_path = os.path.join(out_dir, "issue.md")
    if issue_text:
        with open(issue_path, "w") as fh:
            fh.write(issue_text + "\n")
    elif os.path.exists(issue_path):
        os.remove(issue_path)
    with open(os.path.join(out_dir, "description.md"), "w") as fh:
        fh.write(description_text(data["meta"]))
    with open(os.path.join(out_dir, "hunks.json"), "w") as fh:
        json.dump(data, fh)
    with open(os.path.join(out_dir, "review.diff"), "w") as fh:
        fh.write(text)
    batches, cur, size = [], [], 0
    for _f, lines, _n in chunks:
        if cur and size + len(lines) > BATCH_LINES:
            batches.append(cur)
            cur, size = [], 0
        cur += lines + [""]
        size += len(lines)
    if cur:
        batches.append(cur)
    for old in os.listdir(os.path.join(out_dir, "batches")):
        os.remove(os.path.join(out_dir, "batches", old))
    for i, b in enumerate(batches, 1):
        with open(os.path.join(out_dir, "batches", f"batch-{i}.diff"), "w") as fh:
            fh.write("\n".join(b))
    return count, len(batches)


def self_test():
    ts, py, yml, sql = "a.ts", "a.py", "values.yaml", "q.sql"
    assert block_kind(["  foo(a, b)"], ["foo(a, b)"], ts) == "whitespace"
    assert block_kind(["foo(a,", "  b)"], ["foo(a, b)"], ts) == "whitespace"
    assert block_kind(["foo(a, b,)"], ["foo(a, b)"], ts) == "formatting"
    assert block_kind(["x = 'a';"], ['x = "a"'], ts) == "formatting"
    assert block_kind(["const f = (x) => x"], ["const f = x => x"], ts) == "formatting"
    assert block_kind(["a = 1;", "b = 2;"], ["a = 1", "b = 2"], ts) == "formatting"
    assert block_kind(["return;", "foo()"], ["return foo()"], ts) is None
    assert block_kind(["return; foo()"], ["return foo()"], ts) is None
    assert block_kind(["a; b"], ["a b"], ts) is None
    assert block_kind(["x=1"], ["x = 1"], py) == "whitespace"
    assert block_kind(["x = 'a'"], ['x = "a"'], py) == "formatting"
    assert block_kind(["import b", "import a"], ["import a", "import b"], ts) == "reordered"
    assert block_kind(["import {", "  b,", "  a,", '} from "x";'], ["import {", "  a,", "  b,", '} from "x";'], ts) == "reordered"
    assert block_kind(["foo(a)"], ["foo(b)"], ts) is None
    assert block_kind([], ["new line"], ts) is None
    assert block_kind([], [""], ts) == "whitespace"
    # Each of these changes behaviour, so none may be called noise.
    assert block_kind(["    do_work(req)"], ["do_work(req)"], py) is None
    assert block_kind(["key:", "  nested: 1"], ["nested: 1"], yml) is None
    assert block_kind(["return", "  value"], ["return value"], py) is None
    assert block_kind(['sep = " "'], ['sep = ""'], ts) is None
    assert block_kind(["where name = 'foo'"], ['where name = "foo"'], sql) is None
    assert block_kind(["const s = 'hi ${name}'"], ["const s = `hi ${name}`"], ts) is None
    assert block_kind(["x = (a,)"], ["x = (a)"], py) is None
    assert block_kind(["x = 1,"], ["x = 1"], py) is None
    assert block_kind(['sep = ";"'], ['sep = ""'], ts) is None
    assert block_kind(["db.commit()", "notify(req)"], ["notify(req)", "db.commit()"], py) is None
    assert block_kind(["if not ready:"], ["if notready:"], py) is None
    assert block_kind(["rm -rf /var/app/tmp"], ["rm -rf /var/app/ tmp"], "clean.sh") is None
    assert block_kind(["key: value"], ["key:value"], yml) is None
    assert block_kind(["rm  -rf  tmp"], ["rm -rf tmp"], "clean.sh") == "whitespace"
    assert block_kind(['    return "forbidden"'], ['    "fmt"'], "x.go") is None
    assert categorise("web/package-lock.json", {}, None)[0] == "lockfile"
    assert categorise("src/routeTree.gen.ts", {}, None)[0] == "generated"
    assert categorise("api/client.ts", {}, "// Code generated by oapi. DO NOT EDIT.\n")[0] == "generated"
    assert categorise("dist/app.js", {}, None)[0] == "generated"
    assert categorise("cmd/build/main.go", {}, None)[0] == "implementation"
    assert categorise("integrations/vendor/payments.py", {}, None)[0] == "implementation"
    assert categorise("thumbs.py", {}, '"""Auto-generated thumbnails are stored under cache/."""\n')[0] == "implementation"
    assert categorise("dist/app.js", {"linguist-generated": "unset"}, None)[0] == "implementation"
    assert categorise("e2e/login.spec.ts", {}, None)[0] == "test"
    assert categorise("pkg/upload/server_test.go", {}, None)[0] == "test"
    assert categorise("CLAUDE.md", {}, None)[0] == "agent-guidance"
    assert categorise("README.md", {}, None)[0] == "docs"
    assert categorise("src/store/auth/index.ts", {}, None)[0] == "implementation"
    assert categorise("tests/helpers.ts", {"review-test": "unset", "review-implementation": "set"}, None)[0] == "implementation"
    f = {"path": ts, "hunks": [
        {"lines": [[" ", 1, 1, "a"], ["-", 2, None, "import x"], [" ", 3, 2, "b"]]},
        {"lines": [[" ", 9, 8, "c"], ["+", None, 9, "import x"], ["+", None, 10, ""], [" ", 10, 11, "d"]]},
    ]}
    mark_blocks(f)
    assert [h["noise"] for h in f["hunks"]] == ["moved", "moved"]
    f = {"path": py, "hunks": [
        {"lines": [[" ", 1, 1, "def delete_user(req):"], ["-", 2, None, "    check_admin(req)"], [" ", 3, 2, "    drop()"]]},
        {"lines": [[" ", 9, 8, "def list_users(req):"], ["+", None, 9, "    check_admin(req)"], [" ", 10, 10, "    rows()"]]},
    ]}
    mark_blocks(f)
    assert [h["noise"] for h in f["hunks"]] == [None, None]
    unwrap = {"path": py, "hunks": [{"lines": [
        [" ", 1, 1, "def f():"], ["-", 2, None, "    if use_cache:"], ["-", 3, None, "        rows = cache()"],
        ["-", 4, None, "    else:"], ["-", 5, None, "        rows = db()"], ["+", None, 2, "    rows = cache()"],
        [" ", 6, 3, "    return rows"]]}]}
    mark_blocks(unwrap)
    mark_reindented(unwrap, ({2, 4, 5}, set()))
    assert unwrap["hunks"][0]["noise"] is None and unwrap["hunks"][0]["blocks"][0]["reindented"] == [2, 5]
    moved_out = {"path": py, "hunks": [{"lines": [
        [" ", 1, 1, "    for x in xs:"], ["-", 2, None, "        a()"], ["+", None, 2, "    a()"],
        ["+", None, 3, "    b()"]]}]}
    mark_blocks(moved_out)
    mark_reindented(moved_out, (set(), {3}))
    assert "reindented" not in moved_out["hunks"][0]["blocks"][0]
    moved_out["path"] = ts
    mark_reindented(moved_out, (set(), {3}))
    assert moved_out["hunks"][0]["blocks"][0]["reindented"] == [1, 2]
    # An unrelated opener elsewhere in the hunk does not let a statement leave its block quietly.
    sig = {"path": py, "hunks": [{"lines": [
        ["-", 1, None, "def f(a):"], ["+", None, 1, "def f(a, b):"], [" ", 2, 2, "    if a:"],
        ["-", 3, None, "        return 1"], ["+", None, 3, "    return 1"]]}]}
    mark_blocks(sig)
    mark_reindented(sig, ({1}, {1}))
    assert "reindented" not in sig["hunks"][0]["blocks"][1], sig
    joined = {"path": ts, "hunks": [{"lines": [["-", 1, None, "  if (typeof x) {"], ["+", None, 1, "if (typeofx) {"]]}]}
    mark_blocks(joined)
    mark_reindented(joined, (set(), set()))
    assert "reindented" not in joined["hunks"][0]["blocks"][0], joined
    guard = {"path": "x.go", "hunks": [
        {"lines": [[" ", 1, 1, "func deleteUser() {"], ["-", 2, None, '    return "forbidden"'], [" ", 3, 2, "    drop()"]]},
        {"lines": [[" ", 9, 8, "func listUsers() {"], ["+", None, 9, '    return "forbidden"'], [" ", 10, 10, "    rows()"]]}]}
    mark_blocks(guard)
    assert [h["noise"] for h in guard["hunks"]] == [None, None]
    gomove = {"path": "x.go", "hunks": [
        {"lines": [[" ", 1, 1, "import ("], ["-", 2, None, '    "fmt"'], [" ", 3, 2, ")"]]},
        {"lines": [[" ", 9, 8, "import ("], ["+", None, 9, '    "fmt"'], [" ", 10, 10, ")"]]}]}
    mark_blocks(gomove)
    assert [h["noise"] for h in gomove["hunks"]] == ["moved", "moved"]
    quoted = ('diff --git "a/we\\"ird.txt" "b/we\\"ird.txt"\n--- "a/we\\"ird.txt"\n+++ "b/we\\"ird.txt"\n'
              "@@ -1 +1 @@\n-x\n+y\n")
    [qf] = parse_diff(quoted)
    assert qf["path"] == 'we"ird.txt' and qf["oldPath"] == 'we"ird.txt', qf
    [bf] = parse_diff("diff --git a/my file.png b/my file.png\nBinary files a/my file.png and b/my file.png differ\n")
    assert bf["path"] == "my file.png" and bf.get("binary"), bf
    [df] = parse_diff("diff --git a/gone.ts b/gone.ts\ndeleted file mode 100644\n--- a/gone.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    assert df["path"] == "gone.ts" and df["status"] == "deleted", df
    diff = ("diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1,3 +1,3 @@ fn\n a\n--- old dashes\n+b\n c\n"
            "@@ -10 +10 @@\n-p\n+q\n")
    [pf] = parse_diff(diff)
    assert [r[0] for r in pf["hunks"][0]["lines"]] == [" ", "-", "+", " "], pf
    assert pf["hunks"][0]["lines"][1][3] == "-- old dashes"
    assert pf["hunks"][1]["lines"] == [["-", 10, None, "p"], ["+", None, 10, "q"]]
    assert defined_names(["export function resetOnWorkspaceChange(router) {"]) == {"resetOnWorkspaceChange"}
    assert defined_names(["  switchWorkspace: async workspaceId => {"]) == {"switchWorkspace"}
    assert defined_names(["const useThing = () => x", "  if (a) {", "  const token = 1"]) == {"useThing"}
    assert IDENT.findall(code_only('a.workspaceId + workspaceIds // resetTab "openMenu"')) == ["a", "workspaceIds"]
    assert defined_names(["func (s *Server) handleUpload(c net.Conn) {", "def parse_frame(buf):"]) == {"handleUpload", "parse_frame"}
    fs = [{"noise": False, "hunks": [
        {"id": "f1h1", "noise": None, "lines": [["+", None, 1, "export function resetTab(r) {"], ["+", None, 2, "const data = 1"]]},
        {"id": "f2h1", "noise": None, "lines": [["+", None, 9, "resetTab(router); use(data)"], ["+", None, 10, "const data = 2"]]},
        {"id": "f3h1", "noise": None, "lines": [["-", 4, None, "function oldFlow() {}"]]},
        {"id": "f4h1", "noise": None, "lines": [["-", 7, None, "oldFlow()"]]},
    ]}]
    connect(fs)
    h = {x["id"]: x for x in fs[0]["hunks"]}
    assert h["f2h1"]["uses"] == {"resetTab": "f1h1"}, h["f2h1"]
    assert h["f1h1"]["usedBy"] == {"f2h1": ["resetTab"]}
    assert h["f4h1"]["uses"] == {"oldFlow": "f3h1"}
    def tf(path, cat, rows, status="modified"):
        f = {"id": "f1", "path": path, "category": cat, "noise": False, "status": status,
             "hunks": [{"id": "f1h1", "lines": rows}]}
        mark_blocks(f)
        return {t["kind"]: t for t in find_tells([f])}
    t = tf("src/api.ts", "implementation", [["+", None, 1, "  console.log(user)"], ["+", None, 2, "  const x = y as any"],
                                            ["+", None, 3, "  // @ts-ignore"], ["+", None, 4, "  // TODO: handle 404"],
                                            ["+", None, 5, "  // return fetchAll(user);"]])
    assert set(t) == {"debug", "any", "silenced", "todo", "commented-code"}, t
    assert t["debug"]["line"] == "R1"
    assert tf("src/app.py", "implementation", [["+", None, 1, "x: Any = 1  # noqa: E501"], ["+", None, 2, "# return early when empty"]]).keys() == {"silenced"}
    assert tf("src/model.py", "implementation", [["+", None, 1, "model.fit(x)"], ["+", None, 2, "m = {'any': 1}"]]) == {}
    f = {"id": "f1", "path": "src/app.py", "category": "implementation", "noise": False, "status": "modified",
         "hunks": [{"id": "f1h1", "lines": [["-", 1, None, "x = 1  # noqa"], ["+", None, 1, "def f():"],
                                            ["+", None, 2, "    x = 1  # noqa"]]}]}
    mark_blocks(f)
    mark_reindented(f, (set(), {1}))
    assert find_tells([f]) == [], "a re-indented line is not a new one"
    t = tf("e2e/login.spec.ts", "test", [[" ", 1, 1, "it('logs in', async () => {"], ["-", 2, None, "  expect(res.status).toBe(200)"],
                                         ["+", None, 2, "  expect(res.status).toBe(500)"], [" ", 3, 3, "})"],
                                         ["+", None, 4, "it.skip('logs out', () => {})"], ["+", None, 5, "describe.only('x', () => {})"]])
    assert set(t) == {"assertion", "skip", "focus"} and t["assertion"]["label"] == "assertion changed", t
    t = tf("tests/test_auth.py", "test", [["-", 1, None, "def test_refresh_rejected():"], ["-", 2, None, "    assert r.status == 401"]])
    assert t["test-removed"]["line"] == "L1" and t["assertion"]["label"] == "assertion removed", t
    assert tf("tests/test_auth.py", "test", [["-", 1, None, "x"]], status="deleted")["test-removed"]["label"] == "test file deleted"
    assert categorise("spec/models/user_spec.rb", {}, None)[0] == "test"
    t = tf("spec/user_spec.rb", "test", [["+", None, 1, "  xit 'saves' do"], ["+", None, 2, "  binding.pry"],
                                         ["+", None, 3, "  fit 'loads' do"], ["+", None, 4, "  # rubocop:disable Metrics"]])
    assert set(t) == {"skip", "debug", "focus", "silenced"}, t
    t = tf("tests/api.rs", "test", [["+", None, 1, "#[ignore]"], ["-", 2, None, "#[test]"], ["-", 3, None, "fn saves() {}"]])
    assert set(t) == {"skip", "test-removed"}, t
    assert set(tf("src/test/java/AppTest.java", "test", [["+", None, 1, "  @Disabled"]])) == {"skip"}
    assert tf("tests/test_model.py", "test", [["+", None, 1, "model.fit(x)"], ["+", None, 2, "fit(x, y)"]]) == {}
    assert risk_hints("migrations/0042_add_index.py") == ["migration"]
    assert risk_hints("src/store/auth/tokenChannel.ts") == ["auth"] and risk_hints("web/package.json") == ["dependencies"]
    assert risk_hints("src/components/Button.tsx") == []
    assert ready_check({"stats": {"categories": {"implementation": {"add": 5, "del": 0}}}, "files": [], "meta": {}}, 2500)["band"] == "too big for one review"
    d = description_text({"title": "t", "url": "", "body": "", "commits": ["abc123 adds x\nwhy it matters\n"]})
    assert "(none)" in d and "- abc123 adds x" in d and "  why it matters" in d
    got, text = linked_issues("/nonexistent", "o/r", {"closingIssuesReferences": [
        {"number": 7, "url": "https://github.com/o/r/issues/7", "repository": {"name": "r", "owner": {"login": "o"}}}]},
        "PROJ-12 adds the thing for UTF-8 names", "Fixes #9 and closes other/repo#3.\nhttps://linear.app/acme/issue/ENG-5/x",
        ["abc1234 ENG-5 first pass\nbody PROJ-99 is only mentioned here"], "me/proj-12-the-thing")
    assert [x["ref"] for x in got] == ["#7", "#9", "other/repo#3", "ENG-5", "PROJ-12"], got
    assert got[0]["url"] == "https://github.com/o/r/issues/7" and got[3]["url"].startswith("https://linear.app/") and not text
    assert [x["ref"] for x in linked_issues("/nonexistent", "o/r", None, "release-2 work", "", [], "release-2")[0]] == []
    got = linked_issues("/nonexistent", "o/r", None, "bac-1555: client settings API", "", [], "bt/da-6361-client-settings")[0]
    assert [(x["ref"], x["closes"]) for x in got] == [("BAC-1555", True), ("DA-6361", True)], got
    assert same_owner("O/other", "o/r") and not same_owner("other/repo", "o/r") and not same_owner("", "o/r")
    got = {x["ref"]: x["closes"] for x in linked_issues(
        "/nonexistent", "o/r", None, "adds the thing",
        "Fixes ENG-1 and ENG-2. Part of ENG-4. Implements #5 and ENG-6. Refs #8.\nIgnore ENG-7, skip #9.\nfixes #9",
        ["abc1234 tidies ENG-7"], "me/eng-10-thing")[0]}
    assert got == {"#5": False, "#8": False, "ENG-1": True, "ENG-2": True, "ENG-4": False,
                   "ENG-6": True, "ENG-10": True}, got
    def hunk(hid, rows):
        return {"id": hid, "noise": None, "lines": rows}
    body = ["export function slugify(text) {", "  return text", '    .replace(/[^a-z0-9]+/g, "-")', "}"]
    src = {"path": "a.ts", "noise": False, "hunks": [hunk("f1h1", [["-", 10 + k, None, x] for k, x in enumerate(body)])]}
    dst = {"path": "b.ts", "noise": False, "hunks": [hunk("f2h1", [["+", None, 1, "// slugs"]] +
                                                          [["+", None, 2 + k, "  " + x] for k, x in enumerate(body)])]}
    for f in (src, dst):
        mark_blocks(f)
    find_moves([src, dst])
    assert src["hunks"][0]["moves"] == [{"side": "-", "start": 0, "end": 4, "to": "f2h1", "line": "R2"}], src
    assert dst["hunks"][0]["moves"] == [{"side": "+", "start": 1, "end": 5, "from": "f1h1", "line": "L10"}], dst
    # Python keeps the move only while the lines keep their indentation relative to each other.
    py_body = ["def f(x):", "    if x:", "        return slow(x)", "    return fast(x)"]
    src = {"path": "a.py", "noise": False, "hunks": [hunk("f1h1", [["-", 1 + k, None, x] for k, x in enumerate(py_body)])]}
    dst = {"path": "b.py", "noise": False, "hunks": [hunk("f2h1", [["+", None, 1 + k, x] for k, x in enumerate(
        py_body[:2] + ["    return slow(x)"] + py_body[3:])])]}
    for f in (src, dst):
        mark_blocks(f)
    find_moves([src, dst])
    assert dst["hunks"][0]["moves"] == [], "a statement moved out of its block is not a plain move"
    short = {"path": "a.ts", "noise": False, "hunks": [hunk("f1h1", [["-", 1, None, "return null;"], ["+", None, 9, "return null;"]])]}
    mark_blocks(short)
    find_moves([short])
    assert short["hunks"][0]["moves"] == []
    f = {"path": "a.ts", "hunks": []}
    assert hunk_sig(f, {"lines": [[" ", 1, 1, "a"], ["-", 2, None, "b"]]}) == hunk_sig(f, {"lines": [[" ", 7, 9, "z"], ["-", 8, None, "b"]]})
    assert hunk_sig(f, {"lines": [["-", 2, None, "b"]]}) != hunk_sig({"path": "c.ts"}, {"lines": [["-", 2, None, "b"]]})
    data = {"meta": {"headSha": "new"}, "files": [{"noise": False, "hunks": [{"sig": "s1", "noise": None}, {"sig": "s2", "noise": None}]}]}
    mark_since(data, [{"head": "old", "sigs": ["s1", "s3"]}, {"head": "new", "sigs": ["s1", "s2"]}])
    assert [h["fresh"] for h in data["files"][0]["hunks"]] == [False, True] and data["meta"]["lastPass"]["gone"] == 1, data
    anns = place_annotations([{"id": "f1", "path": "a.ts", "noise": False, "hunks": [
        {"id": "f1h1", "lines": [[" ", 4, 4, "a"], ["-", 5, None, "b"], ["+", None, 5, "c"]]}]}],
        [{"path": "a.ts", "line": 4, "end": 6}, {"path": "a.ts", "line": 40, "end": 40}])
    assert anns[0]["row"] == "R5" and anns[0]["hunk"] == "f1h1" and "hunk" not in anns[1], anns
    print("self-test ok")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pr", type=int)
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--repo", default=".")
    p.add_argument("--out")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.out:
        p.error("--out is required")
    a.repo = os.path.abspath(a.repo)
    data = collect(a)
    count, nbatches = write(data, a.out)
    record_pass(pass_path(a.repo, data["meta"]), data)
    s = data["stats"]
    print(f"{data['meta']['repo']} {data['meta']['title']!r}: {s['total']['files']} files, "
          f"+{s['total']['add']} -{s['total']['del']}")
    for cat, v in sorted(s["categories"].items()):
        print(f"  {cat:15} {v['files']:3} files  +{v['add']} -{v['del']}")
    print(f"  {'formatting':15}       +{s['noise']['add']} -{s['noise']['del']}")
    print(f"reviewable changed lines: {count} ({data['ready']['band']}), batches: {nbatches}")
    if s["moved"]:
        print(f"moved unchanged: {s['moved']} lines")
    hunkless = sum(1 for f in data["files"] if not f["noise"] and not f["hunks"])
    if hunkless:
        print(f"files with no line changes (binary, renamed or mode only): {hunkless}")
    last = data["meta"].get("lastPass")
    if last:
        print(f"since the page built at {last['headSha'][:7]}: {last['fresh']} hunks new or changed, {last['gone']} gone")
    if not data["meta"]["commitMap"] and len(data["meta"]["commits"]) > 1:
        print(f"commit filter skipped: more than {COMMIT_MAP_MAX_FILES} files")
    checks = data["ready"]["checks"]
    if checks:
        print(f"CI: {checks['pass']} passing, {checks['fail']} failing, {checks['cancel']} cancelled, "
              f"{checks['pending']} pending" + (", on the pushed head, not this one" if checks.get("stale") else ""))
    if data["annotations"]:
        on = sum(1 for x in data["annotations"] if x.get("hunk"))
        print(f"CI annotations: {on} on changed lines, {len(data['annotations']) - on} elsewhere")
    if data["ready"]["implChanged"] and not data["ready"]["testsChanged"]:
        print("no test changes")
    if data["tells"]:
        kinds = Counter(t["label"] for t in data["tells"])
        print("tells: " + ", ".join(f"{n} {k}" for k, n in kinds.most_common()))
    issues = data["meta"]["issues"]
    if issues:
        print("linked issues: " + ", ".join(f"{x['ref']} ({'closes' if x['closes'] else 'related'}, "
                                            f"{'in issue.md' if x['fetched'] else 'not fetched'})" for x in issues))
    print(f"wrote {a.out}/hunks.json, review.diff, description.md, batches/" + (", issue.md" if any(x["fetched"] for x in issues) else ""))


if __name__ == "__main__":
    main()
