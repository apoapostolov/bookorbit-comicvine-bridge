# ComicVine Bridge for BookOrbit

<p align="center">
  <img src="assets/icon.png" alt="ComicVine Bridge icon" width="112">
</p>

<p align="center">
  <strong>Fill the gaps in a BookOrbit comics library with ComicVine metadata, one issue at a time.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/BookOrbit-API-green" alt="BookOrbit API">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license">
</p>

![ComicVine Bridge](docs/hero.png)

ComicVine Bridge walks a BookOrbit comics library and fills in what is missing: description, publication date, year, issue number, volume name, and the ComicVine ID. It asks ComicVine about a book only when the filename names a single issue, so trade paperbacks, omnibuses, and multi-issue collections are left alone.

It was written for large libraries. ComicVine's public API allows about 200 requests an hour, so the bridge spends that budget deliberately, saves its place after every book, and stops cleanly when the queue is empty. Point it at a library and let it run for a day; it will not repeat work when you start it again.

## What it writes

For each single issue with a confident match, the bridge sets:

- `comicvineId` in the `4000-<id>` form BookOrbit expects
- `description`, cleaned of ComicVine's HTML markup
- `publishedDate` and `publishedYear` from the cover date
- `comicMetadata.issueNumber` and `comicMetadata.volumeName`

It deliberately does **not** touch series, title, language, or the filesystem. Folder-derived series names survive a run unchanged, and no archive is renamed or repacked.

## How a match is found

ComicVine's search endpoint answers with ten relevance-ranked issues drawn from every volume it knows, so asking it for "Hellblazer 280" returns issue 280 of Viz, Wolverine and Judge Dredd before the one you want, and asking for a series with a common word often returns the right volume's issue not at all. The bridge therefore uses the structured filters instead:

1. The series name resolves to a volume through `/volumes/?filter=name:`, caching the answer per series name.
2. That volume's full issue list is fetched and cached, two to nine pages of a hundred, once per run of the series.
3. Each book is matched against that list by issue number, locally and exactly.

When one name maps to several runs, as "Action Comics" does, the volume that actually contains more of the library's issue numbers wins, and a number held by none of them is checked against the sibling runs and accepted only when exactly one of them claims it. Nothing is attached on a guess. Where the name resolves to no volume at all, the book is recorded as unmatched instead of being given another series' metadata.

## Install

The bridge is a single dependency-free Python file. Python 3.10 or newer is the only requirement.

```bash
git clone https://github.com/apoapostolov/bookorbit-comicvine-bridge.git
cd bookorbit-comicvine-bridge
python bridge.py --version
```

## First run

The bridge needs a BookOrbit operator API key and a ComicVine API key. Keep both out of the repository; pass them as environment variables or through key files with restrictive permissions.

```bash
export BOOKORBIT_URL="http://192.168.1.217:8123/api/v1"
export BOOKORBIT_API_KEY="bok_..."
export COMICVINE_API_KEY="..."
```

Check your parsing and matching against real data without writing anything:

```bash
python bridge.py --dry-run --limit 10
```

Then do a small real run and inspect the result in BookOrbit before opening the taps:

```bash
python bridge.py --limit 25
```

When you are satisfied, run without a limit. The process keeps going until every book has been considered.

```bash
python bridge.py
```

## Running as a service

`scripts/bookorbit-comicvine-bridge.service` is a systemd unit that runs the bridge as a dedicated user, reads credentials from an environment file, and restarts on failure. Install it on the machine that can reach BookOrbit.

```bash
sudo install -d -m 750 /opt/comicvine-bridge
sudo cp bridge.py /opt/comicvine-bridge/
sudo cp scripts/bridge.env.example /opt/comicvine-bridge/bridge.env
sudo nano /opt/comicvine-bridge/bridge.env          # add both keys
sudo chmod 600 /opt/comicvine-bridge/bridge.env
sudo cp scripts/bookorbit-comicvine-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bookorbit-comicvine-bridge
sudo journalctl -u bookorbit-comicvine-bridge -f
```

The service writes `state.json` next to the script. Back that file up if you want to preserve the record of what was matched; delete it to start the library over from the beginning.

## Rate limiting

ComicVine's published limit is roughly 200 requests an hour. The bridge uses a sliding one-hour window of 200 lookups and, when the window is full, waits for the oldest call to age out before continuing. The wait and the timestamps are saved in the state file, so a restart does not reset the budget and risk a ban. Set a lower ceiling with `--rate-limit` if you share the key.

## Options

Run `python bridge.py --help` for the full list. The useful ones:

| Option | Purpose |
| --- | --- |
| `--library-id` | BookOrbit library to scan. Defaults to `2`, the Comics library. |
| `--state` | Path to the checkpoint file. Defaults to `state.json`. |
| `--dry-run` | Query ComicVine and log matches without writing to BookOrbit. |
| `--limit N` | Stop after N newly processed books. Handy for pilots. |
| `--rate-limit N` | ComicVine requests per hour. Defaults to 200. |
| `--bookorbit-key-file` | Read the BookOrbit key from a file instead of the environment. |
| `--comicvine-key-file` | Read the ComicVine key from a file instead of the environment. |

## License

MIT. See [LICENSE](LICENSE).
