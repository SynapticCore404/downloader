from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Optional


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        err = p.stderr.decode(errors="ignore") or "ffmpeg failed"
        raise RuntimeError(err)


async def apply_effect(input_file: str, effect: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_file))[0]
    out = os.path.join(output_dir, f"{base}_{effect}.mp3")
    if effect == "8d":
        filt = "apulsator=hz=0.3"
    elif effect == "concert":
        filt = "aecho=0.8:0.9:1000:0.3,aecho=0.8:0.9:1800:0.25"
    elif effect == "reverb":
        filt = "aecho=0.6:0.7:50:0.5"
    elif effect == "slow":
        filt = "atempo=0.85"
    else:
        raise ValueError("unknown effect")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_file,
        "-vn",
        "-af",
        filt,
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        out,
    ]
    await asyncio.to_thread(_run, cmd)
    return out


async def extract_audio_mp3(input_file: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_file))[0]
    out = os.path.join(output_dir, f"{base}.mp3")
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        input_file,
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        out,
    ]
    await asyncio.to_thread(_run, cmd)
    return out


async def trim_audio_segment(input_file: str, start: str | None, end: str | None, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_file))[0]
    out = os.path.join(output_dir, f"{base}_trim.mp3")
    def _to_seconds(ts: str) -> float:
        parts = ts.split(":")
        parts = [float(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return float(parts[0])

    cmd: list[str] = ["ffmpeg", "-y"]
    duration: Optional[float] = None
    if start:
        cmd += ["-ss", start]
    if end and start:
        try:
            duration = max(0.0, _to_seconds(end) - _to_seconds(start))
        except Exception:
            duration = None
    cmd += ["-i", input_file]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    elif end and not start:
        cmd += ["-to", end]
    cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", out]
    await asyncio.to_thread(_run, cmd)
    return out


async def convert_to_voice(input_file: str, output_dir: str, start: str | None = None, end: str | None = None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_file))[0]
    out = os.path.join(output_dir, f"{base}.ogg")
    def _to_seconds(ts: str) -> float:
        parts = ts.split(":")
        parts = [float(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return float(parts[0])

    cmd: list[str] = ["ffmpeg", "-y"]
    duration: Optional[float] = None
    if start:
        cmd += ["-ss", start]
    if end and start:
        try:
            duration = max(0.0, _to_seconds(end) - _to_seconds(start))
        except Exception:
            duration = None
    cmd += ["-i", input_file]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    elif end and not start:
        cmd += ["-to", end]
    cmd += [
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        "-vbr",
        "on",
        "-compression_level",
        "10",
        "-application",
        "voip",
        out,
    ]
    await asyncio.to_thread(_run, cmd)
    return out
