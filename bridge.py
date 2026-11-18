#!/usr/bin/env python3
"""ComicVine Bridge for BookOrbit.

Scans a BookOrbit comics library and fills missing issue metadata from the
ComicVine API at a staggered, checkpointed rate.

Lookups go through ComicVine's structured filters, not its fuzzy issue search:
a series name resolves to a volume once, that volume's whole issue list is
fetched once and cached, and every book in the series is then matched against
it locally. The old approach queried `/search/?resources=issue&query=<series>
<issue>`, which answers with ten relevance-ranked issues drawn from any volume,
so the right issue was frequently absent from the page and the book was
reported as "no match" although ComicVine holds it.

Where a series name matches several volumes, the book's own publication year
decides which run it belongs to. With no year and no single candidate the book
is left unmatched rather than guessed at.

State is written to a JSON file after every book so the service can be stopped
and resumed without repeating lookups. Volume candidates and issue lists are
cached beside the state file and never expire.
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

VERSION = "1.1.0"

CV_BASE = "https://comicvine.gamespot.com/api/"
CV_VOLUME_FIELDS = "id,name,start_year,count_of_issues,publisher"
CV_ISSUE_FIELDS = "id,issue_number,name,volume,cover_date,description"

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
TRAILING_YEAR_RE = re.compile(r"^(?P<base>.*?)\s+(?P<year>(?:19|20)\d{2})$")
VOLUME_SUFFIX_RE = re.compile(r"\s+(?:v|vol\.?|volume)\s*\d+$", re.IGNORECASE)


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


def normalize_issue(number: Any) -> str:
    """Fold an issue number to a comparable token: '002', '2' and ' 2 ' agree."""
    text = str(number or "").strip().lower()
    text = text.replace("½", "0.5").replace("¼", "0.25").replace("¾", "0.75")
    match = re.match(r"^0*(\d+(?:\.\d+)?)(.*)$", text)
    if match:
        return match.group(1) + match.group(2)
    return text


def split_series_year(series: str) -> tuple[str, int | None]:
    """'Swamp Thing 1971' -> ('Swamp Thing', 1971); a bare name keeps its year of None."""
    match = TRAILING_YEAR_RE.match(series.strip())
    if not match:
        return series, None
    base = match.group("base").strip()
    if len(base) < 2:
        return series, None
    return base, int(match.group("year"))


def series_tokens(series: str) -> list[str]:
    """Progressively plainer names to try against ComicVine's name filter."""
    base, _ = split_series_year(series)
    base = VOLUME_SUFFIX_RE.sub("", base).strip() or base
    tokens = [series]
    for token in (base,):
        if token and token not in tokens:
            tokens.append(token)
    for separator in (" - ", ": "):
        if separator in base:
            head = base.split(separator, 1)[0].strip()
            if len(head) >= 3 and head not in tokens:
                tokens.append(head)
    return tokens


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
    """Structured lookups: name -> volumes, volume -> every issue, matched locally."""

    def __init__(self, key: str, limiter: RateLimiter, cache_dir: Path, timeout: int = 30):
        self.key = key
        self.limiter = limiter
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.calls = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.volumes_path = self.cache_dir / "volumes.json"
        self.failed = False
        self.volumes: dict[str, list[dict]] = self._load_json(self.volumes_path) or {}

    def _load_json(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def _save_json(self, path: Path, value: Any) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
        tmp.replace(path)

    def _get(self, resource: str, params: dict) -> dict | None:
        wait = self.limiter.wait_time(time.time())
        if wait > 0:
            log(f"rate window full; waiting {int(wait)}s before the next lookup")
            time.sleep(wait)
        query = dict(params, api_key=self.key, format="json")
        req = urllib.request.Request(
            CV_BASE + resource + "?" + urllib.parse.urlencode(query),
            headers={"User-Agent": f"ComicVineBridge/{VERSION}", "Accept": "application/json"},
        )
        self.failed = False
        payload = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                self.calls += 1
                if exc.code == 420 and attempt < 2:
                    # The key's hourly budget is spent. Wait it out rather than recording the
                    # books of this series as "no match", which no later run would revisit.
                    pause = 120 * (attempt + 1)
                    log(f"ComicVine rate limit reached ({attempt + 1}/3); pausing {pause}s")
                    self.failed = True
                    time.sleep(pause)
                    continue
                self.failed = True
                self.limiter.record()
                log(f"ComicVine HTTP {exc.code} for {resource} {params.get('filter') or params.get('query')}")
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                # A DNS blip must not kill a run with hours of work ahead of it, and must not be
                # cached as if ComicVine had answered with nothing.
                self.failed = True
                self.calls += 1
                log(f"ComicVine unreachable for {resource}: {exc}")
                time.sleep(30)
                return None
        if payload is None:
            return None
        self.limiter.record()
        self.calls += 1
        if payload.get("status_code") != 1:
            self.failed = True
            log(f"ComicVine error {payload.get('status_code')} for {resource} {params.get('filter')}")
            return None
        self.failed = False
        return payload

    def resolve_volumes(self, series: str) -> list[dict]:
        """Every volume whose name is plausibly this series; cached per series token."""
        if series in self.volumes:
            return self.volumes[series]
        base, _ = split_series_year(series)
        want_full = normalize_series(series)
        want_base = normalize_series(VOLUME_SUFFIX_RE.sub("", base).strip() or base)
        found: list[dict] = []
        for token in series_tokens(series):
            payload = self._get("volumes/", {
                "filter": f"name:{token}",
                "field_list": CV_VOLUME_FIELDS,
                "limit": "100",
            })
            volumes = (payload or {}).get("results", []) or []
            if not volumes:
                continue
            exact = [v for v in volumes
                     if normalize_series(str(v.get("name", ""))) in (want_full, want_base)]
            if exact:
                # A run can sit under an author or imprint prefix, as "Garth Ennis' 303" does
                # beside a one-issue "303", so looser matches join the list and the issue
                # coverage in score_volumes decides between them.
                looser = [v for v in volumes
                          if normalize_series(str(v.get("name", ""))).endswith(want_base)
                          or normalize_series(str(v.get("name", ""))).startswith(want_base)]
                found = exact + [v for v in looser if v["id"] not in {e["id"] for e in exact}]
                break
            prefix = [v for v in volumes
                      if normalize_series(str(v.get("name", ""))).startswith(want_base)]
            if len(prefix) == 1:
                found = prefix
                break
        if not found:
            payload = self._get("search/", {
                "resources": "volume",
                "query": series,
                "field_list": CV_VOLUME_FIELDS,
                "limit": "20",
            })
            volumes = (payload or {}).get("results", []) or []
            exact = [v for v in volumes
                     if normalize_series(str(v.get("name", ""))) in (want_full, want_base)]
            if len(exact) == 1:
                found = exact
        if found or not self.failed:
            self.volumes[series] = found
            self._save_json(self.volumes_path, self.volumes)
        if found:
            described = ", ".join(
                "{} {} ({})".format(v.get("id"), v.get("name"), v.get("start_year")) for v in found
            )
            log(f"series '{series}' -> {described}")
        return found

    def score_volumes(self, candidates: list[dict], wanted: set[str], limit: int = 4) -> list[tuple[int, dict, list[dict]]]:
        """Rank the likeliest runs by how many of this series' issue numbers each one holds.

        A name such as "Action Comics" maps to a dozen volumes; only the ones that actually
        contain the numbers in the library can be the right run, so the issue lists decide.
        At most `limit` candidates are fetched, largest first, to bound the request cost.
        """
        ranked = sorted(candidates, key=lambda v: -(v.get("count_of_issues") or 0))[:limit]
        scored: list[tuple[int, dict, list[dict]]] = []
        for volume in ranked:
            issues = self.volume_issues(volume)
            have = {normalize_issue(r.get("issue_number")) for r in issues}
            scored.append((len(wanted & have), volume, issues))
        scored.sort(key=lambda t: (-t[0], -(t[1].get("count_of_issues") or 0)))
        return scored

    def volume_issues(self, volume: dict) -> list[dict]:
        """Every issue of a volume, cached on disk; a handful of requests per series, once."""
        path = self.cache_dir / f"issues-{volume['id']}.json"
        cached = self._load_json(path)
        if cached is not None:
            return cached
        collected: list[dict] = []
        offset = 0
        complete = True
        while True:
            payload = self._get("issues/", {
                "filter": f"volume:{volume['id']}",
                "field_list": CV_ISSUE_FIELDS,
                "limit": "100",
                "offset": str(offset),
            })
            if payload is None:
                complete = False
                break
            results = payload.get("results", []) or []
            collected.extend(results)
            if len(results) < 100:
                break
            offset += 100
        # A half-fetched list must never be cached: the cached copy is what later runs trust.
        if collected and complete:
            self._save_json(path, collected)
        log(f"volume {volume['id']}: {len(collected)} issues cached")
        return collected


def pick_issue(issues: list[dict], issue: str) -> dict | None:
    """Match an issue number against a volume's issue list, earliest printing first."""
    want = normalize_issue(issue)
    hits = [r for r in issues if normalize_issue(r.get("issue_number")) == want]
    if not hits:
        return None
    hits.sort(key=lambda r: str(r.get("cover_date") or "9999-99-99"))
    return hits[0]


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
    parser.add_argument("--retry-no-match", action="store_true",
                        help="Forget previous 'no match' results so they are looked up again.")
    parser.add_argument("--rate-limit", type=int, default=int(os.environ.get("CV_RATE_LIMIT", "200")))
    parser.add_argument("--window", type=int, default=3600)
    args = parser.parse_args()

    bookorbit_key = read_secret(args.bookorbit_key or os.environ.get("BOOKORBIT_API_KEY"), args.bookorbit_key_file)
    comicvine_key = read_secret(args.comicvine_key or os.environ.get("COMICVINE_API_KEY"), args.comicvine_key_file)

    state_path = Path(args.state).expanduser()
    cache_dir = state_path.parent / "cv-cache"

    state = load_state(state_path)
    if args.retry_no_match:
        stale = [k for k, v in state["processed"].items() if v.get("status") == "no_match"]
        for key in stale:
            state["processed"].pop(key, None)
        log(f"forgot {len(stale)} previous 'no match' results")
        save_state(state_path, state)
    if not state.get("startedAt"):
        state["startedAt"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)
    state.setdefault("stats", {"matched": 0, "no_match": 0, "skipped": 0, "error": 0})

    limiter = RateLimiter.from_state(state.get("calls", []), args.rate_limit, args.window)
    bo = BookOrbit(args.bookorbit_url, bookorbit_key)
    cv = ComicVine(comicvine_key, limiter, cache_dir)

    log(f"starting library {args.library_id}: {len(state['processed'])} books already processed")

    pending: dict[str, list[dict]] = {}
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
        year = book.get("publishedYear")
        pending.setdefault(series, []).append({
            "id": book_id, "title": book.get("title", ""), "issue": issue,
            "year": year if isinstance(year, int) else None,
        })

    log(f"{sum(len(v) for v in pending.values())} books across {len(pending)} series to look up")
    processed_this_run = 0
    stopped_early = False

    for series, books in pending.items():
        candidates = cv.resolve_volumes(series)
        if not candidates and cv.failed:
            # ComicVine was unreachable or rate-limited; leave these books for the next run
            # instead of recording a failure that would never be looked at again.
            log(f"skipping series '{series}': ComicVine unavailable")
            stopped_early = True
            break
        wanted = {normalize_issue(b["issue"]) for b in books}
        ranked = cv.score_volumes(candidates, wanted) if candidates else []
        if not ranked and cv.failed:
            log(f"skipping series '{series}': no volume list could be fetched")
            stopped_early = True
            break
        best = ranked[0] if ranked and ranked[0][0] > 0 else None
        others = [entry for entry in ranked[1:] if entry[0] > 0]
        if best:
            log(f"{series}: {len(books)} books -> volume {best[1]['id']} '{best[1]['name']}' "
                f"covers {best[0]} of {len(wanted)} numbers")
        for book in books:
            match = None
            if best:
                match = pick_issue(best[2], book["issue"])
            if match is None and others:
                # A number that the leading run does not hold may belong to a sibling run;
                # accept it only when exactly one other volume claims it.
                claims = [entry for entry in others if pick_issue(entry[2], book["issue"])]
                if len(claims) == 1:
                    match = pick_issue(claims[0][2], book["issue"])
            book_id = book["id"]
            if not match and cv.failed:
                log(f"leaving {book['title']} for the next run: ComicVine unavailable")
                continue
            if not match:
                state["processed"][book_id] = {"status": "no_match", "series": series,
                                               "issue": book["issue"], "volumes": len(candidates)}
                state["stats"]["no_match"] += 1
                log(f"no match: {book['title']}")
            else:
                patch = build_patch(match)
                if args.dry_run:
                    log(f"DRY RUN would patch {book_id}: {book['title']} -> {patch.get('comicvineId')}")
                else:
                    try:
                        bo.patch_metadata(int(book_id), patch)
                        state["stats"]["matched"] += 1
                        log(f"patched {book_id}: {book['title']} -> {patch.get('comicvineId')}")
                    except Exception as exc:  # noqa: BLE001 - keep the service alive on one bad book
                        state["stats"]["error"] += 1
                        log(f"write error {book_id}: {type(exc).__name__}: {exc}")
                        state["processed"][book_id] = {"status": "error"}
                        state["calls"] = cv.limiter.to_state()
                        save_state(state_path, state)
                        continue
                state["processed"][book_id] = {
                    "status": "matched", "comicvineId": patch.get("comicvineId"),
                    "series": series, "issue": book["issue"],
                }
            state["calls"] = cv.limiter.to_state()
            save_state(state_path, state)

            processed_this_run += 1
            if args.limit and processed_this_run >= args.limit:
                log(f"limit of {args.limit} reached this run; stopping")
                state["finishedAt"] = datetime.now(timezone.utc).isoformat()
                save_state(state_path, state)
                log(f"complete: {json.dumps(state['stats'])}")
                return 0

    state["finishedAt"] = datetime.now(timezone.utc).isoformat()
    save_state(state_path, state)
    log(f"complete: {json.dumps(state['stats'])}")
    if stopped_early:
        # Exit non-zero so the unit's Restart=on-failure brings the run back once the key's
        # hourly budget has refilled, instead of the service sitting dead.
        log("stopped because ComicVine was unavailable; the service will restart and resume")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
