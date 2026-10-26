# AGENTS.md

`instarec` is a CLI tool that downloads Instagram livestreams (DASH/MPD), recovering past segments and recording the live
edge concurrently, then muxing everything with `ffmpeg` without re-encoding. See `README.md` for user-facing usage and
CLI arguments.

## Git

**Never commit** (or push, amend, or otherwise create commits). Leave all changes uncommitted in the working tree; the
maintainer reviews and commits them.

## Code style

**Follow the existing code style.** Match the surrounding code's naming, structure, comment density (sparse), logging
patterns and error handling rather than introducing new conventions. In particular:

- Formatting and linting are done with `ruff` (`ruff.toml`): 120-char lines, double quotes, a broad rule set. Run
  `ruff check` and `ruff format` before finishing a change.
- Modern type hints (`str | None`, `list[str]`, `dict[str, Any]`) on function signatures.
- Use `pathlib.Path` rather than `os.path`.
- Log through the per-task loggers in `instarec/log.py` (`log.MAIN`, `log.PAST`, `log.LIVE_DL`, ...) with f-strings;
  don't create ad-hoc loggers or use `print` (except in `interactive.py`, which talks to the user directly).
- Core modules are collections of plain functions that take the `StreamDownloader` as their first argument, importing
  it only under `TYPE_CHECKING` to avoid circular imports. Keep that pattern instead of adding methods or classes.
- Keep the README's argument table in sync when adding or changing CLI options.

## Structure

```
instarec/
  cli.py            Entry point (`instarec` script -> cli:main_entry): argparse, logging setup, MPD resolution, run
  downloader.py     StreamDownloader: holds config + shared state, orchestrates the download tasks
  mpd.py            Fetch/parse the MPD manifest (lxml), select video/audio representations
  past.py           Recover already-broadcast segments (before the first manifest's live edge)
  live.py           Poll the manifest for new segments and download them (producer/consumer via asyncio.Queue)
  io.py             HTTP fetch with retries/exponential backoff, download + append segment pairs
  merger.py         Concatenate past+live temp files, mux with ffmpeg, clean up segments dir
  loss_check.py     Optional English/Korean summary files (duration, missing segments, loss %)
  interactive.py    Interactive quality selection prompt (-i)
  progress_bar.py   tqdm wrapper styled to match log lines
  utils.py          ffprobe helpers, formatting helpers
  log.py            Named LoggerAdapters that tag records with a `task_name`
  instagram/        Resolving a username / profile URL / user ID to an MPD URL
    __init__.py     get_client(): CookieClient if --cookies, else CredentialsClient (needs optional instagrapi)
    client.py       InstagramClient ABC: user ID resolution (HTML scraping + web_profile_info fallback), get_mpd()
    cookies.py      CookieClient: Netscape cookie file, web API via curl_cffi (falls back to aiohttp)
    credentials.py  CredentialsClient: instagrapi login, credentials.json/session.json in the platformdirs config dir
    exceptions.py   InstagramError hierarchy (AuthError, UserNotFoundError, UserNotLiveError, MissingDependencyError)
tests/              pytest; currently only CLI smoke tests (--help, --version) run against the installed package
```

## Main flows

### 1. CLI startup (`cli.main_entry`)

1. Parse args, configure logging (`TqdmStreamHandler` so logs don't break progress bars; `TaskNameFilter` defaults
   `task_name` to `SYSTEM`). Default the output extension to `.mkv`.
2. If the input contains `live-dash` and `.mpd`, use it directly as the MPD URL. Otherwise resolve it via
   `instagram.get_client(...).get_mpd(identifier)`; Instagram errors are logged and exit with status 1.
3. With `-i`, `interactive_stream_selection` fetches the manifest and prompts for representation IDs, which replace
   `--video-quality` / `--audio-quality`.
4. Build a `StreamDownloader` from the args and `asyncio.run(downloader.run())`.

### 2. MPD resolution (`instagram.InstagramClient.get_mpd`)

Normalize the identifier (strip profile URL, `@`) → resolve a numeric user ID if needed (`fetch_user_id`) → query
`live/web_info` for `dash_abr_playback_url` → if not live, check the user's story feed `broadcast` (covers co-hosts) →
otherwise raise `UserNotLiveError`. Subclasses only implement `fetch_live_info` and `fetch_story_feed_info`;
`CredentialsClient` runs blocking instagrapi calls via `asyncio.to_thread` and re-logs in once on `LoginRequired`.

### 3. Download (`StreamDownloader.run`)

All download state lives on the `StreamDownloader` instance (`session`, `stream_info`, `live_download_queue`,
`first_segment_t`, `total_expected_segments`, `missing_segment_timestamps`), and the task modules read/mutate it.

1. Create `<output_stem>_segments/` next to the output file.
2. `_fetch_initial_mpd`: parse the manifest, pick representations (preferred IDs, else highest bandwidth), record
   `initial_t` (last segment timestamp = boundary between past and live) and `publish_frame_time`.
3. Download the video/audio init segments into `video_past.tmp` / `audio_past.tmp`.
4. Run concurrently with `asyncio.gather`:
   - `live.poll_live_manifest`: every `--poll-interval`, queue new timestamps `>= initial_t`. Stops (pushes `None`) on
     the `x-fb-video-broadcast-ended` header or after `--live-end-timeout` without new segments.
   - `live.process_live_downloads`: consume the queue, append segments to `video_live.tmp` / `audio_live.tmp`.
   - `past.download_past_segments` (unless `--no-past`): start at `publishFrameTime` (or brute-force search from 0),
     append each segment to the `*_past.tmp` files, and get the next timestamp from ffprobe's `duration_ts` of the
     concatenated file. On a missing segment or ffprobe failure, `search_forwards_for_next_segment` fires concurrent
     HEAD requests in chunks of `--search-chunk-size` up to `--end-stream-miss-threshold`.
5. `finally` (also on Ctrl+C or errors): `merger.finalize_video` concatenates past+live into `video_full.mp4` /
   `audio_full.mp4`, muxes them with `ffmpeg -c copy` (adds `+faststart` for `.mp4`), and removes the segments dir
   unless `--keep-segments`. Then the optional summary files are written.

Segment URLs are built by substituting `$Time$` in the representation's `SegmentTemplate@media` and joining onto the
MPD's base URL. A segment pair counts as successful only if both video and audio download.

## Development

- Install: `pip install -e .` (plus `instagrapi` and `curl_cffi` for the Instagram features; `requirements.txt` lists
  the runtime extras). `ffmpeg` and `ffprobe` must be on `PATH`.
- Tests: `pytest tests/` (requires the package installed so the `instarec` command exists).
- CI (`.github/workflows/test.yaml`) runs the CLI smoke test and pytest on Python 3.10–3.13. Publishing to PyPI
  (`publish.yml`) runs on GitHub releases; the version lives in `pyproject.toml`.
