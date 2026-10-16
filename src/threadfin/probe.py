"""Channel probing: run ffprobe against a stream URL and summarise the result.

Port of ``probeChannel`` / ``parseFrameRate`` (m3u.go). The three summary
fields (resolution, frame rate, audio channels) are computed exactly as in the
Go app, including its rounding and fallback rules; ``tests/golden/probe``
proves it. New in this port: extra details (codecs, bit rates), a timeout, a
URL scheme allow-list, and structured errors instead of silently ignored ones.
Storage of results lives in :mod:`threadfin.probe_store`.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https", "rtmp", "rtmps", "rtsp", "rtsps", "rtp", "udp", "srt", "tcp"})
_HTTP_SCHEMES = frozenset({"http", "https"})


class ProbeError(Exception):
    """A probe could not be run or produced no usable output."""


class ProbeParseError(ProbeError):
    """ffprobe output could not be decoded (Go: "failed to parse ffprobe output")."""


class InvalidProbeURLError(ProbeError):
    """The URL is not something we are willing to hand to ffprobe."""


# --- parsing (parity with Go) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeSummary:
    """The three values the Go app reports (empty string when unknown)."""

    resolution: str = ""
    frame_rate: str = ""
    audio_channels: str = ""


@dataclass(frozen=True, slots=True)
class ProbeDetails:
    """Extra facts about the same (last) video and audio streams; new in this port."""

    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    video_bit_rate: int | None = None
    audio_codec: str | None = None
    audio_bit_rate: int | None = None
    audio_sample_rate: int | None = None
    audio_channel_count: int | None = None
    stream_count: int = 0


@dataclass(frozen=True, slots=True)
class ProbeAnalysis:
    summary: ProbeSummary
    details: ProbeDetails


def _typed(stream: dict[str, Any], key: str, kind: type) -> Any:
    """Read ``key`` like Go's json decoder would: null/absent -> zero value, wrong type -> error."""
    value = stream.get(key)
    if value is None:
        return kind()
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbeParseError(f"cannot decode {key!r}={value!r} as an integer")
        return value
    if not isinstance(value, kind):
        raise ProbeParseError(f"cannot decode {key!r}={value!r} as {kind.__name__}")
    return value


def _scan_int(text: str) -> int | None:
    """Like ``fmt.Sscanf(text, "%d", &n)``: leading spaces, optional sign, digits."""
    match = re.match(r"\s*([+-]?\d+)", text)
    return int(match.group(1)) if match else None


def _round_half_away(value: float) -> int:
    """Go's ``math.Round``: halves round away from zero (Python's round() does not)."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _parse_frame_rate(numerator_text: str, denominator_text: str) -> int:
    numerator = _scan_int(numerator_text)
    denominator = _scan_int(denominator_text)
    numerator = 1 if numerator is None else numerator  # Go keeps its default of 1 on failure
    denominator = 1 if denominator is None else denominator
    if denominator == 0:
        return 0
    return _round_half_away(numerator / denominator)


def _audio_layout_label(layout: str, channels: int) -> str:
    if layout:
        return layout
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels} channels")


def _to_int(value: Any) -> int | None:
    """Lenient conversion for the extra details (ffprobe reports bit rates as strings)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def analyze_ffprobe_output(output: str | bytes) -> ProbeAnalysis:
    """Parse ``ffprobe -show_streams -of json`` output.

    The last video stream and the last audio stream win, as in the Go app.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProbeParseError(f"failed to parse ffprobe output: {exc}") from exc

    if data is None:  # JSON null decodes to an empty result
        data = {}
    if not isinstance(data, dict):
        raise ProbeParseError("failed to parse ffprobe output: expected a JSON object")

    streams = data.get("streams")
    if streams is None:
        streams = []
    if not isinstance(streams, list):
        raise ProbeParseError("failed to parse ffprobe output: 'streams' is not a list")

    resolution = frame_rate = audio_channels = ""
    video: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None

    for entry in streams:
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise ProbeParseError("failed to parse ffprobe output: stream is not an object")

        codec_type = _typed(entry, "codec_type", str)
        height = _typed(entry, "height", int)
        _typed(entry, "width", int)
        rate = _typed(entry, "r_frame_rate", str)
        layout = _typed(entry, "channel_layout", str)
        channels = _typed(entry, "channels", int)

        if codec_type == "video":
            video = entry
            resolution = f"{height}p"
            parts = rate.split("/")
            if len(parts) == 2:
                frame_rate = str(_parse_frame_rate(parts[0], parts[1]))
            else:
                frame_rate = rate
        if codec_type == "audio":
            audio = entry
            audio_channels = _audio_layout_label(layout, channels)

    details = ProbeDetails(
        width=_to_int(video.get("width")) if video else None,
        height=_to_int(video.get("height")) if video else None,
        video_codec=(video.get("codec_name") or None) if video else None,
        video_bit_rate=_to_int(video.get("bit_rate")) if video else None,
        audio_codec=(audio.get("codec_name") or None) if audio else None,
        audio_bit_rate=_to_int(audio.get("bit_rate")) if audio else None,
        audio_sample_rate=_to_int(audio.get("sample_rate")) if audio else None,
        audio_channel_count=_to_int(audio.get("channels")) if audio else None,
        stream_count=len(streams),
    )
    return ProbeAnalysis(ProbeSummary(resolution, frame_rate, audio_channels), details)


def parse_ffprobe_output(output: str | bytes) -> ProbeSummary:
    """Just the Go-compatible summary."""
    return analyze_ffprobe_output(output).summary


# --- running ffprobe ---------------------------------------------------------


def validate_probe_url(url: str, *, allow_local: bool = False) -> str:
    """Reject URLs ffprobe should not be asked to open (files, pipes, option injection)."""
    url = url.strip()
    if not url:
        raise InvalidProbeURLError("empty URL")
    if allow_local:
        return url
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidProbeURLError(f"unsupported URL scheme {scheme!r}")
    return url


def resolve_ffprobe(ffmpeg_path: str = "") -> str:
    """Find ffprobe: next to the configured ffmpeg first, then on PATH.

    (The Go app replaced the first "ffmpeg" anywhere in the path, which breaks
    for directories such as ``/opt/ffmpeg/bin/ffmpeg``.)
    """
    if ffmpeg_path:
        path = Path(ffmpeg_path)
        sibling = path.with_name(path.name.replace("ffmpeg", "ffprobe", 1))
        if sibling != path and sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe") or ""


def build_ffprobe_command(ffprobe: str, url: str, *, user_agent: str = "", timeout: float = 20.0) -> list[str]:
    command = [ffprobe, "-v", "error", "-show_streams", "-of", "json"]
    scheme = urlsplit(url).scheme.lower()
    if scheme in _HTTP_SCHEMES:
        # Network options are only valid for HTTP inputs; other protocols reject them.
        command += ["-protocol_whitelist", "http,https,tcp,tls,crypto,httpproxy"]
        command += ["-rw_timeout", str(int(timeout * 1_000_000))]
        if user_agent:
            command += ["-user_agent", user_agent]
    command.append(url)
    return command


@dataclass(frozen=True, slots=True)
class ProbeRun:
    """Outcome of one ffprobe execution (not yet stored)."""

    url: str
    ok: bool
    error: str | None
    duration_ms: int
    summary: ProbeSummary
    details: ProbeDetails
    raw_json: str | None


async def run_ffprobe(
    url: str,
    *,
    ffprobe: str,
    timeout: float = 20.0,  # noqa: ASYNC109 - also ffprobe's own -rw_timeout
    user_agent: str = "",
    allow_local: bool = False,
) -> ProbeRun:
    """Probe ``url``. Failures become ``ok=False`` results; only a bad URL raises."""
    url = validate_probe_url(url, allow_local=allow_local)
    started = time.monotonic()

    def finish(error: str | None, analysis: ProbeAnalysis | None = None, raw: str | None = None) -> ProbeRun:
        return ProbeRun(
            url=url,
            ok=error is None,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
            summary=analysis.summary if analysis else ProbeSummary(),
            details=analysis.details if analysis else ProbeDetails(),
            raw_json=raw,
        )

    if not ffprobe:
        return finish("ffprobe not found: install ffmpeg or set the ffmpeg path")

    command = build_ffprobe_command(ffprobe, url, user_agent=user_agent, timeout=timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return finish(f"failed to execute ffprobe: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return finish(f"ffprobe timed out after {timeout:g}s")
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        return finish(f"ffprobe exited with status {process.returncode}: {detail}".rstrip(": "))

    try:
        analysis = analyze_ffprobe_output(stdout)
    except ProbeParseError as exc:
        return finish(str(exc))
    return finish(None, analysis, stdout.decode("utf-8", errors="replace"))
