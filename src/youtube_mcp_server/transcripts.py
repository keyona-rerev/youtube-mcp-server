"""Transcript extraction and search using yt-dlp caption tracks.

Replaces youtube-transcript-api, which YouTube blocks from datacenter IPs.
yt-dlp reads the caption tracks from the player response instead, and with
curl_cffi installed it impersonates a real browser TLS fingerprint.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import yt_dlp

from youtube_mcp_server import config
from youtube_mcp_server.models import TranscriptMatch, TranscriptSegment

# Preference order. json3 carries start and duration natively.
_CAPTION_FORMATS = ("json3", "srv3", "srv1", "vtt")

_FETCH_OPTS = config.with_cookies(
    {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 60,
    }
)


class TranscriptUnavailable(Exception):
    """Raised when a video has no usable caption track."""


def _opts_variants() -> list[dict]:
    """Options to try, in order.

    Cookies answer YouTube's bot check on a datacenter IP, but some yt-dlp and
    YouTube combinations fail only on the cookie path ("The page needs to be
    reloaded", yt-dlp#17389). Neither path is reliable alone, so try the jar
    first and fall back to a bare request.
    """
    variants = [_FETCH_OPTS]
    bare = {k: v for k, v in _FETCH_OPTS.items() if k != "cookiefile"}
    if bare != _FETCH_OPTS:
        variants.append(bare)
    return variants


def get_transcript(
    video_url: str,
    language: str = "en",
) -> list[TranscriptSegment]:
    """Get the transcript of a YouTube video."""
    video_id = _extract_video_id(video_url)
    url = f"https://www.youtube.com/watch?v={video_id}"

    last_error: Exception | None = None
    for opts in _opts_variants():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                track = _pick_track(info, language)
                if not track:
                    raise TranscriptUnavailable(
                        f"No transcript available for {video_id}. "
                        "Subtitles may be disabled."
                    )
                fmt = _pick_format(track)
                raw = ydl.urlopen(fmt["url"]).read().decode(
                    "utf-8", errors="replace"
                )
        except TranscriptUnavailable:
            # A real absence of captions. Another attempt cannot help.
            raise
        except Exception as exc:
            last_error = exc
            continue

        segments = (
            _parse_json3(raw) if fmt.get("ext") == "json3" else _parse_vtt(raw)
        )
        if not segments:
            raise TranscriptUnavailable(f"Caption track for {video_id} was empty.")
        return segments

    raise last_error if last_error else TranscriptUnavailable(video_id)


def search_transcript(
    video_url: str,
    query: str,
    language: str = "en",
    context_segments: int = 2,
) -> list[TranscriptMatch]:
    """Search for a query within a video's transcript.

    Returns matching segments with surrounding context.
    """
    segments = get_transcript(video_url, language)
    query_lower = query.lower()
    matches = []
    used_indices: set[int] = set()

    for i, segment in enumerate(segments):
        if query_lower in segment.text.lower() and i not in used_indices:
            start_idx = max(0, i - context_segments)
            end_idx = min(len(segments), i + context_segments + 1)

            context_text_parts = []
            for j in range(start_idx, end_idx):
                context_text_parts.append(segments[j].text)
                used_indices.add(j)

            combined_text = " ".join(context_text_parts)
            matches.append(
                TranscriptMatch(
                    text=combined_text,
                    start=segments[start_idx].start,
                )
            )

    return matches


def search_channel_transcripts(
    video_urls: list[dict],
    query: str,
    language: str = "en",
    max_videos: int = 20,
) -> list[TranscriptMatch]:
    """Search for a query across multiple video transcripts.

    video_urls should be a list of dicts with 'url' and 'title' keys.
    Concurrency is held at 3 to reduce the chance of a YouTube bot check.
    """
    all_matches = []

    def _search_one(video: dict) -> list[TranscriptMatch]:
        url = video["url"]
        title = video.get("title", "")
        try:
            matches = search_transcript(url, query, language, context_segments=1)
            for match in matches:
                match.video_title = title
                match.video_url = url
                if len(match.text) > 300:
                    match.text = match.text[:300] + "…"
            return matches[:3]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_search_one, v): v for v in video_urls[:max_videos]}
        for future in as_completed(futures):
            all_matches.extend(future.result())
            if len(all_matches) >= 15:
                break

    return all_matches[:15]


def _pick_track(info: dict, language: str) -> list[dict] | None:
    """Choose a caption track. Manual captions win over auto-generated."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    wanted = [language, language.split("-")[0], "en"]

    for source in (manual, auto):
        for code in wanted:
            if code in source:
                return source[code]
        # Fall back on a regional variant, e.g. "en-US" for "en".
        for code in wanted:
            for key in source:
                if key.split("-")[0] == code:
                    return source[key]
    return None


def _pick_format(track: list[dict]) -> dict:
    for ext in _CAPTION_FORMATS:
        for fmt in track:
            if fmt.get("ext") == ext and fmt.get("url"):
                return fmt
    raise TranscriptUnavailable("No downloadable caption format found.")


def _parse_json3(raw: str) -> list[TranscriptSegment]:
    data = json.loads(raw)
    segments = []
    for event in data.get("events", []):
        parts = [s.get("utf8", "") for s in event.get("segs", [])]
        text = "".join(parts).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                text=text,
                start=event.get("tStartMs", 0) / 1000.0,
                duration=event.get("dDurationMs", 0) / 1000.0,
            )
        )
    return segments


def _ts_to_seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds.replace(",", "."))


def _parse_vtt(raw: str) -> list[TranscriptSegment]:
    segments = []
    cue = re.compile(r"(\d+:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d+:\d{2}:\d{2}[.,]\d{3})")

    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        match = None
        text_lines = []
        for line in lines:
            found = cue.search(line)
            if found and match is None:
                match = found
                continue
            if match is not None:
                text_lines.append(re.sub(r"<[^>]+>", "", line))
        if match is None:
            continue
        text = " ".join(text_lines).strip()
        if not text:
            continue
        start = _ts_to_seconds(match.group(1))
        end = _ts_to_seconds(match.group(2))
        segments.append(
            TranscriptSegment(text=text, start=start, duration=max(end - start, 0.0))
        )
    return segments


def _extract_video_id(url: str) -> str:
    """Extract a video ID from a YouTube URL.

    Rejects anything that is not a YouTube video reference. This keeps the
    tool from being used to fetch arbitrary hosts.
    """
    patterns = [
        r"(?:v=|/v/|/live/|/embed/|/shorts/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Not a recognized YouTube video URL: {url}")
