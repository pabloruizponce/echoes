from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_media.py"
SPEC = importlib.util.spec_from_file_location("generate_media", MODULE_PATH)
assert SPEC and SPEC.loader
generate_media = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generate_media
SPEC.loader.exec_module(generate_media)


def write_media_plan(path: Path, titles: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "title": title,
                        "audio_prompt": f"Audio prompt for {title}",
                        "video_prompt": f"Video prompt for {title}",
                    }
                    for title in titles
                ]
            },
            indent=2,
        )
        + "\n"
    )


def video_artifact_payload(
    artifact_id: str,
    status: str,
    *,
    source_id: str = "src_456",
    prompt: str = "",
    language: str = generate_media.NOTEBOOKLM_LANGUAGE,
    style_code: int | None = None,
    created_at: str = "2026-03-24T10:00:01",
) -> dict:
    return {
        "id": artifact_id,
        "status": status,
        "type_id": "ArtifactType.VIDEO",
        "created_at": created_at,
        "source_ids": [source_id],
        "language": language,
        "prompt": prompt,
        "prompt_fingerprint": generate_media.prompt_fingerprint(prompt),
        "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
        "raw_style_code": (
            generate_media.VIDEO_WHITEBOARD_STYLE_CODE if style_code is None else style_code
        ),
    }


class GenerateMediaTests(unittest.TestCase):
    def test_audio_generate_command_defaults_to_english_short_deep_dive(self) -> None:
        with mock.patch.object(generate_media, "notebooklm_binary", return_value="notebooklm"):
            command = generate_media.build_generate_command(
                media_type="audio",
                notebook_id="nb_123",
                source_id="src_456",
                prompt="Explain the paper for this researcher.",
            )

        self.assertEqual(
            command,
            [
                "notebooklm",
                "generate",
                "audio",
                "-n",
                "nb_123",
                "-s",
                "src_456",
                "--language",
                "en",
                "--json",
                "--format",
                "deep-dive",
                "--length",
                "short",
                "Explain the paper for this researcher.",
            ],
        )

    def test_audio_generate_command_accepts_spanish_language(self) -> None:
        with mock.patch.object(generate_media, "notebooklm_binary", return_value="notebooklm"):
            command = generate_media.build_generate_command(
                media_type="audio",
                notebook_id="nb_123",
                source_id="src_456",
                prompt="Explain the paper for this researcher.",
                language="spanish",
            )

        self.assertEqual(command[command.index("--language") + 1], "es")
        self.assertEqual(command[-1], "Explain the paper for this researcher.")

    def test_video_generate_command_defaults_to_english_explainer_whiteboard(self) -> None:
        with mock.patch.object(generate_media, "notebooklm_binary", return_value="notebooklm"):
            command = generate_media.build_generate_command(
                media_type="video",
                notebook_id="nb_123",
                source_id="src_456",
                prompt="Explain the paper for this researcher.",
            )

        self.assertEqual(
            command,
            [
                "notebooklm",
                "generate",
                "video",
                "-n",
                "nb_123",
                "-s",
                "src_456",
                "--language",
                "en",
                "--json",
                "--format",
                "explainer",
                "--style",
                "whiteboard",
                "Explain the paper for this researcher.",
            ],
        )

    def test_video_generate_params_use_observed_whiteboard_style_code(self) -> None:
        params = generate_media.build_video_generate_params(
            notebook_id="nb_123",
            source_id="src_456",
            prompt="Explain the paper for this researcher.",
        )

        self.assertEqual(params[1], "nb_123")
        video_payload = params[2]
        self.assertEqual(video_payload[2], 3)
        self.assertEqual(video_payload[3], [[["src_456"]]])
        request = video_payload[8][2]
        self.assertEqual(
            request,
            [
                [["src_456"]],
                "en",
                "Explain the paper for this researcher.",
                None,
                generate_media.VIDEO_FORMAT_CODE,
                generate_media.VIDEO_WHITEBOARD_STYLE_CODE,
            ],
        )

    def test_video_generate_params_accept_english_language(self) -> None:
        params = generate_media.build_video_generate_params(
            notebook_id="nb_123",
            source_id="src_456",
            prompt="Explain the paper for this researcher.",
            language="en",
        )

        request = params[2][8][2]
        self.assertEqual(request[1], "en")
        self.assertEqual(request[4], generate_media.VIDEO_FORMAT_CODE)
        self.assertEqual(request[5], generate_media.VIDEO_WHITEBOARD_STYLE_CODE)

    def test_raw_video_metadata_parser_extracts_generation_request(self) -> None:
        raw = [
            "video-1",
            "Video title",
            3,
            [[["src_456"]]],
            3,
            None,
            None,
            None,
            [
                None,
                "https://example.invalid/video",
                [[["src_456"]], "es", "Explain the paper.", None, 1, 3],
            ],
            None,
            None,
            None,
            None,
            None,
            None,
            [1776872569, 203027000],
        ]

        payload = generate_media.artifact_payload_from_raw(raw)

        self.assertEqual(payload["id"], "video-1")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["type_id"], "video")
        self.assertEqual(payload["source_ids"], ["src_456"])
        self.assertEqual(payload["language"], "es")
        self.assertEqual(payload["prompt"], "Explain the paper.")
        self.assertEqual(
            payload["prompt_fingerprint"],
            generate_media.prompt_fingerprint("Explain the paper."),
        )
        self.assertEqual(payload["raw_format_code"], 1)
        self.assertEqual(payload["raw_style_code"], 3)

    def test_acquire_run_lock_rejects_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_root = Path(tmp)
            lock_path = out_root / generate_media.LOCK_FILE_NAME
            lock_path.write_text(json.dumps({"pid": 12345}))
            with mock.patch.object(generate_media, "process_is_alive", return_value=True):
                with self.assertRaises(generate_media.MediaRunLockedError):
                    generate_media.acquire_run_lock(out_root, out_root / "manifest.json")

    def test_choose_manifest_path_uses_latest_nested_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed_dir = Path(tmp)
            older = processed_dir / "2026-03-23" / "manifest.json"
            newer = processed_dir / "2026-03-24" / "manifest.json"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_text("{}\n")
            newer.write_text("{}\n")
            older.touch()
            newer.touch()
            self.assertEqual(generate_media.latest_manifest_file(processed_dir), newer)

    def test_main_retries_failed_audio_and_downloads_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "processed" / "2026-03-24"
            paper_dir = run_dir / "paper-one"
            paper_dir.mkdir(parents=True)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "digest_date": "2026-03-24",
                        "results": [
                            {
                                "title": "Paper One",
                                "status": "ok",
                                "notebook_id": "nb_123",
                                "source_id": "src_456",
                                "work_dir": str(paper_dir),
                            }
                        ],
                    }
                )
            )
            media_plan_path = run_dir / "media-plan.json"
            write_media_plan(media_plan_path, ["Paper One"])

            state = {"audio_generate_calls": 0, "snapshot_calls": 0}

            def fake_run_json(
                command: list[str],
                *,
                allow_failure_json: bool = False,
                allow_empty_success: bool = False,
            ) -> dict:
                if command[1:3] == ["generate", "audio"]:
                    state["audio_generate_calls"] += 1
                    if state["audio_generate_calls"] == 1:
                        return {"task_id": "audio-1", "status": "pending"}
                    return {"task_id": "audio-2", "status": "pending"}
                raise AssertionError(f"Unexpected command: {command}")

            def fake_generate_video_api(*, notebook_id, source_id, prompt, language="es", raw_style_code=3):
                return {
                    "task_id": "video-1",
                    "status": "pending",
                    "generation_method": generate_media.VIDEO_API_GENERATION_METHOD,
                    "requested_language": language,
                    "requested_format": generate_media.VIDEO_FORMAT,
                    "requested_style": generate_media.VIDEO_STYLE,
                    "requested_source_id": source_id,
                    "prompt_fingerprint": generate_media.prompt_fingerprint(prompt),
                    "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                    "raw_style_code": raw_style_code,
                }

            def fake_snapshot(_notebook_ids):
                state["snapshot_calls"] += 1
                if state["snapshot_calls"] == 1:
                    return {
                        "active_notebook_ids": {"nb_123"},
                        "artifacts": {
                            "nb_123": [
                                {"id": "audio-1", "status": "failed", "type_id": "audio", "created_at": "2026-03-24T10:00:00"},
                                {"id": "video-1", "status": "completed", "type_id": "video", "created_at": "2026-03-24T10:00:01"},
                            ]
                        },
                    }
                return {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-2", "status": "completed", "type_id": "audio", "created_at": "2026-03-24T10:00:02"},
                            {"id": "video-1", "status": "completed", "type_id": "video", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                }

            def fake_download_api(*, notebook_id, media_type, artifact_id, output_path):
                payload = b"audio" if media_type == "audio" else b"video"
                output_path.write_bytes(payload)
                return {"artifact_id": artifact_id, "path": str(output_path), "status": "downloaded"}

            with (
                mock.patch.object(generate_media, "run_json_command", side_effect=fake_run_json),
                mock.patch.object(generate_media, "generate_video_via_api", side_effect=fake_generate_video_api),
                mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
                mock.patch.object(generate_media, "download_artifact_via_api", side_effect=fake_download_api),
                mock.patch.object(generate_media.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_media.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--media-plan",
                        str(media_plan_path),
                        "--poll-interval",
                        "0",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            media_manifest = json.loads((run_dir / "media-manifest.json").read_text())
            self.assertEqual(media_manifest["status"], "ok")
            result_path = paper_dir / "media" / "result.json"
            self.assertTrue(result_path.exists())
            paper_result = json.loads(result_path.read_text())
            self.assertEqual(paper_result["status"], "ok")
            self.assertEqual(paper_result["audio"]["attempts"], 2)
            self.assertEqual(paper_result["video"]["attempts"], 1)
            self.assertTrue((paper_dir / "media" / "audio.mp3").exists())
            self.assertTrue((paper_dir / "media" / "video.mp4").exists())

    def test_main_defaults_to_english_for_audio_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "processed" / "2026-03-24"
            paper_dir = run_dir / "paper-one"
            paper_dir.mkdir(parents=True)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "digest_date": "2026-03-24",
                        "results": [
                            {
                                "title": "Paper One",
                                "status": "ok",
                                "notebook_id": "nb_123",
                                "source_id": "src_456",
                                "work_dir": str(paper_dir),
                            }
                        ],
                    }
                )
            )
            media_plan_path = run_dir / "media-plan.json"
            write_media_plan(media_plan_path, ["Paper One"])
            seen_audio_languages: list[str] = []
            seen_video_languages: list[str] = []

            def fake_run_json(
                command: list[str],
                *,
                allow_failure_json: bool = False,
                allow_empty_success: bool = False,
            ) -> dict:
                if command[1:3] == ["generate", "audio"]:
                    seen_audio_languages.append(command[command.index("--language") + 1])
                    return {"task_id": "audio-1", "status": "pending"}
                raise AssertionError(f"Unexpected command: {command}")

            def fake_generate_video_api(*, notebook_id, source_id, prompt, language="en", raw_style_code=3):
                seen_video_languages.append(language)
                return {
                    "task_id": "video-1",
                    "status": "pending",
                    "generation_method": generate_media.VIDEO_API_GENERATION_METHOD,
                    "requested_language": language,
                    "requested_format": generate_media.VIDEO_FORMAT,
                    "requested_style": generate_media.VIDEO_STYLE,
                    "requested_source_id": source_id,
                    "prompt_fingerprint": generate_media.prompt_fingerprint(prompt),
                    "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                    "raw_style_code": raw_style_code,
                }

            def fake_snapshot(_notebook_ids):
                return {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "audio", "created_at": "2026-03-24T10:00:00"},
                            video_artifact_payload(
                                "video-1",
                                "completed",
                                prompt="Video prompt for Paper One",
                                language="en",
                                created_at="2026-03-24T10:00:01",
                            ),
                        ]
                    },
                }

            def fake_download_api(*, notebook_id, media_type, artifact_id, output_path):
                output_path.write_bytes(media_type.encode())
                return {"artifact_id": artifact_id, "path": str(output_path), "status": "downloaded"}

            with (
                mock.patch.object(generate_media, "run_json_command", side_effect=fake_run_json),
                mock.patch.object(generate_media, "generate_video_via_api", side_effect=fake_generate_video_api),
                mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
                mock.patch.object(generate_media, "download_artifact_via_api", side_effect=fake_download_api),
                mock.patch.object(generate_media.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_media.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--media-plan",
                        str(media_plan_path),
                        "--poll-interval",
                        "0",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(seen_audio_languages, ["en"])
            self.assertEqual(seen_video_languages, ["en"])
            media_manifest = json.loads((run_dir / "media-manifest.json").read_text())
            self.assertEqual(media_manifest["media_language"], "en")
            paper_result = json.loads((paper_dir / "media" / "result.json").read_text())
            self.assertEqual(paper_result["media_language"], "en")
            self.assertEqual(paper_result["audio"]["requested_language"], "en")
            self.assertEqual(paper_result["video"]["requested_language"], "en")

    def test_main_requires_media_plan_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(json.dumps({"digest_date": "2026-03-24", "results": []}) + "\n")

            with self.assertRaises(SystemExit) as exc:
                generate_media.main(["--manifest", str(manifest_path), "--json"])

            self.assertEqual(exc.exception.code, 2)

    def test_main_rejects_invalid_language_value(self) -> None:
        with self.assertRaises(SystemExit) as exc, contextlib.redirect_stderr(io.StringIO()):
            generate_media.main(["--media-plan", "/tmp/media-plan.json", "--language", "fr"])

        self.assertEqual(exc.exception.code, 2)

    def test_media_plan_rejects_unknown_missing_or_incomplete_titles(self) -> None:
        processed_entries = [
            {"title": "Paper One", "status": "ok"},
            {"title": "Paper Two", "status": "ok"},
        ]

        with self.assertRaises(SystemExit) as unknown_exc:
            generate_media.validate_media_plan(
                {
                    "Paper One": {"audio_prompt": "audio", "video_prompt": "video"},
                    "Paper Two": {"audio_prompt": "audio", "video_prompt": "video"},
                    "Paper Three": {"audio_prompt": "audio", "video_prompt": "video"},
                },
                processed_entries,
            )
        self.assertIn("unknown paper", str(unknown_exc.exception))

        with self.assertRaises(SystemExit) as missing_exc:
            generate_media.validate_media_plan(
                {"Paper One": {"audio_prompt": "audio", "video_prompt": "video"}},
                processed_entries,
            )
        self.assertIn("missing successful processed paper", str(missing_exc.exception))

        with self.assertRaises(SystemExit) as incomplete_exc:
            generate_media.validate_media_plan(
                {"Paper One": {"audio_prompt": "audio"}, "Paper Two": {"audio_prompt": "audio", "video_prompt": "video"}},
                processed_entries,
            )
        self.assertIn("must include non-empty audio_prompt and video_prompt", str(incomplete_exc.exception))

    def test_pending_poll_keeps_same_artifact_running_without_resubmit(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Timeout",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-timeout",
            processing_status="ok",
            media_dir="/tmp/paper-timeout/media",
        )
        artifact = paper.video
        submit_counts = {"audio": 0, "video": 0}
        snapshot_payloads = iter(
            [
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "audio", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "pending", "type_id": "video", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "audio", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "completed", "type_id": "video", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
            ]
        )

        def fake_submit(_paper, _artifact):
            submit_counts[_artifact.media_type] += 1
            _artifact.attempts += 1
            if _artifact.media_type == "video":
                generate_media.record_video_generation_options(_paper, _artifact)
            else:
                generate_media.record_audio_generation_options(_paper, _artifact)
            _artifact.artifact_id = "audio-1" if _artifact.media_type == "audio" else "video-1"
            _artifact.generation_status = "pending"
            return True

        def fake_snapshot(_notebook_ids):
            return next(snapshot_payloads)

        def fake_download(_paper, _artifact):
            suffix = "audio.mp3" if _artifact.media_type == "audio" else "video.mp4"
            _artifact.download_path = f"/tmp/paper-timeout/media/{suffix}"

        with (
            mock.patch.object(generate_media, "submit_generation", side_effect=fake_submit),
            mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
            mock.patch.object(generate_media, "download_artifact", side_effect=fake_download),
            mock.patch.object(generate_media, "persist_paper_result"),
            mock.patch.object(generate_media.time, "sleep"),
        ):
            generate_media.process_media([paper], poll_interval=0)

        self.assertEqual(submit_counts["audio"], 1)
        self.assertEqual(submit_counts["video"], 1)
        self.assertEqual(artifact.attempts, 1)
        self.assertEqual(artifact.artifact_id, "video-1")
        self.assertEqual(paper.status, "ok")

    def test_resume_uses_existing_result_and_downloaded_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paper_dir = tmp_path / "paper"
            media_dir = paper_dir / "media"
            media_dir.mkdir(parents=True)
            (media_dir / "audio.mp3").write_bytes(b"audio")
            (media_dir / "result.json").write_text(
                json.dumps(
                    {
                        "notebook_id": "nb_123",
                        "source_id": "src_456",
                        "status": "pending",
                        "audio": {
                            "media_type": "audio",
                            "prompt": "audio",
                            "artifact_id": "audio-1",
                            "generation_status": "in_progress",
                            "attempts": 1,
                            "wait_attempts": 2,
                        },
                        "video": {
                            "media_type": "video",
                            "prompt": "Video prompt",
                            "artifact_id": "video-1",
                            "generation_status": "pending",
                            "requested_language": "es",
                            "requested_format": "explainer",
                            "requested_style": "whiteboard",
                            "requested_source_id": "src_456",
                            "prompt_fingerprint": generate_media.prompt_fingerprint("Video prompt"),
                            "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                            "raw_style_code": generate_media.VIDEO_WHITEBOARD_STYLE_CODE,
                            "generation_method": generate_media.VIDEO_API_GENERATION_METHOD,
                            "attempts": 1,
                            "wait_attempts": 2,
                        },
                    }
                )
            )

            paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper Resume",
                    "status": "ok",
                    "notebook_id": "nb_123",
                    "source_id": "src_456",
                    "work_dir": str(paper_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
                media_language="es",
            )

            self.assertEqual(paper.audio.generation_status, "completed")
            self.assertEqual(paper.audio.download_path, str(media_dir / "audio.mp3"))
            self.assertEqual(paper.audio.prompt, "Audio prompt")
            self.assertEqual(paper.video.prompt, "Video prompt")
            self.assertEqual(paper.video.artifact_id, "video-1")

    def test_existing_video_without_matching_metadata_is_archived_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paper_dir = tmp_path / "paper"
            media_dir = paper_dir / "media"
            media_dir.mkdir(parents=True)
            original_video = media_dir / "video.mp4"
            original_video.write_bytes(b"old video")

            paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper Video",
                    "status": "ok",
                    "notebook_id": "nb_123",
                    "source_id": "src_456",
                    "work_dir": str(paper_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
            )

            self.assertFalse(original_video.exists())
            self.assertTrue(list(media_dir.glob("video.stale-*.mp4")))
            self.assertIsNone(paper.video.download_path)
            self.assertEqual(paper.video.generation_status, "pending")
            self.assertIsNone(paper.video.artifact_id)

    def test_legacy_audio_without_language_metadata_reused_only_for_explicit_spanish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spanish_dir = tmp_path / "paper-spanish"
            spanish_media_dir = spanish_dir / "media"
            spanish_media_dir.mkdir(parents=True)
            spanish_audio = spanish_media_dir / "audio.mp3"
            spanish_audio.write_bytes(b"legacy spanish audio")

            spanish_paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper Spanish",
                    "status": "ok",
                    "notebook_id": "nb_123",
                    "source_id": "src_456",
                    "work_dir": str(spanish_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
                media_language="es",
            )

            self.assertTrue(spanish_audio.exists())
            self.assertEqual(spanish_paper.audio.generation_status, "completed")
            self.assertEqual(spanish_paper.audio.requested_language, "es")

            english_dir = tmp_path / "paper-english"
            english_media_dir = english_dir / "media"
            english_media_dir.mkdir(parents=True)
            english_audio = english_media_dir / "audio.mp3"
            english_audio.write_bytes(b"legacy spanish audio")

            english_paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper English",
                    "status": "ok",
                    "notebook_id": "nb_123",
                    "source_id": "src_456",
                    "work_dir": str(english_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
            )

            self.assertFalse(english_audio.exists())
            self.assertTrue(list(english_media_dir.glob("audio.stale-*.mp3")))
            self.assertIsNone(english_paper.audio.download_path)
            self.assertEqual(english_paper.audio.generation_status, "pending")

    def test_hydrate_ignores_saved_media_state_when_notebook_binding_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paper_dir = tmp_path / "paper"
            media_dir = paper_dir / "media"
            media_dir.mkdir(parents=True)
            (media_dir / "result.json").write_text(
                json.dumps(
                    {
                        "notebook_id": "nb-old",
                        "source_id": "src-old",
                        "status": "failed",
                        "error": "stale",
                        "audio": {
                            "media_type": "audio",
                            "prompt": "audio",
                            "artifact_id": "audio-old",
                            "generation_status": "failed",
                            "attempts": 3,
                        },
                        "video": {
                            "media_type": "video",
                            "prompt": "video",
                            "artifact_id": "video-old",
                            "generation_status": "in_progress",
                            "attempts": 2,
                        },
                    }
                )
            )

            paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper Reset",
                    "status": "ok",
                    "notebook_id": "nb-new",
                    "source_id": "src-new",
                    "work_dir": str(paper_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
            )

            self.assertEqual(paper.status, "pending")
            self.assertIsNone(paper.error)
            self.assertIsNone(paper.audio.artifact_id)
            self.assertEqual(paper.audio.attempts, 0)
            self.assertIsNone(paper.video.artifact_id)

    def test_submit_generation_clears_stale_artifact_id_when_no_new_id_is_returned(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-generate",
            processing_status="ok",
            media_dir="/tmp/paper-generate/media",
        )
        artifact = paper.audio
        artifact.artifact_id = "old-artifact"

        with mock.patch.object(
            generate_media,
            "run_json_command",
            return_value={"error": True, "code": "GENERATION_FAILED", "message": "Generation failed"},
        ):
            generate_media.submit_generation(paper, artifact)

        self.assertIsNone(artifact.artifact_id)

    def test_submit_generation_uses_video_api_with_observed_style_code(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-video-generate",
            processing_status="ok",
            media_dir="/tmp/paper-video-generate/media",
        )
        artifact = paper.video
        artifact.prompt = "Explain the paper."

        with mock.patch.object(
            generate_media,
            "generate_video_via_api",
            return_value={
                "task_id": "video-1",
                "status": "pending",
                "generation_method": generate_media.VIDEO_API_GENERATION_METHOD,
                "requested_language": generate_media.NOTEBOOKLM_LANGUAGE,
                "requested_format": generate_media.VIDEO_FORMAT,
                "requested_style": generate_media.VIDEO_STYLE,
                "requested_source_id": "src_456",
                "prompt_fingerprint": generate_media.prompt_fingerprint("Explain the paper."),
                "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                "raw_style_code": generate_media.VIDEO_WHITEBOARD_STYLE_CODE,
            },
        ) as generate_video:
            accepted = generate_media.submit_generation(paper, artifact)

        self.assertTrue(accepted)
        generate_video.assert_called_once_with(
            notebook_id="nb_123",
            source_id="src_456",
            prompt="Explain the paper.",
            language="en",
        )
        self.assertEqual(artifact.artifact_id, "video-1")
        self.assertEqual(artifact.requested_language, "en")
        self.assertEqual(artifact.requested_format, "explainer")
        self.assertEqual(artifact.requested_style, "whiteboard")
        self.assertEqual(artifact.requested_source_id, "src_456")
        self.assertEqual(artifact.raw_format_code, generate_media.VIDEO_FORMAT_CODE)
        self.assertEqual(artifact.raw_style_code, generate_media.VIDEO_WHITEBOARD_STYLE_CODE)

    def test_submit_generation_falls_back_to_official_cli_when_raw_style_is_rejected(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-video-fallback",
            processing_status="ok",
            media_dir="/tmp/paper-video-fallback/media",
        )
        artifact = paper.video
        artifact.prompt = "Explain the paper."

        with (
            mock.patch.object(
                generate_media,
                "generate_video_via_api",
                return_value={
                    "error": True,
                    "status": "failed",
                    "message": "Invalid video style code",
                    "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                    "raw_style_code": generate_media.VIDEO_WHITEBOARD_STYLE_CODE,
                },
            ),
            mock.patch.object(
                generate_media,
                "run_json_command",
                return_value={"task_id": "video-fallback", "status": "pending"},
            ) as run_json,
            mock.patch.object(generate_media, "notebooklm_binary", return_value="notebooklm"),
        ):
            accepted = generate_media.submit_generation(paper, artifact)

        self.assertTrue(accepted)
        self.assertEqual(artifact.artifact_id, "video-fallback")
        self.assertTrue(artifact.fallback_used)
        self.assertEqual(artifact.generation_method, generate_media.VIDEO_OFFICIAL_FALLBACK_METHOD)
        self.assertEqual(artifact.raw_style_code, generate_media.VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE)
        self.assertIn("fallback style code", artifact.style_warning or "")
        fallback_command = run_json.call_args.args[0]
        self.assertIn("--style", fallback_command)
        self.assertIn("whiteboard", fallback_command)
        self.assertEqual(fallback_command[-1], "Explain the paper.")

    def test_saved_failed_paper_is_retried_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paper_dir = tmp_path / "paper"
            media_dir = paper_dir / "media"
            media_dir.mkdir(parents=True)
            (media_dir / "result.json").write_text(
                json.dumps(
                    {
                        "notebook_id": "nb_123",
                        "source_id": "src_456",
                        "status": "failed",
                        "error": "NotebookLM notebook nb_123 is no longer available.",
                        "audio": {
                            "media_type": "audio",
                            "prompt": "audio",
                            "artifact_id": "audio-1",
                            "generation_status": "failed",
                            "attempts": 1,
                        },
                        "video": {
                            "media_type": "video",
                            "prompt": "Video prompt",
                            "artifact_id": "video-1",
                            "generation_status": "in_progress",
                            "requested_language": "es",
                            "requested_format": "explainer",
                            "requested_style": "whiteboard",
                            "requested_source_id": "src_456",
                            "prompt_fingerprint": generate_media.prompt_fingerprint("Video prompt"),
                            "raw_format_code": generate_media.VIDEO_FORMAT_CODE,
                            "raw_style_code": generate_media.VIDEO_WHITEBOARD_STYLE_CODE,
                            "generation_method": generate_media.VIDEO_API_GENERATION_METHOD,
                            "attempts": 1,
                        },
                    }
                )
            )

            paper = generate_media.paper_result_from_manifest(
                {
                    "title": "Paper Retry",
                    "status": "ok",
                    "notebook_id": "nb_123",
                    "source_id": "src_456",
                    "work_dir": str(paper_dir),
                },
                tmp_path,
                {"audio_prompt": "Audio prompt", "video_prompt": "Video prompt"},
                media_language="es",
            )

            snapshot_payloads = iter(
                [
                    {
                        "active_notebook_ids": {"nb_123"},
                        "artifacts": {
                            "nb_123": [
                                {"id": "audio-2", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:02"},
                                {"id": "video-1", "status": "completed", "type_id": "ArtifactType.VIDEO", "created_at": "2026-03-24T10:00:03"},
                            ]
                        },
                    }
                ]
            )
            submit_counts = {"audio": 0, "video": 0}

            def fake_submit(_paper, _artifact):
                submit_counts[_artifact.media_type] += 1
                _artifact.attempts += 1
                if _artifact.media_type == "video":
                    generate_media.record_video_generation_options(_paper, _artifact)
                else:
                    generate_media.record_audio_generation_options(_paper, _artifact)
                if _artifact.media_type == "audio":
                    _artifact.artifact_id = "audio-2"
                _artifact.generation_status = "pending"
                return True

            def fake_snapshot(_notebook_ids):
                return next(snapshot_payloads)

            def fake_download(_paper, _artifact):
                suffix = "audio.mp3" if _artifact.media_type == "audio" else "video.mp4"
                _artifact.download_path = str(media_dir / suffix)

            with (
                mock.patch.object(generate_media, "submit_generation", side_effect=fake_submit),
                mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
                mock.patch.object(generate_media, "download_artifact", side_effect=fake_download),
                mock.patch.object(generate_media, "persist_paper_result"),
                mock.patch.object(generate_media.time, "sleep"),
            ):
                generate_media.process_media([paper], poll_interval=0)

            self.assertEqual(submit_counts["audio"], 1)
            self.assertEqual(submit_counts["video"], 0)
            self.assertEqual(paper.status, "ok")

    def test_refresh_artifact_status_adopts_newer_matching_artifact(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper",
            processing_status="ok",
            media_dir="/tmp/paper/media",
        )
        artifact = paper.video
        artifact.artifact_id = "old-video"
        artifact.generation_status = "pending"
        artifact.prompt = "Video prompt"

        status = generate_media.refresh_artifact_status(
            paper,
            artifact,
            {"nb_123"},
            set(),
            {},
            {},
            {
                "nb_123": [
                    video_artifact_payload(
                        "new-video",
                        "completed",
                        prompt="Video prompt",
                        created_at="2026-03-24T10:00:05",
                    ),
                    {"id": "old-audio", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:01"},
                ]
            },
        )

        self.assertEqual(status, "completed")
        self.assertEqual(artifact.artifact_id, "new-video")

    def test_adopt_latest_matching_artifact_rejects_wrong_video_style_code(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper",
            processing_status="ok",
            media_dir="/tmp/paper/media",
        )
        artifact = paper.video
        artifact.prompt = "Video prompt"

        accepted_wrong_style = generate_media.adopt_latest_matching_artifact(
            paper,
            artifact,
            [
                video_artifact_payload(
                    "video-style-4",
                    "completed",
                    prompt="Video prompt",
                    style_code=generate_media.VIDEO_OFFICIAL_WHITEBOARD_STYLE_CODE,
                )
            ],
        )

        self.assertFalse(accepted_wrong_style)
        self.assertIsNone(artifact.artifact_id)

        accepted_observed_style = generate_media.adopt_latest_matching_artifact(
            paper,
            artifact,
            [video_artifact_payload("video-style-3", "completed", prompt="Video prompt")],
        )

        self.assertTrue(accepted_observed_style)
        self.assertEqual(artifact.artifact_id, "video-style-3")
        self.assertEqual(artifact.raw_style_code, generate_media.VIDEO_WHITEBOARD_STYLE_CODE)

    def test_adopt_latest_matching_artifact_rejects_wrong_video_language(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper",
            processing_status="ok",
            media_dir="/tmp/paper/media",
            media_language="en",
        )
        artifact = paper.video
        artifact.prompt = "Video prompt"

        accepted_wrong_language = generate_media.adopt_latest_matching_artifact(
            paper,
            artifact,
            [video_artifact_payload("video-es", "completed", prompt="Video prompt", language="es")],
        )

        self.assertFalse(accepted_wrong_language)
        self.assertIsNone(artifact.artifact_id)

        accepted_english = generate_media.adopt_latest_matching_artifact(
            paper,
            artifact,
            [video_artifact_payload("video-en", "completed", prompt="Video prompt", language="en")],
        )

        self.assertTrue(accepted_english)
        self.assertEqual(artifact.artifact_id, "video-en")
        self.assertEqual(artifact.requested_language, "en")

    def test_missing_notebook_snapshot_does_not_immediately_fail(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Missing Notebook",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-missing-notebook",
            processing_status="ok",
            media_dir="/tmp/paper-missing-notebook/media",
        )
        submit_counts = {"audio": 0, "video": 0}
        snapshot_payloads = iter(
            [
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "in_progress", "type_id": "ArtifactType.VIDEO", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
                {
                    "active_notebook_ids": set(),
                    "missing_notebook_ids": {"nb_123"},
                    "notebook_errors": {},
                    "artifact_errors": {},
                    "artifacts": {"nb_123": []},
                },
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "completed", "type_id": "ArtifactType.VIDEO", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
            ]
        )

        def fake_submit(_paper, _artifact):
            submit_counts[_artifact.media_type] += 1
            _artifact.attempts += 1
            if _artifact.media_type == "video":
                generate_media.record_video_generation_options(_paper, _artifact)
            else:
                generate_media.record_audio_generation_options(_paper, _artifact)
            _artifact.artifact_id = "audio-1" if _artifact.media_type == "audio" else "video-1"
            _artifact.generation_status = "pending"
            return True

        def fake_snapshot(_notebook_ids):
            return next(snapshot_payloads)

        def fake_download(_paper, _artifact):
            suffix = "audio.mp3" if _artifact.media_type == "audio" else "video.mp4"
            _artifact.download_path = f"/tmp/paper-missing-notebook/media/{suffix}"

        with (
            mock.patch.object(generate_media, "submit_generation", side_effect=fake_submit),
            mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
            mock.patch.object(generate_media, "download_artifact", side_effect=fake_download),
            mock.patch.object(generate_media, "persist_paper_result"),
            mock.patch.object(generate_media.time, "sleep"),
        ):
            generate_media.process_media([paper], poll_interval=0)

        self.assertEqual(submit_counts["audio"], 1)
        self.assertEqual(submit_counts["video"], 1)
        self.assertEqual(paper.video.attempts, 1)
        self.assertEqual(paper.video.missing_notebook_polls, 0)
        self.assertEqual(paper.status, "ok")

    def test_refresh_artifact_status_fails_after_repeated_missing_notebook_polls(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Gone",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-gone",
            processing_status="ok",
            media_dir="/tmp/paper-gone/media",
        )
        artifact = paper.audio
        artifact.artifact_id = "audio-1"
        artifact.generation_status = "in_progress"

        first = generate_media.refresh_artifact_status(
            paper,
            artifact,
            set(),
            {"nb_123"},
            {},
            {},
            {"nb_123": []},
        )
        second = generate_media.refresh_artifact_status(
            paper,
            artifact,
            set(),
            {"nb_123"},
            {},
            {},
            {"nb_123": []},
        )
        third = generate_media.refresh_artifact_status(
            paper,
            artifact,
            set(),
            {"nb_123"},
            {},
            {},
            {"nb_123": []},
        )

        self.assertEqual(first, "unknown")
        self.assertEqual(second, "unknown")
        self.assertEqual(third, "failed")
        self.assertIn("no longer available", artifact.last_error or "")

    def test_missing_poll_does_not_immediately_resubmit(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Missing",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-missing",
            processing_status="ok",
            media_dir="/tmp/paper-missing/media",
        )
        submit_counts = {"audio": 0, "video": 0}
        snapshot_payloads = iter(
            [
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "pending", "type_id": "ArtifactType.VIDEO", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:00"},
                        ]
                    },
                },
                {
                    "active_notebook_ids": {"nb_123"},
                    "artifacts": {
                        "nb_123": [
                            {"id": "audio-1", "status": "completed", "type_id": "ArtifactType.AUDIO", "created_at": "2026-03-24T10:00:00"},
                            {"id": "video-1", "status": "completed", "type_id": "ArtifactType.VIDEO", "created_at": "2026-03-24T10:00:01"},
                        ]
                    },
                },
            ]
        )

        def fake_submit(_paper, _artifact):
            submit_counts[_artifact.media_type] += 1
            _artifact.attempts += 1
            if _artifact.media_type == "video":
                generate_media.record_video_generation_options(_paper, _artifact)
            else:
                generate_media.record_audio_generation_options(_paper, _artifact)
            _artifact.artifact_id = "audio-1" if _artifact.media_type == "audio" else "video-1"
            _artifact.generation_status = "pending"
            return True

        def fake_snapshot(_notebook_ids):
            return next(snapshot_payloads)

        def fake_download(_paper, _artifact):
            suffix = "audio.mp3" if _artifact.media_type == "audio" else "video.mp4"
            _artifact.download_path = f"/tmp/paper-missing/media/{suffix}"

        with (
            mock.patch.object(generate_media, "submit_generation", side_effect=fake_submit),
            mock.patch.object(generate_media, "snapshot_notebook_state", side_effect=fake_snapshot),
            mock.patch.object(generate_media, "download_artifact", side_effect=fake_download),
            mock.patch.object(generate_media, "persist_paper_result"),
            mock.patch.object(generate_media.time, "sleep"),
        ):
            generate_media.process_media([paper], poll_interval=0)

        self.assertEqual(submit_counts["audio"], 1)
        self.assertEqual(submit_counts["video"], 1)
        self.assertEqual(paper.video.attempts, 1)
        self.assertEqual(paper.status, "ok")

    def test_process_media_bounds_repeated_transient_poll_errors(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Poll Errors",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-poll-errors",
            processing_status="ok",
            media_dir="/tmp/paper-poll-errors/media",
        )
        submit_counts = {"audio": 0, "video": 0}

        def fake_submit(_paper, _artifact):
            submit_counts[_artifact.media_type] += 1
            _artifact.attempts += 1
            if _artifact.media_type == "video":
                generate_media.record_video_generation_options(_paper, _artifact)
            else:
                generate_media.record_audio_generation_options(_paper, _artifact)
            _artifact.artifact_id = f"{_artifact.media_type}-1"
            _artifact.generation_status = "pending"
            return True

        snapshot_payload = {
            "active_notebook_ids": {"nb_123"},
            "missing_notebook_ids": set(),
            "notebook_errors": {},
            "artifact_errors": {
                "nb_123": "RPC rLM1Ne returned null result data (possible server error or parameter mismatch)"
            },
            "artifacts": {"nb_123": []},
        }

        with (
            mock.patch.object(generate_media, "DEFAULT_MAX_TRANSIENT_POLL_ERRORS", 3),
            mock.patch.object(generate_media, "submit_generation", side_effect=fake_submit),
            mock.patch.object(generate_media, "snapshot_notebook_state", return_value=snapshot_payload),
            mock.patch.object(generate_media, "persist_paper_result"),
            mock.patch.object(generate_media.time, "sleep"),
        ):
            generate_media.process_media([paper], poll_interval=0)

        self.assertEqual(submit_counts["audio"], 1)
        self.assertEqual(submit_counts["video"], 1)
        self.assertEqual(paper.status, "failed")
        self.assertIn("status polling failed after 3 transient error(s)", paper.error or "")
        self.assertIn("null result data", paper.error or "")

    def test_process_media_bounds_transient_submit_failures_without_artifact_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paper_dir = tmp_path / "paper"
            media_dir = paper_dir / "media"
            media_dir.mkdir(parents=True)
            (media_dir / "video.mp4").write_bytes(b"video")

            paper = generate_media.MediaPaperResult(
                title="Paper Rate Limited",
                notebook_id="nb_123",
                source_id="src_456",
                work_dir=str(paper_dir),
                processing_status="ok",
                media_dir=str(media_dir),
            )
            generate_media.record_video_generation_options(paper, paper.video)

            def fake_run_json(
                command: list[str],
                *,
                allow_failure_json: bool = False,
                allow_empty_success: bool = False,
            ) -> dict:
                if command[1:3] == ["generate", "audio"]:
                    return {
                        "error": True,
                        "code": "RATE_LIMITED",
                        "message": "Audio generation rate limited by Google",
                    }
                raise AssertionError(f"Unexpected command: {command}")

            with (
                mock.patch.object(generate_media, "DEFAULT_MAX_TRANSIENT_GENERATION_FAILURES", 3),
                mock.patch.object(generate_media, "run_json_command", side_effect=fake_run_json),
                mock.patch.object(
                    generate_media,
                    "snapshot_notebook_state",
                    return_value={"active_notebook_ids": {"nb_123"}, "artifacts": {"nb_123": []}},
                ),
                mock.patch.object(generate_media, "persist_paper_result"),
                mock.patch.object(generate_media.time, "sleep"),
            ):
                generate_media.process_media([paper], poll_interval=0)

            self.assertEqual(paper.audio.attempts, 3)
            self.assertEqual(paper.video.generation_status, "completed")
            self.assertEqual(paper.status, "failed")
            self.assertIn("generation failed after 3 transient attempt(s)", paper.error or "")
            self.assertIn("rate limited", paper.error or "")

    def test_poll_error_streak_resets_after_progress(self) -> None:
        paper = generate_media.MediaPaperResult(
            title="Paper Recovering",
            notebook_id="nb_123",
            source_id="src_456",
            work_dir="/tmp/paper-recovering",
            processing_status="ok",
            media_dir="/tmp/paper-recovering/media",
        )
        artifact = paper.video
        artifact.artifact_id = "video-1"
        artifact.generation_status = "pending"
        transient_message = "RPC rLM1Ne returned null result data (possible server error or parameter mismatch)"

        with mock.patch.object(generate_media, "DEFAULT_MAX_TRANSIENT_POLL_ERRORS", 3):
            first = generate_media.refresh_artifact_status(
                paper,
                artifact,
                {"nb_123"},
                set(),
                {},
                {"nb_123": transient_message},
                {"nb_123": []},
            )
            second = generate_media.refresh_artifact_status(
                paper,
                artifact,
                {"nb_123"},
                set(),
                {},
                {},
                {
                    "nb_123": [
                        video_artifact_payload(
                            "video-1",
                            "in_progress",
                            prompt="",
                            created_at="2026-03-24T10:00:01",
                        )
                    ]
                },
            )
            third = generate_media.refresh_artifact_status(
                paper,
                artifact,
                {"nb_123"},
                set(),
                {},
                {"nb_123": transient_message},
                {"nb_123": []},
            )
            fourth = generate_media.refresh_artifact_status(
                paper,
                artifact,
                {"nb_123"},
                set(),
                {},
                {"nb_123": transient_message},
                {"nb_123": []},
            )
            fifth = generate_media.refresh_artifact_status(
                paper,
                artifact,
                {"nb_123"},
                set(),
                {},
                {},
                {
                    "nb_123": [
                        video_artifact_payload(
                            "video-1",
                            "completed",
                            prompt="",
                            created_at="2026-03-24T10:00:02",
                        )
                    ]
                },
            )

        self.assertEqual(first, "unknown")
        self.assertEqual(second, "in_progress")
        self.assertEqual(third, "unknown")
        self.assertEqual(fourth, "unknown")
        self.assertEqual(fifth, "completed")
        self.assertEqual(paper.status, "pending")
        self.assertIsNone(paper.error)

    def test_main_returns_json_when_polling_repeatedly_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "processed" / "2026-03-24"
            paper_dir = run_dir / "paper-one"
            paper_dir.mkdir(parents=True)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "digest_date": "2026-03-24",
                        "results": [
                            {
                                "title": "Paper One",
                                "status": "ok",
                                "notebook_id": "nb_123",
                                "source_id": "src_456",
                                "work_dir": str(paper_dir),
                            }
                        ],
                    }
                )
            )
            media_plan_path = run_dir / "media-plan.json"
            write_media_plan(media_plan_path, ["Paper One"])

            with (
                mock.patch.object(generate_media, "submit_generation", return_value=True),
                mock.patch.object(
                    generate_media,
                    "snapshot_notebook_state",
                    side_effect=RuntimeError("LIST_ARTIFACTS timed out"),
                ),
                mock.patch.object(generate_media.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = generate_media.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--media-plan",
                        str(media_plan_path),
                        "--poll-interval",
                        "0",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "failed")
            self.assertIn("LIST_ARTIFACTS timed out", payload["message"])

    def test_non_ok_processed_paper_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "processed" / "2026-03-24"
            paper_dir = run_dir / "paper-two"
            paper_dir.mkdir(parents=True)
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "digest_date": "2026-03-24",
                        "results": [
                            {
                                "title": "Paper Two",
                                "status": "failed",
                                "notebook_id": "nb_bad",
                                "source_id": "src_bad",
                                "work_dir": str(paper_dir),
                            }
                        ],
                    }
                )
            )
            media_plan_path = run_dir / "media-plan.json"
            write_media_plan(media_plan_path, [])

            with (
                mock.patch.object(generate_media.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = generate_media.main(
                    ["--manifest", str(manifest_path), "--media-plan", str(media_plan_path), "--json"]
                )

            self.assertEqual(exit_code, 1)
            media_manifest = json.loads((run_dir / "media-manifest.json").read_text())
            self.assertEqual(media_manifest["failed_count"], 1)
            paper_result = json.loads((paper_dir / "media" / "result.json").read_text())
            self.assertEqual(paper_result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
