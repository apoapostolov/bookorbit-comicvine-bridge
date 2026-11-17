#!/usr/bin/env python3
"""ComicVine Bridge for BookOrbit.

Scans a BookOrbit comics library and fills missing issue metadata from the
ComicVine API at a staggered, checkpointed rate. Each ComicVine lookup is
limited to a single issue whose series and issue number can be parsed from
the book's title; compilations are skipped by design.

State is written to a JSON file after every book so the service can be
stopped and resumed without repeating lookups.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

VERSION = "1.0.0"

CV_SEARCH_URL = "https://comicvine.gamespot.com/api/search/"
CV_FIELDS = ",".join([
    "id", "name", "issue_number", "volume", "cover_date",
    "description", "image", "resource_type",
])

DEFAULT_BOOKORBIT_URL = "http://192.168.1.217:8123/api/v1"
DEFAULT_STATE = "state.json"

# Multi-issue and compilation markers: these cannot be matched to one issue.
COMPILATION_RE = re.compile(
    r"(?:\bv\d?\s*\d*\s*[-–]\s*\d+|\b\d+\s*[-–]\s*\d+\b|\btpb\b|\bomnibus\b|"
    r"\bcollection\b|\btrade\b|\bvol\.?\s*\d+\s*\(|\bannual\b|\bhc\b|\bgn\b)",
    re.IGNORECASE,
)
HASH_ISSUE_RE = re.compile(r"#\s*0*(\d{1,4})(?:[a-z])?\b", re.IGNORECASE)
BARE_ISSUE_RE = re.compile(r"(?:^|\s)0*(\d{1,4})(?:[a-z])?(?=\s|\)|$|\.(?:\s|$))", re.IGNORECASE)
YEAR_RE = re.compile(r"\((19|20)\d{2}\)")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = (clean.replace("&nbsp;", " ").replace("&amp;", "&")
             .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", clean).strip() or None


def normalize_series(name: str) -> str:
    """Fold a series name so 'The Boys (2019)' matches ComicVine 'The Boys'."""
    s = YEAR_RE.sub("", name or "")
    s = re.sub(r"^(the)\s+", "", s.strip(), flags=re.IGNORECASE)
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    return s


def parse_issue(title: str) -> tuple[str, str] | None:
    """Return (series, issue) when a title resolves to exactly one issue."""
    if not title or COMPILATION_RE.search(title):
        return None
    hashed = HASH_ISSUE_RE.findall(title)
    if hashed:
        issue = hashed[-1]
        series = HASH_ISSUE_RE.sub("", title)
        # A zero-padded leading sort ordinal ('001 Series #01') is not the name.
        series = re.sub(r"^\s*0\d{2,}\s+(?=[A-Za-z])", "", series)
    else:
        bare = BARE_ISSUE_RE.findall(title)
        if len(bare) != 1:
            return None
        issue = bare[0]
        series = BARE_ISSUE_RE.sub("", title)
    series = re.sub(r"\s*\(\d{4}\s*-\s*\d{4}\)", "", series)
    series = re.sub(r"\s*\(\d{4}\)", "", series)
    series = re.sub(r"\.(cbz|cbr|cb7)$", "", series, flags=re.IGNORECASE)
    series = re.sub(r"[\s\-–]+$", "", series).strip(" -–#")
    if len(series) < 2:
        return None
    return series, issue


@dataclass
class RateLimiter:
    """Sliding one-hour window, 200 ComicVine requests, with a hard wait."""

    limit: int = 200
    window: int = 3600
    stamps: list[float] = field(default_factory=list)

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        self.stamps = [t for t in self.stamps if t > cutoff]

    def wait_time(self, now: float) -> float:
        self.prune(now)
        if len(self.stamps) < self.limit:
            return 0.0
        return max(0.0, self.stamps[0] + self.window - now) + 1.0

    def record(self, now: float | None = None) -> None:
        self.stamps.append(now or time.time())

    def to_state(self) -> list[float]:
        return self.stamps

    @classmethod
    def from_state(cls, stamps: list[float], limit: int, window: int) -> "RateLimiter":
        obj = cls(limit=limit, window=window, stamps=list(stamps))
        obj.prune(time.time())
        return obj


class BookOrbit:
    def __init__(self, base: str, key: str, timeout: int = 60):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"X-Api-Key": self.key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None

    def books(self, library_id: int, page_size: int = 200) -> Iterator[dict]:
        page = 0
        while True:
            body = {"pagination": {"page": page, "size": page_size}}
            result = self.request("POST", "/books/query", body)
            items = result.get("items", [])
            for item in items:
                if item.get("libraryId") == library_id:
                    yield item
            if len(items) < page_size:
                break
            page += 1

    def patch_metadata(self, book_id: int, patch: dict, sync_file_write: bool = False) -> None:
        suffix = "?syncFileWrite=true" if sync_file_write else "?syncFileWrite=false"
        self.request("PATCH", f"/books/{book_id}/metadata{suffix}", patch)


class ComicVine:
    def __init__(self, key: str, limiter: RateLimiter, timeout: int = 30):
        self.key = key
        self.limiter = limiter
        self.timeout = timeout
        self.calls = 0

    def search(self, series: str, issue: str) -> list[dict]:
        wait = self.limiter.wait_time(time.time())
        if wait > 0:
            log(f"rate window full; waiting {int(wait)}s before the next lookup")
            time.sleep(wait)
        params = urllib.parse.urlencode({
            "api_key": self.key,
            "format": "json",
            "resources": "issue",
            "query": f"{series} {issue}",
            "field_list": CV_FIELDS,
            "limit": "10",
        })
        req = urllib.request.Request(
            CV_SEARCH_URL + "?" + params,
            headers={"User-Agent": "ComicVineBridge/1.0", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            log(f"ComicVine HTTP {exc.code} for '{series} {issue}'")
            self.limiter.record()
            self.calls += 1
            return []
        self.limiter.record()
        self.calls += 1
        if payload.get("status_code") != 1:
            log(f"ComicVine error {payload.get('status_code')} for '{series} {issue}'")
            return []
        return payload.get("results", []) or []


def best_match(results: list[dict], series: str, issue: str) -> dict | None:
    """Accept only an exact series+issue match; skip ambiguous compilations."""
    target = normalize_series(series)
    candidates = []
    for result in results:
        if str(result.get("issue_number", "")).strip().lstrip("0") != issue.lstrip("0"):
            continue
        volume = result.get("volume") or {}
        if normalize_series(str(volume.get("name", ""))) != target:
            continue
        candidates.append(result)
    if len(candidates) != 1:
        return None
    chosen = candidates[0]
    if not (chosen.get("id") and (chosen.get("volume") or {}).get("id")):
        return None
    return chosen


def build_patch(match: dict) -> dict:
    """Fill only fields ComicVine can supply; never touch series or language."""
    volume = match.get("volume") or {}
    cover_date = match.get("cover_date")
    patch: dict[str, Any] = {
        "comicvineId": f"4000-{match['id']}",
        "comicMetadata": {
            "issueNumber": str(match.get("issue_number", "")),
            "volumeName": volume.get("name"),
        },
    }
    description = strip_html(match.get("description"))
    if description:
        patch["description"] = description
    if isinstance(cover_date, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", cover_date):
        patch["publishedDate"] = cover_date
        patch["publishedYear"] = int(cover_date[:4])
    return patch


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "processed": {}, "stats": {"matched": 0, "no_match": 0,
                "skipped": 0, "error": 0}, "calls": [], "startedAt": None, "finishedAt": None}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, separators=(",", ":"))
    tmp.replace(path)


def read_secret(value: str | None, file_value: str | None) -> str:
    if value:
        return value
    if file_value:
        return Path(file_value).read_text(encoding="utf-8").strip()
    raise SystemExit("A required credential was not provided")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"ComicVine Bridge {VERSION}")
    parser.add_argument("--bookorbit-url", default=os.environ.get("BOOKORBIT_URL", DEFAULT_BOOKORBIT_URL))
    parser.add_argument("--bookorbit-key")
    parser.add_argument("--bookorbit-key-file")
    parser.add_argument("--comicvine-key")
    parser.add_argument("--comicvine-key-file")
    parser.add_argument("--library-id", type=int, default=int(os.environ.get("LIBRARY_ID", "2")))
    parser.add_argument("--state", default=os.environ.get("STATE_FILE", DEFAULT_STATE))
    parser.add_argument("--limit", type=int, default=0, help="Stop after N newly processed books (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Query ComicVine but do not write to BookOrbit.")
    parser.add_argument("--rate-limit", type=int, default=int(os.environ.get("CV_RATE_LIMIT", "200")))
    parser.add_argument("--window", type=int, default=3600)
    args = parser.parse_args()

    bookorbit_key = read_secret(args.bookorbit_key or os.environ.get("BOOKORBIT_API_KEY"), args.bookorbit_key_file)
    comicvine_key = read_secret(args.comicvine_key or os.environ.get("COMICVINE_API_KEY"), args.comicvine_key_file)
    state_path = Path(args.state).expanduser()

    state = load_state(state_path)
    if not state.get("startedAt"):
        state["startedAt"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)

    limiter = RateLimiter.from_state(state.get("calls", []), args.rate_limit, args.window)
    bo = BookOrbit(args.bookorbit_url, bookorbit_key)
    cv = ComicVine(comicvine_key, limiter)

    log(f"starting library {args.library_id}: {len(state['processed'])} books already processed")
    processed_this_run = 0

    for book in bo.books(args.library_id):
        book_id = str(book.get("id"))
        if not book_id or book_id in state["processed"]:
            continue

        parsed = parse_issue(book.get("title", ""))
        if not parsed:
            state["processed"][book_id] = {"status": "skipped"}
            state["stats"]["skipped"] += 1
            save_state(state_path, state)
            continue

        series, issue = parsed
        results = cv.search(series, issue)
        match = best_match(results, series, issue)
        if not match:
            state["processed"][book_id] = {"status": "no_match", "series": series, "issue": issue}
            state["stats"]["no_match"] += 1
            log(f"no match: {book.get('title')}")
        else:
            patch = build_patch(match)
            if args.dry_run:
                log(f"DRY RUN would patch {book_id}: {book.get('title')} -> {patch.get('comicvineId')}")
            else:
                try:
                    bo.patch_metadata(int(book_id), patch)
                    state["stats"]["matched"] += 1
                    log(f"patched {book_id}: {book.get('title')} -> {patch.get('comicvineId')}")
                except Exception as exc:  # noqa: BLE001 - keep the service alive on one bad book
                    state["stats"]["error"] += 1
                    log(f"write error {book_id}: {type(exc).__name__}: {exc}")
                    state["processed"][book_id] = {"status": "error"}
                    save_state(state_path, state)
                    continue
            state["processed"][book_id] = {
                "status": "matched", "comicvineId": patch.get("comicvineId"),
                "series": series, "issue": issue,
            }
        state["calls"] = cv.limiter.to_state()
        save_state(state_path, state)

        processed_this_run += 1
        if args.limit and processed_this_run >= args.limit:
            log(f"limit of {args.limit} reached this run; stopping")
            break

    state["finishedAt"] = datetime.now(timezone.utc).isoformat()
    save_state(state_path, state)
    log(f"complete: {json.dumps(state['stats'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
