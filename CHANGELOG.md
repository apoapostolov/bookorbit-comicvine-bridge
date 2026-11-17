# Changelog

All notable changes to this project are documented here. This project follows [Keep a Changelog](https://keepachangelog.com/en/2.0.0/) and uses semantic versioning.

## [Unreleased]

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
