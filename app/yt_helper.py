from __future__ import annotations

import asyncio
import glob
import os
from dataclasses import dataclass
from typing import Any, List, Optional

import yt_dlp


@dataclass
class FormatOption:
    height: int
    label: str
    has_audio: bool
    ext: Optional[str]
    format_string: str


@dataclass
class ProbeResult:
    id: str
    title: str
    url: str
    options: List[FormatOption]
    duration: Optional[int]


def _base_opts(download_dir: str, cookies_file: Optional[str], for_download: bool = False) -> dict[str, Any]:
    o: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "retries": 3,
        "fragment_retries": 3,
    }
    if cookies_file:
        o["cookiefile"] = cookies_file
    if for_download:
        o.update(
            {
                "outtmpl": os.path.join(download_dir, "%(id)s_h%(height)s.%(ext)s"),
                "restrictfilenames": True,
                "continuedl": True,
                "overwrites": False,
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
            }
        )
    return o


async def probe(url: str, download_dir: str, cookies_file: Optional[str]) -> ProbeResult:
    def _probe() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(_base_opts(download_dir, cookies_file)) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_probe)
    formats = info.get("formats") or []
    found: dict[int, dict[str, Any]] = {}
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        h = f.get("height")
        if not h:
            continue
        has_audio = f.get("acodec") not in (None, "none")
        ext = f.get("ext")
        if h not in found:
            found[h] = {"has_audio": has_audio, "ext": ext}
        else:
            found[h]["has_audio"] = found[h]["has_audio"] or has_audio
            if not found[h].get("ext") and ext:
                found[h]["ext"] = ext
    options: List[FormatOption] = []
    for h in sorted(found.keys()):
        fs = f"bv*[height={h}]+ba/b[height={h}]"
        options.append(
            FormatOption(height=h, label=f"{h}p", has_audio=found[h]["has_audio"], ext=found[h].get("ext"), format_string=fs)
        )
    return ProbeResult(
        id=info.get("id") or "unknown",
        title=info.get("title") or "Video",
        url=info.get("webpage_url") or url,
        options=options,
        duration=info.get("duration"),
    )


def find_cached_file(download_dir: str, video_id: str, height: int) -> Optional[str]:
    pattern = os.path.join(download_dir, f"{video_id}_h{height}.*")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


async def download(url: str, height: int, download_dir: str, cookies_file: Optional[str]) -> dict[str, Any]:
    opts = _base_opts(download_dir, cookies_file, for_download=True)
    opts["format"] = f"bv*[height={height}]+ba/b[height={height}]"

    def _dl() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath: Optional[str] = None
            rds = info.get("requested_downloads")
            if rds:
                for it in rds:
                    p = it.get("filepath") or it.get("_filename") or it.get("filename")
                    if p:
                        filepath = p
            if not filepath:
                filepath = ydl.prepare_filename(info)
            return {"info": info, "filepath": filepath}

    return await asyncio.to_thread(_dl)


def find_cached_audio_file(download_dir: str, video_id: str) -> Optional[str]:
    pattern = os.path.join(download_dir, f"{video_id}_audio.*")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


async def download_audio(url: str, download_dir: str, cookies_file: Optional[str], codec: str = "mp3") -> dict[str, Any]:
    opts: dict[str, Any] = _base_opts(download_dir, cookies_file, for_download=False)
    opts.update(
        {
            "outtmpl": os.path.join(download_dir, "%(id)s_audio.%(ext)s"),
            "restrictfilenames": True,
            "continuedl": True,
            "overwrites": False,
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "192"},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        }
    )

    def _dl_audio() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid = info.get("id")
            filepath: Optional[str] = None
            if vid:
                candidates = sorted(glob.glob(os.path.join(download_dir, f"{vid}_audio.*")))
                if candidates:
                    filepath = candidates[-1]
            if not filepath:
                candidate = ydl.prepare_filename(info)
                if os.path.exists(candidate):
                    filepath = candidate
            return {"info": info, "filepath": filepath}

    return await asyncio.to_thread(_dl_audio)
