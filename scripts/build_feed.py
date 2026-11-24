#!/usr/bin/env python3
"""Build MP3 episodes and RSS 2.0 feed for the Buongiorno, Indianapolis! podcast."""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1glOqWl8hwgyz7BrwTI6-ZF7qdRzLhR_j")
SITE_BASE_URL = os.environ.get(
    "SITE_BASE_URL", "https://bgross19.github.io/buongiorno-indianapolis"
).rstrip("/")
VOICE = os.environ.get("PODCAST_VOICE", "it-IT-ElsaNeural")

PODCAST_TITLE = "Buongiorno, Indianapolis!"
PODCAST_AUTHOR = "Brian Gross"
PODCAST_LANGUAGE = "it-IT"
PODCAST_DESCRIPTION = (
    "Il tuo briefing mattutino quotidiano in italiano da Indianapolis: "
    "meteo, agenda e uno spunto per iniziare la giornata."
)
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)
KEY_RE = re.compile(r"([?&]key=)[^&\s\"']+")

RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
DRIVE_RETRY_DELAYS = (1, 2, 4)
TTS_RETRY_DELAYS = (2, 5, 10)
MIN_MP3_BYTES = 1025

# MPEG Audio frame tables for pure-Python duration calculation
BITRATE_TABLE_V1_L1 = (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0)
BITRATE_TABLE_V1_L2 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0)
BITRATE_TABLE_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
BITRATE_TABLE_V2_L1 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0)
BITRATE_TABLE_V2_L23 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)

SAMPLE_RATE_TABLE = {
    3: (44100, 48000, 32000),  # MPEG 1
    2: (22050, 24000, 16000),  # MPEG 2
    0: (11025, 12000, 8000),   # MPEG 2.5
}


@dataclass
class Episode:
    date: str
    pub_dt: datetime
    pub_date_str: str
    title: str
    description: str
    script: str
    modified_time: str = ""


def redact_secrets(text: str) -> str:
    """Redact API keys from URLs or error strings before logging."""
    return KEY_RE.sub(r"\1REDACTED", text)


def _http_get_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    """Fetch URL bytes with retry/backoff for transient HTTP or network errors."""
    req_headers = {"User-Agent": "buongiorno-indianapolis-feed-builder/1.0"}
    if headers:
        req_headers.update(headers)

    attempts = len(DRIVE_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body_preview = exc.read(512).decode("utf-8", errors="replace")
            if exc.code in RETRY_HTTP_CODES and attempt < len(DRIVE_RETRY_DELAYS):
                delay = DRIVE_RETRY_DELAYS[attempt]
                print(
                    f"Transient HTTP {exc.code} from {redact_secrets(url)}; retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            hint = ""
            if exc.code in (403, 404):
                hint = (
                    " Hint: verify Google Drive API is enabled in Google Cloud, "
                    "GOOGLE_API_KEY is valid and permitted for Drive API, "
                    "and the Drive folder is shared as 'Anyone with the link: Viewer'."
                )
            raise RuntimeError(
                f"HTTP {exc.code} fetching {redact_secrets(url)}: "
                f"{redact_secrets(body_preview)}{hint}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < len(DRIVE_RETRY_DELAYS):
                delay = DRIVE_RETRY_DELAYS[attempt]
                print(
                    f"Network error fetching {redact_secrets(url)} ({exc}); retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Network error fetching {redact_secrets(url)}: {redact_secrets(str(exc))}"
            ) from None

    raise RuntimeError(f"Exhausted retries fetching {redact_secrets(url)}")


def _get_drive_auth() -> tuple[str | None, dict[str, str]]:
    """Resolve Google Drive API key or Service Account bearer token headers."""
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if api_key:
        return api_key, {}

    sa_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_raw:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set, but 'google-auth' and 'requests' "
                "are not installed. Install them or set GOOGLE_API_KEY instead."
            ) from exc

        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        if sa_raw.lstrip().startswith("{"):
            info = json.loads(sa_raw)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            creds = service_account.Credentials.from_service_account_file(sa_raw, scopes=scopes)
        creds.refresh(Request())
        return None, {"Authorization": f"Bearer {creds.token}"}

    raise RuntimeError(
        "Missing Google Drive credentials: set GOOGLE_API_KEY or GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def _list_drive_json_files(
    folder_id: str, api_key: str | None, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """List non-trashed *.json files inside the target Google Drive folder."""
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        params: dict[str, str] = {
            "q": query,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "1000",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if api_key:
            params["key"] = api_key

        url = f"https://www.googleapis.com/drive/v3/files?{urllib.parse.urlencode(params)}"
        payload = json.loads(_http_get_bytes(url, headers=headers).decode("utf-8"))

        for item in payload.get("files", []):
            name = str(item.get("name", ""))
            mime_type = str(item.get("mimeType", ""))
            if name.lower().endswith(".json") and mime_type != "application/vnd.google-apps.folder":
                files.append(item)

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return files


def _download_drive_file(
    file_meta: dict[str, Any], api_key: str | None, headers: dict[str, str]
) -> str:
    """Download a JSON file (or export Google Doc plain text) from Google Drive."""
    file_id = urllib.parse.quote(str(file_meta["id"]), safe="")
    mime_type = str(file_meta.get("mimeType", ""))

    if mime_type == "application/vnd.google-apps.document":
        params = {"mimeType": "text/plain"}
        endpoint = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
    else:
        params = {"alt": "media", "supportsAllDrives": "true"}
        endpoint = f"https://www.googleapis.com/drive/v3/files/{file_id}"

    if api_key:
        params["key"] = api_key

    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
    return _http_get_bytes(url, headers=headers).decode("utf-8-sig")


def _parse_pub_date(raw_pub_date: Any, date_str: str) -> tuple[datetime, str]:
    """Parse RFC 2822 pubDate or fall back to 04:00:00 America/Indiana/Indianapolis."""
    if isinstance(raw_pub_date, str) and raw_pub_date.strip():
        try:
            dt = email.utils.parsedate_to_datetime(raw_pub_date.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt, email.utils.format_datetime(dt)
        except (TypeError, ValueError):
            pass

    parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    dt = datetime.combine(parsed_date, dt_time(4, 0, 0), tzinfo=LOCAL_TZ)
    return dt, email.utils.format_datetime(dt)


def _parse_episode_json(raw_text: str, filename: str, modified_time: str = "") -> Episode | None:
    """Parse and validate an episode JSON payload, stripping accidental markdown fences."""
    cleaned = raw_text.lstrip("\ufeff").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = FENCE_RE.match(cleaned)
        if not match:
            print(f"Warning: skipping malformed JSON file '{filename}'.", file=sys.stderr)
            return None
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            print(f"Warning: skipping unparseable JSON file '{filename}'.", file=sys.stderr)
            return None

    if not isinstance(data, dict):
        print(f"Warning: skipping non-object JSON file '{filename}'.", file=sys.stderr)
        return None

    raw_date = str(data.get("date") or "").strip()
    stem = Path(filename).stem.strip()
    if DATE_RE.match(raw_date):
        date_str = raw_date
    elif DATE_RE.match(stem):
        date_str = stem
    else:
        print(
            f"Warning: skipping '{filename}' (missing valid YYYY-MM-DD date).",
            file=sys.stderr,
        )
        return None

    title = str(data.get("title") or "").strip()
    script = str(data.get("script") or "").replace("\r\n", "\n").strip()
    if not title or not script:
        print(
            f"Warning: skipping '{filename}' (missing required 'title' or 'script').",
            file=sys.stderr,
        )
        return None

    description = str(data.get("description") or "").strip()
    if not description:
        description = script[:220].rstrip() + ("..." if len(script) > 220 else "")

    pub_dt, pub_date_str = _parse_pub_date(data.get("pubDate"), date_str)
    return Episode(
        date=date_str,
        pub_dt=pub_dt,
        pub_date_str=pub_date_str,
        title=title,
        description=description,
        script=script,
        modified_time=modified_time,
    )


def load_episodes_from_local(local_dir: Path) -> list[Episode]:
    """Load *.json episode scripts from a local directory (for offline testing)."""
    if not local_dir.is_dir():
        raise FileNotFoundError(f"Local directory does not exist: {local_dir}")

    by_date: dict[str, Episode] = {}
    for path in sorted(local_dir.glob("*.json")):
        if not path.is_file():
            continue
        raw_text = path.read_text(encoding="utf-8-sig")
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ).isoformat()
        episode = _parse_episode_json(raw_text, path.name, modified_time=mtime)
        if episode is None:
            continue
        existing = by_date.get(episode.date)
        if existing is None or episode.modified_time >= existing.modified_time:
            by_date[episode.date] = episode

    return list(by_date.values())


def load_episodes_from_drive(folder_id: str) -> list[Episode]:
    """List and download *.json episode scripts from Google Drive."""
    api_key, headers = _get_drive_auth()
    drive_files = _list_drive_json_files(folder_id, api_key=api_key, headers=headers)

    by_date: dict[str, Episode] = {}
    for meta in drive_files:
        name = str(meta.get("name", meta.get("id", "unknown.json")))
        modified_time = str(meta.get("modifiedTime", ""))
        raw_text = _download_drive_file(meta, api_key=api_key, headers=headers)
        episode = _parse_episode_json(raw_text, name, modified_time=modified_time)
        if episode is None:
            continue
        existing = by_date.get(episode.date)
        if existing is None or episode.modified_time >= existing.modified_time:
            by_date[episode.date] = episode

    return list(by_date.values())


async def _synthesize_episode(episode: Episode, mp3_path: Path, voice: str) -> None:
    """Synthesize a single episode script to MP3 using edge-tts with retries."""
    import edge_tts  # Lazy import so --skip-tts works without edge-tts installed.

    tmp_path = mp3_path.with_suffix(".mp3.tmp")
    attempts = len(TTS_RETRY_DELAYS) + 1

    for attempt in range(attempts):
        try:
            if tmp_path.exists():
                tmp_path.unlink()
            communicate = edge_tts.Communicate(text=episode.script, voice=voice)
            await communicate.save(str(tmp_path))
            size = tmp_path.stat().st_size if tmp_path.exists() else 0
            if size < MIN_MP3_BYTES:
                raise RuntimeError(f"Synthesized MP3 too small ({size} bytes) for {episode.date}")
            tmp_path.replace(mp3_path)
            print(f"Synthesized {mp3_path.name} ({size} bytes)")
            return
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            if attempt < len(TTS_RETRY_DELAYS):
                delay = TTS_RETRY_DELAYS[attempt]
                print(
                    f"TTS failed for {episode.date} ({exc}); retrying in {delay}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
            else:
                raise RuntimeError(f"TTS failed for {episode.date} after retries: {exc}") from exc


async def synthesize_missing_episodes(
    episodes: list[Episode], episodes_dir: Path, voice: str
) -> None:
    """Synthesize MP3 audio for any episode not already present on disk."""
    episodes_dir.mkdir(parents=True, exist_ok=True)
    for episode in sorted(episodes, key=lambda ep: ep.date):
        mp3_path = episodes_dir / f"{episode.date}.mp3"
        if mp3_path.is_file() and mp3_path.stat().st_size > 0:
            continue
        await _synthesize_episode(episode, mp3_path, voice=voice)


def get_mp3_duration_seconds(path: Path) -> int:
    """Compute MP3 duration in whole seconds by scanning MPEG audio frame headers."""
    data = path.read_bytes()
    end = len(data)
    if end == 0:
        return 0

    offset = 0
    if end >= 10 and data[:3] == b"ID3":
        tag_size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        footer_size = 10 if (data[5] & 0x10) else 0
        offset = min(10 + tag_size + footer_size, end)

    if end >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128

    total_seconds = 0.0
    while offset + 4 <= end:
        b0 = data[offset]
        if b0 != 0xFF:
            offset += 1
            continue
        b1 = data[offset + 1]
        if (b1 & 0xE0) != 0xE0:
            offset += 1
            continue

        version_bits = (b1 >> 3) & 0x03
        layer_bits = (b1 >> 1) & 0x03
        b2 = data[offset + 2]
        bitrate_idx = (b2 >> 4) & 0x0F
        sample_rate_idx = (b2 >> 2) & 0x03
        padding = (b2 >> 1) & 0x01

        if (
            version_bits == 1
            or layer_bits == 0
            or bitrate_idx in (0, 15)
            or sample_rate_idx == 3
        ):
            offset += 1
            continue

        sample_rate = SAMPLE_RATE_TABLE[version_bits][sample_rate_idx]
        if version_bits == 3:
            if layer_bits == 3:
                bitrate_kbps = BITRATE_TABLE_V1_L1[bitrate_idx]
            elif layer_bits == 2:
                bitrate_kbps = BITRATE_TABLE_V1_L2[bitrate_idx]
            else:
                bitrate_kbps = BITRATE_TABLE_V1_L3[bitrate_idx]
        else:
            if layer_bits == 3:
                bitrate_kbps = BITRATE_TABLE_V2_L1[bitrate_idx]
            else:
                bitrate_kbps = BITRATE_TABLE_V2_L23[bitrate_idx]

        bitrate_bps = bitrate_kbps * 1000
        if layer_bits == 3:
            samples_per_frame = 384
            frame_length = ((12 * bitrate_bps // sample_rate) + padding) * 4
        elif layer_bits == 2:
            samples_per_frame = 1152
            frame_length = ((samples_per_frame // 8) * bitrate_bps // sample_rate) + padding
        else:
            samples_per_frame = 1152 if version_bits == 3 else 576
            frame_length = ((samples_per_frame // 8) * bitrate_bps // sample_rate) + padding

        if frame_length < 4 or offset + frame_length > end:
            offset += 1
            continue

        total_seconds += samples_per_frame / sample_rate
        offset += frame_length

    return round(total_seconds)


def _ns(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def build_feed_xml(
    episodes: list[Episode],
    episodes_dir: Path,
    site_base_url: str,
) -> ET.ElementTree:
    """Build RSS 2.0 + iTunes podcast XML tree sorted newest-first."""
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("atom", ATOM_NS)
    ET.register_namespace("content", CONTENT_NS)

    base = site_base_url.rstrip("/")
    cover_url = f"{base}/cover.jpg"
    feed_url = f"{base}/feed.xml"
    home_url = f"{base}/"

    sorted_episodes = sorted(
        episodes,
        key=lambda ep: (ep.pub_dt.timestamp(), ep.date),
        reverse=True,
    )

    published_episodes: list[tuple[Episode, Path, int]] = []
    for episode in sorted_episodes:
        mp3_path = episodes_dir / f"{episode.date}.mp3"
        if not mp3_path.is_file():
            continue
        file_size = mp3_path.stat().st_size
        if file_size <= 0:
            continue
        published_episodes.append((episode, mp3_path, file_size))

    last_build_date = (
        published_episodes[0][0].pub_date_str
        if published_episodes
        else email.utils.format_datetime(datetime.now(LOCAL_TZ))
    )

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = PODCAST_TITLE
    ET.SubElement(channel, "link").text = home_url
    ET.SubElement(
        channel,
        _ns(ATOM_NS, "link"),
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )
    ET.SubElement(channel, "language").text = PODCAST_LANGUAGE
    ET.SubElement(channel, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(channel, "lastBuildDate").text = last_build_date

    ET.SubElement(channel, _ns(ITUNES_NS, "author")).text = PODCAST_AUTHOR
    ET.SubElement(channel, _ns(ITUNES_NS, "summary")).text = PODCAST_DESCRIPTION
    ET.SubElement(channel, _ns(ITUNES_NS, "type")).text = "episodic"
    ET.SubElement(channel, _ns(ITUNES_NS, "explicit")).text = "false"
    ET.SubElement(channel, _ns(ITUNES_NS, "image"), {"href": cover_url})

    image_el = ET.SubElement(channel, "image")
    ET.SubElement(image_el, "url").text = cover_url
    ET.SubElement(image_el, "title").text = PODCAST_TITLE
    ET.SubElement(image_el, "link").text = home_url

    news_cat = ET.SubElement(channel, _ns(ITUNES_NS, "category"), {"text": "News"})
    ET.SubElement(news_cat, _ns(ITUNES_NS, "category"), {"text": "Daily News"})

    edu_cat = ET.SubElement(channel, _ns(ITUNES_NS, "category"), {"text": "Education"})
    ET.SubElement(edu_cat, _ns(ITUNES_NS, "category"), {"text": "Language Learning"})

    for episode, mp3_path, file_size in published_episodes:

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = episode.title
        ET.SubElement(item, "description").text = episode.description
        ET.SubElement(item, _ns(ITUNES_NS, "summary")).text = episode.description
        ET.SubElement(item, "pubDate").text = episode.pub_date_str
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = (
            f"buongiorno-indianapolis-{episode.date}"
        )
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": f"{base}/episodes/{episode.date}.mp3",
                "length": str(file_size),
                "type": "audio/mpeg",
            },
        )
        duration_sec = get_mp3_duration_seconds(mp3_path)
        if duration_sec > 0:
            ET.SubElement(item, _ns(ITUNES_NS, "duration")).text = str(duration_sec)
        ET.SubElement(item, _ns(ITUNES_NS, "explicit")).text = "false"
        ET.SubElement(item, _ns(ITUNES_NS, "episodeType")).text = "full"
        ET.SubElement(item, _ns(ITUNES_NS, "image"), {"href": cover_url})

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    return tree


def write_feed_atomically(tree: ET.ElementTree, output_path: Path) -> None:
    """Write formatted UTF-8 XML feed atomically via a temporary file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".xml.tmp")
    xml_body = ET.tostring(tree.getroot(), encoding="unicode")
    content = f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(output_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build Buongiorno, Indianapolis! podcast MP3s and feed.xml."
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help="Load *.json episode files from a local directory instead of Google Drive.",
    )
    parser.add_argument(
        "--episodes-dir",
        type=Path,
        default=repo_root / "episodes",
        help="Directory for synthesized <date>.mp3 files (default: <repo>/episodes).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "feed.xml",
        help="Output RSS feed path (default: <repo>/feed.xml).",
    )
    parser.add_argument(
        "--skip-tts",
        action="store_true",
        help="Skip edge-tts synthesis and build feed.xml from existing MP3 files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.local_dir is not None:
        episodes = load_episodes_from_local(args.local_dir)
    else:
        episodes = load_episodes_from_drive(DRIVE_FOLDER_ID)

    if not args.skip_tts:
        asyncio.run(synthesize_missing_episodes(episodes, args.episodes_dir, voice=VOICE))

    tree = build_feed_xml(episodes, args.episodes_dir, site_base_url=SITE_BASE_URL)
    write_feed_atomically(tree, args.output)
    published_count = len(tree.getroot().findall("./channel/item"))
    print(f"Wrote {args.output} with {published_count} episode(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
