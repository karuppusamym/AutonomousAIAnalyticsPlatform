"""Read-only knowledge sources: a GitHub repository path and a Confluence space as untrusted drafts (P2-10).

A source is configuration only (`kind`, a location filter, a `secret_ref`); this module fetches, screens and
renders. It never writes to the pack (`services/knowledge_sources.py` does, through `knowledge/drafts`) and
never decides anything: every file is data, never instructions (CLAUDE.md rule 7).

* **Fetch** goes through `tools/http.call` only: https, host on the allowlist, every resolved address public,
  connection pinned to the vetted address, no redirects, byte caps, timeouts. The allowlist is the built-in API
  hosts (`api.github.com`, `*.atlassian.net`) plus the operator's `http_tool_allowlist`, so a workspace editor
  cannot point a credential at a host of their choosing. GitHub Enterprise or Confluence Data Center need the
  operator to list the host.
* **Credentials** are a `secret_ref` (`connectors/secrets.py`), resolved when a sync starts, sent in a header,
  never stored, logged or returned. A config that carries a token field is refused.
* **Screening** is `knowledge/documents.screen` for every file: credentials, e-mail addresses and card-like
  numbers redacted, instruction-like lines replaced by a marker, control characters removed, frontmatter
  dropped, bundle-relative links reduced to text. SQL and YAML are shown inside a code fence.
* **Caps**: files per sync, bytes per file, bytes per sync, Confluence pages and result pages, request timeout.
* **Incremental**: `state` maps each remote path to the remote revision (git blob sha, Confluence version) and
  the sha256 of the fetched bytes. An unchanged revision is not fetched; unchanged bytes are not re-rendered.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from analystos.core.errors import InvalidInput
from analystos.knowledge import documents, okf
from analystos.knowledge.crawl_docs import safe_segment

SOURCE_ACTOR = "process:analystos-knowledge-source"
KINDS = ("github", "confluence")
DEFAULT_HOSTS = ("api.github.com", "*.atlassian.net")
GITHUB_API = "https://api.github.com"
MAX_FILES = 200            # files (or pages) taken per sync
MAX_FILE_BYTES = 256 * 1024
MAX_SYNC_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_PAGES_PER_REQUEST = 50
REQUEST_TIMEOUT = 20.0
MAX_REQUESTS = 600         # a sync that needs more is cut, not left running
TEXT_EXTENSIONS = {".md": "markdown", ".markdown": "markdown", ".txt": "markdown", ".sql": "sql"}
DBT_YAML = re.compile(r"^(schema|sources|models|properties)\.ya?ml$|__(models|sources)\.ya?ml$", re.I)
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,199}$")
_SPACE = re.compile(r"^[A-Za-z0-9_~-]{1,100}$")
_FORBIDDEN_CONFIG = ("token", "password", "secret", "api_key", "apikey", "authorization", "credentials")
Fetch = Callable[..., dict[str, Any]]


@dataclass
class Item:
    """One remote file or page, fetched and ready to render."""

    path: str            # path in the repo / "<id> <title>" for a page
    rev: str             # git blob sha / Confluence version number
    kind: str            # markdown | sql | yaml | html
    data: bytes
    title: str | None = None
    url: str | None = None


@dataclass
class SyncPlan:
    items: list[Item] = field(default_factory=list)
    skipped_unchanged: int = 0
    skipped_too_large: list[str] = field(default_factory=list)
    skipped_type: int = 0
    removed: list[str] = field(default_factory=list)
    truncated: bool = False
    bytes: int = 0
    requests: int = 0
    state: dict[str, dict[str, str]] = field(default_factory=dict)


# ------------------------------------------------------------------------------------ configuration
def validate_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The normalised config, or InvalidInput. No credential may appear here: `secret_ref` is a separate field."""
    if kind not in KINDS:
        raise InvalidInput(f"knowledge source kind must be one of {list(KINDS)}")
    if not isinstance(config, dict):
        raise InvalidInput("config must be an object")
    for key in config:
        if any(w in key.lower() for w in _FORBIDDEN_CONFIG):
            raise InvalidInput(f"config.{key}: credentials are never stored in the configuration; use secret_ref")
    if kind == "github":
        repo = str(config.get("repo") or "").strip()
        if not _REPO.match(repo) or ".." in repo:
            raise InvalidInput("config.repo must look like owner/name")
        branch = str(config.get("branch") or "main").strip()
        if not _REF.match(branch) or ".." in branch or branch.endswith(("/", ".lock")) or "//" in branch:
            raise InvalidInput("config.branch is not a valid branch name")
        prefix = _prefix(config.get("path"))
        base = _base_url(config.get("api_url") or GITHUB_API)
        return {"repo": repo, "branch": branch, "path": prefix, "api_url": base}
    base = _base_url(config.get("base_url"))
    space = str(config.get("space") or "").strip()
    if not _SPACE.match(space):
        raise InvalidInput("config.space must be a Confluence space key")
    if str(config.get("auth") or "basic") not in ("basic", "bearer"):
        raise InvalidInput("config.auth must be basic (the secret is user:api_token) or bearer")
    return {"base_url": base, "space": space, "auth": str(config.get("auth") or "basic")}


def _prefix(value: Any) -> str:
    p = str(value or "").strip().strip("/")
    if not p:
        return ""
    if "\\" in p or any(seg in ("", ".", "..") for seg in p.split("/")) or re.search(r"[\x00-\x1f]", p):
        raise InvalidInput("config.path must be a plain relative path inside the repository")
    return p


def _base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise InvalidInput("the base URL must be an https URL without credentials, query or fragment")
    return raw


def allowlist(extra: str | list[str] | None = None) -> list[str]:
    from analystos.tools.http import split_hosts

    return [*DEFAULT_HOSTS, *split_hosts(extra)]


def source_host(kind: str, config: dict[str, Any]) -> str:
    return urlsplit(config["api_url"] if kind == "github" else config["base_url"]).hostname or ""


# ------------------------------------------------------------------------------------ HTTP
def make_fetch(*, secret: str | None, auth: str, settings: Any, resolver: Any = None, transport: Any = None) -> Fetch:
    """`fetch(url, params)` -> parsed JSON through the SSRF-safe client, with the credential in a header."""
    from analystos.tools import http

    headers = {"Accept": "application/json", "User-Agent": "analystos-knowledge-source"}
    if secret:
        if auth == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        else:
            headers["Authorization"] = f"Bearer {secret}"
    hosts = allowlist(getattr(settings, "http_tool_allowlist", ""))
    private = http.split_hosts(getattr(settings, "outbound_private_hosts", ""))
    internal = bool(getattr(settings, "air_gapped", False))

    def fetch(url: str, params: dict[str, Any] | None = None, max_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
        out = http.call(url, method="GET", parameters=params, allowlist=hosts, private_hosts=private, timeout=REQUEST_TIMEOUT,
                        max_bytes=max_bytes, headers=headers, resolver=resolver, transport=transport, internal_only=internal)
        if not isinstance(out["content"], dict | list):
            raise InvalidInput("the remote answered with something other than JSON")
        return out["content"]

    return fetch


# ------------------------------------------------------------------------------------ GitHub
def plan_github(config: dict[str, Any], fetch: Fetch, state: dict[str, dict[str, str]]) -> SyncPlan:
    plan = SyncPlan()
    repo, branch, prefix, api = config["repo"], config["branch"], config["path"], config["api_url"]
    tree = fetch(f"{api}/repos/{repo}/git/trees/{quote(branch, safe='/')}", {"recursive": "1"}, MAX_TREE_BYTES)
    plan.requests += 1
    entries = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(entries, list):
        raise InvalidInput("the repository tree could not be read")
    plan.truncated = bool(tree.get("truncated"))
    wanted: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "blob" or not isinstance(e.get("path"), str):
            continue
        path = e["path"]
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        if _kind_for(path) is None:
            plan.skipped_type += 1
            continue
        wanted.append(e)
    seen = {e["path"] for e in wanted}
    plan.removed = sorted(set(state) - seen)
    total = 0
    for e in sorted(wanted, key=lambda x: x["path"]):
        path, sha = e["path"], str(e.get("sha") or "")
        if len(plan.items) >= MAX_FILES or plan.requests >= MAX_REQUESTS:
            plan.truncated = True
            if path in state:
                plan.state[path] = state[path]
            continue
        if int(e.get("size") or 0) > MAX_FILE_BYTES:
            plan.skipped_too_large.append(path)
            continue
        old = state.get(path)
        if old and old.get("rev") == sha:
            plan.skipped_unchanged += 1
            plan.state[path] = old
            continue
        blob = fetch(f"{api}/repos/{repo}/git/blobs/{quote(sha, safe='')}", None, MAX_FILE_BYTES * 2)
        plan.requests += 1
        data = _blob_bytes(blob)
        if len(data) > MAX_FILE_BYTES:
            plan.skipped_too_large.append(path)
            continue
        if total + len(data) > MAX_SYNC_BYTES:
            plan.truncated = True
            continue
        total += len(data)
        digest = hashlib.sha256(data).hexdigest()
        if old and old.get("sha256") == digest:
            plan.skipped_unchanged += 1
            plan.state[path] = {"rev": sha, "sha256": digest}
            continue
        plan.items.append(Item(path=path, rev=sha, kind=_kind_for(path) or "markdown", data=data,
                               url=f"https://github.com/{repo}/blob/{quote(branch, safe='/')}/{quote(path)}"))
        plan.state[path] = {"rev": sha, "sha256": digest}
    plan.bytes = total
    return plan


def _kind_for(path: str) -> str | None:
    name = PurePosixPath(path).name
    ext = PurePosixPath(path.lower()).suffix
    if ext in TEXT_EXTENSIONS:
        return TEXT_EXTENSIONS[ext]
    if DBT_YAML.search(name):
        return "yaml"
    return None


def _blob_bytes(blob: Any) -> bytes:
    if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
        raise InvalidInput("a repository file could not be read")
    try:
        return base64.b64decode(blob["content"], validate=False)
    except ValueError:
        raise InvalidInput("a repository file is not valid base64") from None


# ------------------------------------------------------------------------------------ Confluence
class _Text(HTMLParser):
    """Confluence storage format (XHTML + `ac:` macros) to plain Markdown-ish text. Tags are never kept."""

    BLOCK = {"p", "div", "br", "tr", "table", "ul", "ol", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def storage_to_text(storage: str) -> str:
    p = _Text()
    p.feed(storage)
    p.close()
    text = html.unescape("".join(p.out))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text)).strip()


def plan_confluence(config: dict[str, Any], fetch: Fetch, state: dict[str, dict[str, str]]) -> SyncPlan:
    plan = SyncPlan()
    base, space = config["base_url"], config["space"]
    start, total = 0, 0
    live: set[str] = set()
    while True:
        page = fetch(f"{base}/rest/api/content", {"spaceKey": space, "type": "page", "expand": "body.storage,version",
                                                  "limit": MAX_PAGES_PER_REQUEST, "start": start})
        plan.requests += 1
        results = page.get("results") if isinstance(page, dict) else None
        if not isinstance(results, list):
            raise InvalidInput("the Confluence space could not be read")
        for r in results:
            if not isinstance(r, dict) or not str(r.get("id") or "").isdigit():
                continue
            pid = str(r["id"])
            key = f"{pid}"
            live.add(key)
            if len(plan.items) >= MAX_FILES:
                plan.truncated = True
                if key in state:
                    plan.state[key] = state[key]
                continue
            rev = str((r.get("version") or {}).get("number") or "")
            old = state.get(key)
            body = ((r.get("body") or {}).get("storage") or {}).get("value")
            if not isinstance(body, str):
                continue
            data = body.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                plan.skipped_too_large.append(key)
                continue
            if total + len(data) > MAX_SYNC_BYTES:
                plan.truncated = True
                continue
            total += len(data)
            digest = hashlib.sha256(data).hexdigest()
            if old and old.get("sha256") == digest:
                plan.skipped_unchanged += 1
                plan.state[key] = {"rev": rev, "sha256": digest}
                continue
            webui = str(((r.get("_links") or {}).get("webui")) or "")
            plan.items.append(Item(path=key, rev=rev, kind="html", data=data, title=str(r.get("title") or "")[:200] or None,
                                   url=f"{base}{webui}" if webui.startswith("/") else None))
            plan.state[key] = {"rev": rev, "sha256": digest}
        nxt = (page.get("_links") or {}).get("next") if isinstance(page, dict) else None
        if not nxt or not results or plan.truncated or plan.requests >= MAX_REQUESTS:
            plan.truncated = plan.truncated or bool(nxt)
            break
        start += len(results)
    plan.removed = sorted(set(state) - live) if not plan.truncated else []
    plan.bytes = total
    return plan


# ------------------------------------------------------------------------------------ drafts
def _fence(text: str, lang: str) -> str:
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{lang}\n{text.strip()}\n{ticks}"


def render_item(source_id: str, source_name: str, kind: str, config: dict[str, Any], item: Item) -> dict[str, str]:
    """{pack path: OKF draft} for one fetched file, screened exactly as an upload is."""
    if item.kind == "html":
        text = storage_to_text(item.data.decode("utf-8", errors="replace"))
        title = item.title or "Confluence page"
        body = f"# {title}\n\n{text}"
        stem = f"page-{item.path}-{safe_segment(title, 60)}"
        resource = f"confluence:{config['space']}/{item.path}"
    else:
        try:
            text = item.data.decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError:
            raise InvalidInput(f"{item.path} is not UTF-8 text") from None
        name = PurePosixPath(item.path).name
        title = documents._title(name, text, "text/markdown") if item.kind == "markdown" else item.path[:200]
        body = documents._markdown_body(text, title) if item.kind == "markdown" else \
            f"# {title}\n\n{_fence(text, 'sql' if item.kind == 'sql' else 'yaml')}"
        stem = safe_segment(item.path.replace("/", "--"), 100)
        resource = f"github:{config['repo']}@{config['branch']}:{item.path}"
    body, screening = documents.screen(body)
    sha = hashlib.sha256(item.data).hexdigest()
    chunks = documents._parts(body)
    base = f"sources/{kind}/{safe_segment(source_id, 40)}"
    docs: dict[str, str] = {}
    for i, chunk in enumerate(chunks, start=1):
        path = f"{base}/{stem}.md" if i == 1 else f"{base}/{stem}.part-{i}.md"
        fm: dict[str, Any] = {
            "type": "Document", "title": title if len(chunks) == 1 else f"{title} (part {i} of {len(chunks)})",
            "status": "draft", "tags": ["document", kind], "generated": {"by": SOURCE_ACTOR},
            "sources": [{"id": source_id, "resource": resource, "title": source_name[:200], "author": SOURCE_ACTOR}],
            "analystos": {"kind": "document", "origin": kind, "trusted": False, "source_id": source_id, "sha256": sha,
                          "remote_rev": item.rev, "bytes": len(item.data), "part": i, "parts": len(chunks),
                          "redactions": screening.redactions, "instruction_lines_removed": screening.instruction_lines}}
        if item.url:
            fm["sources"][0]["url"] = item.url
        docs[path] = okf.render_document(fm, chunk)
    return docs


def build_drafts(source_id: str, source_name: str, kind: str, config: dict[str, Any],
                 items: list[Item]) -> tuple[dict[str, str], list[dict[str, str]]]:
    """(documents, per-file failures). A file that cannot be screened is reported, never half-written."""
    docs: dict[str, str] = {}
    failed: list[dict[str, str]] = []
    for item in items:
        try:
            docs.update(render_item(source_id, source_name, kind, config, item))
        except InvalidInput as exc:
            failed.append({"path": item.path, "error": exc.message})
    return docs, failed


def plan(kind: str, config: dict[str, Any], fetch: Fetch, state: dict[str, dict[str, str]]) -> SyncPlan:
    return (plan_github if kind == "github" else plan_confluence)(config, fetch, state)
