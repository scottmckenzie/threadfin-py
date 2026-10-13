"""Probe parsing parity with Go, command building and the ffprobe runner."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from threadfin.probe import (
    InvalidProbeURLError,
    ProbeParseError,
    analyze_ffprobe_output,
    build_ffprobe_command,
    parse_ffprobe_output,
    resolve_ffprobe,
    run_ffprobe,
    validate_probe_url,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "probe"
CASES = sorted(p.name.removesuffix(".input.json") for p in GOLDEN_DIR.glob("*.input.json"))


class GoldenParseTests(unittest.TestCase):
    def test_cases_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 40)

    def test_matches_go_output(self) -> None:
        for case in CASES:
            with self.subTest(case=case):
                golden = json.loads((GOLDEN_DIR / f"{case}.golden.json").read_text("utf-8"))
                content = (GOLDEN_DIR / f"{case}.input.json").read_bytes()
                if golden["error"]:
                    with self.assertRaises(ProbeParseError):
                        parse_ffprobe_output(content)
                    continue
                summary = parse_ffprobe_output(content)
                self.assertEqual(
                    (summary.resolution, summary.frame_rate, summary.audio_channels),
                    (golden["resolution"], golden["frame_rate"], golden["audio_channels"]),
                )

    def test_details_from_real_ffprobe_output(self) -> None:
        analysis = analyze_ffprobe_output((GOLDEN_DIR / "real_720p_ntsc_51.input.json").read_bytes())
        d = analysis.details
        self.assertEqual((d.width, d.height, d.video_codec), (1280, 720, "mpeg2video"))
        self.assertEqual((d.audio_codec, d.audio_bit_rate), ("ac3", 448000))
        self.assertEqual((d.audio_sample_rate, d.audio_channel_count, d.stream_count), (48000, 6, 2))

    def test_details_use_last_video_and_audio_stream(self) -> None:
        analysis = analyze_ffprobe_output(
            json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "first", "width": 1920, "height": 1080},
                {"codec_type": "video", "codec_name": "last", "width": 640, "height": 480},
                {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "bit_rate": "448000"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2, "bit_rate": "128000"},
            ]})
        )  # fmt: skip
        d = analysis.details
        self.assertEqual((d.video_codec, d.width, d.height), ("last", 640, 480))
        self.assertEqual((d.audio_codec, d.audio_channel_count, d.audio_bit_rate), ("aac", 2, 128000))
        self.assertEqual(d.stream_count, 4)

    def test_details_tolerate_odd_bit_rates(self) -> None:
        d = analyze_ffprobe_output(
            '{"streams":[{"codec_type":"video","bit_rate":"N/A","height":10},'
            '{"codec_type":"audio","bit_rate":null}]}'
        ).details
        self.assertEqual((d.video_bit_rate, d.audio_bit_rate), (None, None))

    def test_details_when_streams_missing(self) -> None:
        d = analyze_ffprobe_output('{"streams": []}').details
        self.assertEqual((d.width, d.video_codec, d.audio_codec, d.stream_count), (None, None, None, 0))


class UrlAndCommandTests(unittest.TestCase):
    def test_validate_accepts_streaming_schemes(self) -> None:
        for url in ("http://h/x.ts", "HTTPS://h/x", "rtmp://h/live", "udp://239.0.0.1:1234"):
            self.assertEqual(validate_probe_url(url), url)

    def test_validate_rejects_dangerous_inputs(self) -> None:
        for url in ("", "  ", "file:///etc/passwd", "/etc/passwd", "-i evil", "concat:a|b",
                    "pipe:0", "data:text/plain,x", "gopher://h"):
            with self.subTest(url=url), self.assertRaises(InvalidProbeURLError):
                validate_probe_url(url)

    def test_validate_allow_local(self) -> None:
        self.assertEqual(validate_probe_url("/tmp/a.ts", allow_local=True), "/tmp/a.ts")

    def test_http_command_has_network_options(self) -> None:
        cmd = build_ffprobe_command("/bin/ffprobe", "http://h/x", user_agent="UA", timeout=5)
        self.assertEqual(cmd[:6], ["/bin/ffprobe", "-v", "error", "-show_streams", "-of", "json"])
        self.assertIn("-protocol_whitelist", cmd)
        self.assertEqual(cmd[cmd.index("-rw_timeout") + 1], "5000000")
        self.assertEqual(cmd[cmd.index("-user_agent") + 1], "UA")
        self.assertEqual(cmd[-1], "http://h/x")

    def test_non_http_command_has_no_http_options(self) -> None:
        cmd = build_ffprobe_command("/bin/ffprobe", "udp://239.0.0.1:1234", user_agent="UA")
        self.assertEqual(cmd[-1], "udp://239.0.0.1:1234")
        for option in ("-user_agent", "-rw_timeout", "-protocol_whitelist"):
            self.assertNotIn(option, cmd)

    def test_resolve_ffprobe_uses_sibling_even_if_dir_contains_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "ffmpeg-bin"
            bin_dir.mkdir()
            (bin_dir / "ffmpeg").touch()
            (bin_dir / "ffprobe").touch()
            self.assertEqual(resolve_ffprobe(str(bin_dir / "ffmpeg")), str(bin_dir / "ffprobe"))

    def test_resolve_ffprobe_falls_back_to_path(self) -> None:
        self.assertEqual(resolve_ffprobe(""), shutil.which("ffprobe") or "")
        self.assertEqual(resolve_ffprobe("/nonexistent/ffmpeg"), shutil.which("ffprobe") or "")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.script = self.root / "fake-ffprobe"
        self.script.write_text(
            '#!/bin/sh\nfor a; do url="$a"; done\n'
            'case "$url" in\n'
            '  *slow*) exec sleep 5 ;;\n'
            '  *fail*) echo "Connection refused" >&2; exit 1 ;;\n'
            '  *garbage*) echo "not json" ;;\n'
            '  *) cat "$FAKE_JSON" ;;\n'
            "esac\n"
        )
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC)
        env = mock.patch.dict(os.environ, {"FAKE_JSON": str(GOLDEN_DIR / "hd_stereo.input.json")})
        env.start()
        self.addCleanup(env.stop)

    async def run_probe(self, url: str, **kwargs: object):
        return await run_ffprobe(url, ffprobe=str(self.script), **kwargs)  # type: ignore[arg-type]

    async def test_success(self) -> None:
        run = await self.run_probe("http://example.invalid/ok")
        self.assertTrue(run.ok)
        self.assertIsNone(run.error)
        self.assertEqual(
            (run.summary.resolution, run.summary.frame_rate, run.summary.audio_channels),
            ("1080p", "25", "stereo"),
        )
        self.assertIn('"streams"', run.raw_json or "")

    async def test_nonzero_exit_reports_stderr(self) -> None:
        run = await self.run_probe("http://example.invalid/fail")
        self.assertFalse(run.ok)
        self.assertIn("status 1", run.error or "")
        self.assertIn("Connection refused", run.error or "")

    async def test_garbage_output(self) -> None:
        run = await self.run_probe("http://example.invalid/garbage")
        self.assertFalse(run.ok)
        self.assertIn("failed to parse ffprobe output", run.error or "")

    async def test_timeout_kills_probe(self) -> None:
        run = await self.run_probe("http://example.invalid/slow", timeout=0.3)
        self.assertFalse(run.ok)
        self.assertIn("timed out", run.error or "")
        self.assertLess(run.duration_ms, 3000)

    async def test_missing_ffprobe_is_a_result_not_a_crash(self) -> None:
        run = await run_ffprobe("http://example.invalid/ok", ffprobe="")
        self.assertFalse(run.ok)
        self.assertIn("ffprobe not found", run.error or "")
        run = await run_ffprobe("http://example.invalid/ok", ffprobe="/nonexistent/ffprobe")
        self.assertFalse(run.ok)
        self.assertIn("failed to execute ffprobe", run.error or "")

    async def test_invalid_url_raises(self) -> None:
        with self.assertRaises(InvalidProbeURLError):
            await self.run_probe("file:///etc/passwd")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg + ffprobe")
class RealFfprobeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_generated_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "t.ts"
            await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                 "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                 "-t", "1", "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "mpegts", str(media)],
                check=True,
            )  # fmt: skip
            run = await run_ffprobe(
                str(media), ffprobe=shutil.which("ffprobe") or "", allow_local=True
            )
        self.assertTrue(run.ok, run.error)
        self.assertEqual(
            (run.summary.resolution, run.summary.frame_rate, run.summary.audio_channels),
            ("240p", "25", "stereo"),
        )
        self.assertEqual((run.details.width, run.details.video_codec), (320, "mpeg2video"))
        self.assertEqual(run.details.audio_codec, "mp2")


if __name__ == "__main__":
    unittest.main()
