# Changelog

All notable changes to this project are documented here. This project follows [Keep a Changelog](https://keepachangelog.com/en/2.0.0/) and uses semantic versioning.

## [Unreleased]

### Changed

- Matching now uses ComicVine's structured filters instead of its fuzzy search. A series name resolves to a volume through `/volumes/?filter=name:`, that volume's whole issue list is fetched once and cached, and every book in the series is matched against it locally. The search endpoint returns ten relevance-ranked issues from any volume, so a correct issue was frequently absent from the page: on a real 400-book sample of the same library the old path matched 28 percent of books and the new one 92 percent.
- When a name maps to several runs, the volume holding more of the library's issue numbers wins rather than the one with the most issues overall. A number no candidate holds is accepted only when exactly one sibling run claims it, so a wrong volume is never preferred over no match.
- Volume candidates and issue lists are cached in `cv-cache/` beside the state file and never expire, which also cuts the request cost: a whole series costs two calls instead of one call per book.

### Fixed

- A transient failure no longer poisons the record. Rate-limited, timed-out and unresolvable requests are retried, and a book whose lookup could not complete is left unrecorded so the next run picks it up, instead of being stored as unmatched forever. An incomplete issue list is never cached.
- A rate limit waits for the budget to refill rather than ending the run, and a run that still cannot reach ComicVine exits non-zero so the unit's `Restart=on-failure` resumes it.

### Added

- `--retry-no-match` forgets previous unmatched results so they are looked up again, which is what makes a matching fix retroactive for an existing library.

## [1.0.0] - 2026-11-17

### Added

- First public release. Scans a chosen BookOrbit library and fills missing issue metadata from ComicVine: ComicVine ID, description, publication date and year, issue number, and volume name.
- Single-issue matching. A book is queried only when its title names one issue, and a match is accepted only when one ComicVine result agrees on both series and issue number. Compilations and ambiguous results are skipped rather than guessed.
- Staggered rate limiting with a sliding one-hour window of 200 ComicVine requests. When the window is full the bridge waits for it to reset, and the timestamps survive a restart.
- Checkpointed state after every book, so an interrupted run resumes without repeating lookups and a completed run stops on its own.
- Dry-run mode and a per-run limit for safe pilots against a live library.
- A systemd unit and an example environment file for running the bridge as a persistent service.
- A README hero and icon, and this changelog.

### Notes

- Series, title, language, and files are never modified. The bridge only fills empty metadata fields that ComicVine can supply.
- A ComicVine API key and a BookOrbit operator API key are required. Neither is stored in the repository.

[Unreleased]: https://github.com/apoapostolov/bookorbit-comicvine-bridge/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/apoapostolov/bookorbit-comicvine-bridge/releases/tag/v1.0.0
