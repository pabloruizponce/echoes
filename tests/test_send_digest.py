from __future__ import annotations

import asyncio
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "send_digest.py"
SPEC = importlib.util.spec_from_file_location("send_digest", MODULE_PATH)
assert SPEC and SPEC.loader
send_digest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = send_digest
SPEC.loader.exec_module(send_digest)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def input_filename(self, value) -> str:  # noqa: ANN001
        field_tuple = getattr(value, "field_tuple", None)
        if isinstance(field_tuple, tuple) and field_tuple:
            return str(field_tuple[0])
        return str(getattr(value, "name", ""))

    async def send_message(self, *, chat_id, text, **kwargs) -> None:  # noqa: ANN001
        self.calls.append(("message", {"text": text, **kwargs}))

    async def send_audio(self, *, chat_id, audio, caption, **kwargs) -> None:  # noqa: ANN001
        self.calls.append(("audio", {"caption": caption, "filename": self.input_filename(audio), **kwargs}))

    async def send_voice(self, *, chat_id, voice, caption, **kwargs) -> None:  # noqa: ANN001
        self.calls.append(("voice", {"caption": caption, "filename": self.input_filename(voice), **kwargs}))

    async def send_document(self, *, chat_id, document, caption, **kwargs) -> None:  # noqa: ANN001
        filename = self.input_filename(document)
        kind = "pdf" if filename.endswith(".pdf") else "video_document"
        self.calls.append((kind, {"caption": caption, "filename": filename, **kwargs}))

    async def send_video(self, *, chat_id, video, caption, **kwargs) -> None:  # noqa: ANN001
        self.calls.append(("video", {"caption": caption, "filename": self.input_filename(video), **kwargs}))


class FlakyBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def send_message(self, *, chat_id, text, **kwargs) -> None:  # noqa: ANN001
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("telegram NetworkError: httpx.ConnectError")
        await super().send_message(chat_id=chat_id, text=text, **kwargs)


class VideoRejectingBot(FakeBot):
    async def send_video(self, *, chat_id, video, caption, **kwargs) -> None:  # noqa: ANN001
        raise RuntimeError("Bad Request: failed to process video")


class TransientVideoRejectingBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.video_attempts = 0

    async def send_video(self, *, chat_id, video, caption, **kwargs) -> None:  # noqa: ANN001
        self.video_attempts += 1
        raise RuntimeError("telegram NetworkError: httpx.WriteError")


class OversizedVideoFallbackBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.video_attempts = 0
        self.document_attempts = 0

    async def send_video(self, *, chat_id, video, caption, **kwargs) -> None:  # noqa: ANN001
        self.video_attempts += 1
        if self.video_attempts == 1:
            raise RuntimeError("Bad Request: request entity too large")
        await super().send_video(chat_id=chat_id, video=video, caption=caption, **kwargs)

    async def send_document(self, *, chat_id, document, caption, **kwargs) -> None:  # noqa: ANN001
        self.document_attempts += 1
        if self.document_attempts <= send_digest.DEFAULT_TELEGRAM_RETRIES:
            raise RuntimeError("Bad Request: request entity too large")
        await super().send_document(chat_id=chat_id, document=document, caption=caption, **kwargs)


class RecordingRequest:
    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.kwargs = kwargs


class RecordingBot:
    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.kwargs = kwargs


class SendDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            send_digest,
            "convert_audio_to_voice",
            side_effect=lambda _source, destination: destination.write_bytes(b"ogg voice"),
        )
        self.convert_audio_to_voice = patcher.start()
        self.addCleanup(patcher.stop)
        self.original_probe_video_metadata = send_digest.probe_video_metadata
        metadata_patcher = mock.patch.object(
            send_digest,
            "probe_video_metadata",
            return_value={"width": 1280, "height": 720, "duration": 42},
        )
        self.probe_video_metadata = metadata_patcher.start()
        self.addCleanup(metadata_patcher.stop)

    def build_bundle(self, root: Path) -> tuple[Path, Path, Path, Path]:
        run_dir = root / "processed" / "2026-03-24"
        paper_dir = run_dir / "paper-one"
        media_dir = paper_dir / "media"
        media_dir.mkdir(parents=True)
        (paper_dir / "compressed.pdf").write_bytes(b"%PDF-1.4\n")
        (media_dir / "audio.mp3").write_bytes(b"audio")
        (media_dir / "video.mp4").write_bytes(b"video")

        digest_path = root / "digests" / "2026-03-24.json"
        digest_path.parent.mkdir(parents=True)
        digest_path.write_text(json.dumps({"effective_digest_date": "2026-03-24"}) + "\n")

        ranking_path = root / "rankings" / "2026-03-24.json"
        ranking_path.parent.mkdir(parents=True)
        ranking_path.write_text(
            json.dumps(
                {
                    "ranked": [
                        {
                            "paper": {"title": "Paper One"},
                            "reasons": [
                                "matches Must Include Signals: Human motion generation",
                            ],
                        }
                    ]
                }
            )
            + "\n"
        )

        processed_manifest_path = run_dir / "manifest.json"
        processed_manifest_path.write_text(
            json.dumps(
                {
                    "digest_path": str(digest_path),
                    "ranking_path": str(ranking_path),
                    "results": [
                        {
                            "title": "Paper One",
                            "paper_url": "https://example.com/paper-one.pdf",
                            "uploaded_pdf_path": str(paper_dir / "compressed.pdf"),
                            "status": "ok",
                        }
                    ],
                }
            )
            + "\n"
        )

        media_manifest_path = run_dir / "media-manifest.json"
        media_manifest_path.write_text(
            json.dumps(
                {
                    "processed_manifest_path": str(processed_manifest_path),
                    "digest_path": str(digest_path),
                    "ranking_path": str(ranking_path),
                    "results": [
                        {
                            "title": "Paper One",
                            "audio": {"download_path": str(media_dir / "audio.mp3")},
                            "video": {"download_path": str(media_dir / "video.mp4")},
                        }
                    ],
                }
            )
            + "\n"
        )
        return media_manifest_path, processed_manifest_path, ranking_path, digest_path

    def write_delivery_plan(self, root: Path, titles: list[str]) -> Path:
        path = root / "delivery-plan.json"
        path.write_text(
            json.dumps(
                {
                    "intro_message": "Digest intro from Codex.",
                    "papers": [
                        {
                            "title": title,
                            "message": (
                                f"<b>Codex summary</b> for <i>{title}</i>.\n\n"
                                f"{send_digest.PDF_EMOJI} <i>Te env\u00edo el PDF como archivo nativo "
                                "a continuaci\u00f3n.</i>"
                            ),
                            "pdf_caption": f"<b>PDF</b> for {title}.",
                            "audio_caption": f"<b>Audio</b> explainer for {title}.",
                            "video_caption": f"<b>Video</b> explainer for {title}.",
                        }
                        for title in titles
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        return path

    def test_deliver_bundle_sends_intro_then_summary_voice_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = FakeBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                )
            )

            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message", "voice", "video"])
            for _kind, payload in bot.calls:
                self.assertEqual(payload["parse_mode"], send_digest.TELEGRAM_PARSE_MODE)
            self.assertTrue(bot.calls[0][1]["text"].startswith(send_digest.INTRO_EMOJI))
            self.assertTrue(bot.calls[1][1]["text"].startswith(send_digest.PAPER_MESSAGE_EMOJI))
            self.assertNotIn("PDF como archivo nativo", bot.calls[1][1]["text"])
            self.assertEqual(bot.calls[2][1]["caption"], f"{send_digest.AUDIO_EMOJI} Paper One")
            self.assertEqual(bot.calls[3][1]["caption"], f"{send_digest.VIDEO_EMOJI} Paper One")
            self.assertEqual(bot.calls[2][1]["filename"], "paper-one-voice-2026-03-24.ogg")
            self.assertEqual(bot.calls[3][1]["filename"], "paper-one-video-2026-03-24.mp4")
            self.assertTrue(bot.calls[3][1]["supports_streaming"])
            self.assertEqual(bot.calls[3][1]["width"], 1280)
            self.assertEqual(bot.calls[3][1]["height"], 720)
            self.assertEqual(bot.calls[3][1]["duration"], 42)
            self.assertEqual(self.convert_audio_to_voice.call_count, 1)
            self.assertTrue(str(self.convert_audio_to_voice.call_args.args[0]).endswith("audio.mp3"))
            self.assertEqual(self.convert_audio_to_voice.call_args.args[1].suffix, ".ogg")
            self.assertEqual(result["results"][0]["sent"], ["summary", "audio", "video"])
            self.assertEqual(
                result["results"][0]["delivery_methods"],
                {"audio": "voice", "video": "video"},
            )
            self.assertEqual(result["delivered_count"], 1)
            self.assertEqual(result["failed_count"], 0)

    def test_main_requires_delivery_plan_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)

            with self.assertRaises(SystemExit) as exc:
                send_digest.main(["--manifest", str(media_manifest_path), "--json"])

            self.assertEqual(exc.exception.code, 2)

    def test_delivery_plan_rejects_unknown_missing_or_incomplete_titles(self) -> None:
        raw_results = [{"title": "Paper One"}, {"title": "Paper Two"}]
        delivery_entries = [{"title": "Paper One"}, {"title": "Paper Two"}]

        base = {
            "intro_message": "Intro",
            "papers": {
                "Paper One": {"message": "one"},
                "Paper Two": {"message": "two"},
            },
        }

        with self.assertRaises(SystemExit) as unknown_exc:
            send_digest.validate_delivery_plan(
                {
                    "intro_message": "Intro",
                    "papers": {
                        **base["papers"],
                        "Paper Three": {"message": "three"},
                    },
                },
                raw_results=raw_results,
                delivery_entries=delivery_entries,
            )
        self.assertIn("unknown paper", str(unknown_exc.exception))

        with self.assertRaises(SystemExit) as missing_exc:
            send_digest.validate_delivery_plan(
                {
                    "intro_message": "Intro",
                    "papers": {
                        "Paper One": {"message": "one"}
                    },
                },
                raw_results=raw_results,
                delivery_entries=delivery_entries,
            )
        self.assertIn("missing deliverable paper", str(missing_exc.exception))

        with self.assertRaises(SystemExit) as incomplete_exc:
            send_digest.validate_delivery_plan(
                {
                    "intro_message": "Intro",
                    "papers": {
                        "Paper One": {"message": ""},
                        "Paper Two": {"message": "two"},
                    },
                },
                raw_results=raw_results,
                delivery_entries=delivery_entries,
            )
        self.assertIn("must include non-empty message", str(incomplete_exc.exception))

        send_digest.validate_delivery_plan(
            {
                "intro_message": "Intro",
                "papers": {
                    "Paper One": {"message": "one", "pdf_caption": ""},
                    "Paper Two": {"message": "two", "audio_caption": "", "video_caption": ""},
                },
            },
            raw_results=raw_results,
            delivery_entries=delivery_entries,
        )

    def test_delivery_plan_strips_legacy_pdf_note_and_ignores_captions(self) -> None:
        plan = send_digest.normalize_delivery_plan(
            {
                "intro_message": "Intro",
                "papers": [
                    {
                        "title": "Paper One",
                        "message": (
                            "<b>Summary</b>\n\n"
                            f"{send_digest.PDF_EMOJI} <i>Te env\u00edo el PDF como archivo nativo "
                            "a continuaci\u00f3n.</i>"
                        ),
                        "pdf_caption": "PDF old",
                        "audio_caption": "Audio old",
                        "video_caption": "Video old",
                    }
                ],
            }
        )

        paper = plan["papers"]["Paper One"]
        self.assertEqual(set(paper), {"message"})
        self.assertNotIn("PDF como archivo nativo", paper["message"])
        self.assertTrue(paper["message"].startswith(send_digest.PAPER_MESSAGE_EMOJI))

    def test_missing_media_assets_do_not_abort_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            payload = json.loads(media_manifest_path.read_text())
            payload["results"][0]["audio"] = {}
            payload["results"][0]["video"] = {}
            media_manifest_path.write_text(json.dumps(payload) + "\n")
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = FakeBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                )
            )

            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message"])
            self.assertEqual(result["partial_count"], 1)
            self.assertEqual(result["results"][0]["missing_assets"], ["audio", "video"])

    def test_missing_pdf_asset_is_ignored_for_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, processed_manifest_path, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            processed_payload = json.loads(processed_manifest_path.read_text())
            Path(processed_payload["results"][0]["uploaded_pdf_path"]).unlink()

            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = FakeBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                )
            )

            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message", "voice", "video"])
            self.assertEqual(result["partial_count"], 0)
            self.assertEqual(result["results"][0]["missing_assets"], [])

    def test_video_send_falls_back_to_document_when_inline_upload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = VideoRejectingBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                )
            )

            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message", "voice", "video_document"])
            self.assertEqual(bot.calls[-1][1]["caption"], f"{send_digest.VIDEO_EMOJI} Paper One")
            self.assertEqual(result["results"][0]["delivery_methods"]["video"], "video_document_fallback")
            self.assertEqual(result["fallback_count"], 1)
            self.assertEqual(result["results"][0]["fallbacks"][0]["to"], "send_document")

    def test_video_send_falls_back_to_document_after_transient_inline_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = TransientVideoRejectingBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                )
            )

            self.assertEqual(bot.video_attempts, send_digest.DEFAULT_TELEGRAM_RETRIES)
            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message", "voice", "video_document"])
            self.assertEqual(result["results"][0]["delivery_methods"]["video"], "video_document_fallback")
            self.assertIn("video for Paper One", result["results"][0]["fallbacks"][0]["reason"])

    def test_oversized_video_fallback_creates_telegram_safe_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            safe_path = root / "safe-video.mp4"
            safe_path.write_bytes(b"safe video")
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = OversizedVideoFallbackBot()
            with (
                mock.patch.object(send_digest, "HOSTED_BOT_API_UPLOAD_LIMIT_BYTES", 1),
                mock.patch.object(send_digest, "make_telegram_safe_video_copy", return_value=safe_path) as make_safe,
            ):
                result = asyncio.run(
                    send_digest.deliver_bundle(
                        bot=bot,
                        chat_id="123",
                        media_manifest=media_manifest,
                        processed_manifest=processed_manifest,
                        ranking_payload=ranking_payload,
                        digest_payload=digest_payload,
                        delivery_plan=delivery_plan,
                    )
            )

            make_safe.assert_called_once()
            self.assertEqual(bot.video_attempts, 2)
            self.assertEqual(bot.document_attempts, 1)
            self.assertEqual([kind for kind, _payload in bot.calls], ["message", "message", "voice", "video"])
            self.assertEqual(result["results"][0]["delivery_methods"]["video"], "video_transcoded_fallback")
            self.assertEqual(result["fallback_count"], 1)
            self.assertEqual(result["results"][0]["fallbacks"][0]["transcoded_path"], str(safe_path))

    def test_probe_video_metadata_reads_dimensions_and_duration_from_ffprobe(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps({"streams": [{"width": 1280, "height": 720}], "format": {"duration": "41.7"}}),
            stderr="",
        )

        with (
            mock.patch.object(send_digest.shutil, "which", return_value="/usr/bin/ffprobe"),
            mock.patch.object(send_digest.subprocess, "run", return_value=completed) as run,
        ):
            metadata = self.original_probe_video_metadata(Path("/tmp/video.mp4"))

        self.assertEqual(metadata, {"width": 1280, "height": 720, "duration": 42})
        command = run.call_args.args[0]
        self.assertIn("-show_entries", command)
        self.assertIn("stream=width,height,duration:format=duration", command)

    def test_pdf_path_prefers_uploaded_then_compressed_then_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.pdf"
            compressed = root / "compressed.pdf"
            uploaded = root / "uploaded.pdf"
            original.write_bytes(b"original")
            compressed.write_bytes(b"compressed")
            uploaded.write_bytes(b"uploaded")
            entry = {
                "original_pdf_path": str(original),
                "compressed_pdf_path": str(compressed),
                "uploaded_pdf_path": str(uploaded),
            }

            self.assertEqual(send_digest.pdf_path(entry), uploaded)
            uploaded.unlink()
            self.assertEqual(send_digest.pdf_path(entry), compressed)
            compressed.unlink()
            self.assertEqual(send_digest.pdf_path(entry), original)

    def test_completed_only_skips_incomplete_papers_and_reports_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, processed_manifest_path, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_payload = json.loads(media_manifest_path.read_text())
            media_payload["results"][0]["status"] = "ok"
            media_payload["results"].append(
                {
                    "title": "Paper Two",
                    "status": "failed",
                    "error": "Audio generation rate limited by Google",
                    "audio": {},
                    "video": {},
                }
            )
            media_manifest_path.write_text(json.dumps(media_payload) + "\n")

            processed_payload = json.loads(processed_manifest_path.read_text())
            processed_payload["results"].append(
                {
                    "title": "Paper Two",
                    "paper_url": "https://example.com/paper-two.pdf",
                    "status": "ok",
                }
            )
            processed_manifest_path.write_text(json.dumps(processed_payload) + "\n")

            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = FakeBot()
            result = asyncio.run(
                send_digest.deliver_bundle(
                    bot=bot,
                    chat_id="123",
                    media_manifest=media_manifest,
                    processed_manifest=processed_manifest,
                    ranking_payload=ranking_payload,
                    digest_payload=digest_payload,
                    delivery_plan=delivery_plan,
                    completed_only=True,
                )
            )

            self.assertEqual(
                [kind for kind, _payload in bot.calls],
                ["message", "message", "voice", "video", "message"],
            )
            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["source_selected_count"], 2)
            self.assertEqual(result["delivered_count"], 1)
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(result["skipped_papers"][0]["title"], "Paper Two")
            self.assertIn("rate limited", result["skipped_papers"][0]["reason"])
            self.assertIn("Pendientes", bot.calls[-1][1]["text"])

    def test_transient_intro_send_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan = send_digest.load_delivery_plan(self.write_delivery_plan(root, ["Paper One"]))
            media_manifest, processed_manifest, ranking_payload, digest_payload = send_digest.resolve_artifacts(
                media_manifest_path
            )

            bot = FlakyBot()
            with mock.patch.object(send_digest.asyncio, "sleep", return_value=None):
                result = asyncio.run(
                    send_digest.deliver_bundle(
                        bot=bot,
                        chat_id="123",
                        media_manifest=media_manifest,
                        processed_manifest=processed_manifest,
                        ranking_payload=ranking_payload,
                        digest_payload=digest_payload,
                        delivery_plan=delivery_plan,
                    )
                )

            self.assertEqual(result["delivered_count"], 1)
            self.assertEqual(bot.failures_remaining, 0)

    def test_main_fails_fast_when_telegram_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            config_root = root / ".config"
            with mock.patch.dict(send_digest.os.environ, {"ECHOES_CONFIG_DIR": str(config_root)}, clear=False):
                with self.assertRaises(SystemExit) as exc:
                    send_digest.require_telegram_credentials({})
            self.assertIn("TELEGRAM_BOT_TOKEN", str(exc.exception))

    def test_telegram_credentials_can_come_from_environment(self) -> None:
        fake_token = "123456:" + "abcdefghijklmnopqrstuvwxyz"
        with mock.patch.dict(
            send_digest.os.environ,
            {"TELEGRAM_BOT_TOKEN": fake_token, "TELEGRAM_CHAT_ID": "42"},
            clear=False,
        ):
            token, chat_id = send_digest.require_telegram_credentials({})

        self.assertEqual(token, fake_token)
        self.assertEqual(chat_id, "42")

    def test_build_telegram_bot_uses_proxy_and_custom_api_base(self) -> None:
        values = {
            "TELEGRAM_API_BASE_URL": "http://127.0.0.1:8081/bot",
            "TELEGRAM_PROXY_URL": "socks5://127.0.0.1:9050",
            "TELEGRAM_MEDIA_WRITE_TIMEOUT": "240",
        }

        with (
            mock.patch.object(send_digest, "HTTPXRequest", RecordingRequest),
            mock.patch.object(send_digest, "Bot", RecordingBot),
        ):
            bot = send_digest.build_telegram_bot("123456:" + "abcdefghijklmnopqrstuvwxyz", values)

        self.assertEqual(bot.kwargs["base_url"], "http://127.0.0.1:8081/bot")
        self.assertEqual(bot.kwargs["base_file_url"], "http://127.0.0.1:8081/file/bot")
        self.assertEqual(bot.kwargs["request"].kwargs["proxy"], "socks5://127.0.0.1:9050")
        self.assertEqual(bot.kwargs["request"].kwargs["media_write_timeout"], 240.0)

    def test_build_telegram_bot_uses_sniless_api_ip_fallback(self) -> None:
        bot = send_digest.build_telegram_bot(
            "123456:" + "abcdefghijklmnopqrstuvwxyz",
            {
                "TELEGRAM_API_IP": "149.154.166.110",
                "TELEGRAM_API_HOST_HEADER": "api.telegram.org",
                "TELEGRAM_MEDIA_WRITE_TIMEOUT": "240",
            },
        )

        self.assertIsInstance(bot, send_digest.SNIlessTelegramBot)
        self.assertEqual(bot.api_ip, "149.154.166.110")
        self.assertEqual(bot.host_header, "api.telegram.org")
        self.assertEqual(bot.media_timeout, 240.0)

    def test_sniless_multipart_encoder_uses_input_file_tuple(self) -> None:
        file_payload = send_digest.InputFile(io.BytesIO(b"audio"), filename="paper.mp3")
        filename, content, mimetype = send_digest.input_file_field_tuple(file_payload)
        body, content_type = send_digest.encode_multipart_form(
            {"chat_id": "1", "disable_web_page_preview": False},
            {"audio": (filename, content, mimetype)},
        )

        self.assertIn("multipart/form-data", content_type)
        self.assertIn(b'name="disable_web_page_preview"', body)
        self.assertIn(b"false", body)
        self.assertIn(b'filename="paper.mp3"', body)
        self.assertIn(b"audio", body)

    def test_certificate_hostname_matcher_supports_single_label_wildcards(self) -> None:
        cert = {"subjectAltName": (("DNS", "*.telegram.org"),)}

        self.assertTrue(send_digest.certificate_matches_hostname(cert, "api.telegram.org"))
        self.assertFalse(send_digest.certificate_matches_hostname(cert, "deep.api.telegram.org"))
        self.assertFalse(send_digest.certificate_matches_hostname(cert, "example.org"))

    def test_main_json_reports_delivery_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan_path = self.write_delivery_plan(root, ["Paper One"])
            with (
                mock.patch.object(send_digest, "load_credentials", return_value={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}),
                mock.patch.object(send_digest, "Bot", return_value=FakeBot()),
                mock.patch.object(send_digest, "deliver_bundle", side_effect=RuntimeError("telegram NetworkError")),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = send_digest.main(
                    [
                        "--manifest",
                        str(media_manifest_path),
                        "--delivery-plan",
                        str(delivery_plan_path),
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertIn("telegram NetworkError", payload["error"])

    def test_main_json_redacts_telegram_token_from_delivery_exception(self) -> None:
        token = "123456:" + "abcdefghijklmnopqrstuvwxyz"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_manifest_path, _processed, _ranking, _digest = self.build_bundle(root)
            delivery_plan_path = self.write_delivery_plan(root, ["Paper One"])
            with (
                mock.patch.object(
                    send_digest,
                    "load_credentials",
                    return_value={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "1"},
                ),
                mock.patch.object(send_digest, "Bot", return_value=FakeBot()),
                mock.patch.object(
                    send_digest,
                    "deliver_bundle",
                    side_effect=RuntimeError(
                        f"telegram NetworkError: https://api.telegram.org/bot{token}/sendMessage"
                    ),
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = send_digest.main(
                    [
                        "--manifest",
                        str(media_manifest_path),
                        "--delivery-plan",
                        str(delivery_plan_path),
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn(token, payload["error"])
        self.assertIn("[redacted-telegram-token]", payload["error"])
        self.assertTrue(payload["network_blocked"])


if __name__ == "__main__":
    unittest.main()
