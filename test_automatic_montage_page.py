# -*- coding: utf-8 -*-
"""自动混剪桌面页离线测试。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QCheckBox

from app_core.montage_models import MontageFailure
from ui.automatic_montage_page import AutomaticMontagePage


class _ImmediateRunner:
    def __init__(self) -> None:
        self.running = False

    def is_running(self, _key: str) -> bool:
        return self.running

    def run(self, _key: str, **callbacks: object) -> bool:
        self.running = True
        on_started = callbacks.get("on_started")
        on_progress = callbacks.get("on_progress")
        on_success = callbacks.get("on_success")
        on_error = callbacks.get("on_error")
        on_finished = callbacks.get("on_finished")
        work = callbacks["with_progress"]
        if callable(on_started):
            on_started()
        try:
            result = work(on_progress if callable(on_progress) else lambda _event: None)
            if callable(on_success):
                on_success(result)
        except Exception as exc:
            if callable(on_error):
                on_error(str(exc))
        finally:
            self.running = False
            if callable(on_finished):
                on_finished()
        return True


class AutomaticMontagePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.video_a = self.root / "a.mp4"
        self.video_b = self.root / "b.mov"
        self.image = self.root / "cover.jpg"
        self.video_a.write_bytes(b"a")
        self.video_b.write_bytes(b"b")
        self.image.write_bytes(b"image")
        self.rows = [
            {"filename": "a.mp4", "storedPath": str(self.video_a), "typeText": "视频", "mediaCategory": "客户甲"},
            {"filename": "b.mov", "storedPath": str(self.video_b), "typeText": "视频", "mediaCategory": "其他"},
            {"filename": "cover.jpg", "storedPath": str(self.image), "typeText": "图片", "mediaCategory": "其他"},
            {"filename": "missing.mp4", "storedPath": str(self.root / "missing.mp4"), "typeText": "视频", "mediaCategory": "其他"},
        ]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def page(self, **kwargs: object) -> AutomaticMontagePage:
        with patch("ui.automatic_montage_page.media_service.list_media", return_value=self.rows):
            page = AutomaticMontagePage(**kwargs)
        self.addCleanup(page.deleteLater)
        return page

    def test_material_library_videos_are_visible_by_default(self) -> None:
        page = self.page()
        self.assertEqual(page.source_list.count(), 2)
        labels = [page.source_list.item(i).text() for i in range(page.source_list.count())]
        self.assertTrue(any("a.mp4" in label and "客户甲" in label for label in labels))
        self.assertFalse(any("cover.jpg" in label or "missing.mp4" in label for label in labels))

    def test_select_all_and_clear_selection(self) -> None:
        page = self.page()
        page.select_all_sources()
        self.assertEqual(len(page.selected_source_paths()), 2)
        page.clear_source_selection()
        self.assertEqual(page.selected_source_paths(), [])

    def test_select_all_limits_selection_to_fifty_sources(self) -> None:
        self.rows = []
        for index in range(51):
            video = self.root / f"source-{index:02d}.mp4"
            video.write_bytes(str(index).encode())
            self.rows.append(
                {
                    "filename": video.name,
                    "storedPath": str(video),
                    "typeText": "视频",
                    "mediaCategory": "批量测试",
                }
            )

        page = self.page()
        page.select_all_sources()

        self.assertEqual(page.source_list.count(), 51)
        self.assertEqual(len(page.selected_source_paths()), 50)
        self.assertEqual(page.source_count.text(), "已选择 50 / 51")

    def test_build_request_uses_visible_controls(self) -> None:
        page = self.page()
        page.source_list.item(0).setCheckState(Qt.CheckState.Checked)
        page.output_count.setValue(3)
        page.target_duration.setValue(25)
        page.clip_duration.setValue(1.5)
        page.allow_reuse.setChecked(True)
        page.audio_mode.setCurrentIndex(1)
        confirmation = page.findChild(QCheckBox, "sourceAudioConfirmation")
        self.assertIsNotNone(confirmation)
        assert confirmation is not None
        confirmation.setChecked(True)
        page.seed.setValue(123)

        request = page.build_request()
        self.assertEqual(request.source_paths, (self.video_a.resolve(),))
        self.assertEqual(request.output_count, 3)
        self.assertEqual(request.target_duration_ms, 25_000)
        self.assertEqual(request.clip_duration_ms, 1_500)
        self.assertTrue(request.allow_reuse)
        self.assertEqual(request.audio_mode, "source")
        self.assertTrue(request.source_audio_confirmed)
        self.assertEqual(request.seed, 123)
        self.assertEqual(request.title_template, "")
        self.assertEqual(request.body_template, "")

    def test_results_replace_copy_editor_as_third_column(self) -> None:
        page = self.page()

        columns = page.layout().itemAt(1).layout()
        self.assertIsNotNone(columns)
        assert columns is not None
        self.assertEqual(columns.count(), 3)
        self.assertIs(columns.itemAt(2).widget(), page.results_panel)
        self.assertFalse(hasattr(page, "title_template"))
        self.assertFalse(hasattr(page, "body_template"))
        self.assertEqual(page.result_table.columnCount(), 4)
        self.assertEqual(
            [page.result_table.horizontalHeaderItem(index).text() for index in range(4)],
            ["结果", "时长", "剪辑指纹", "文件"],
        )

    def test_source_audio_is_ambient_only_and_confirmation_resets_with_sources(self) -> None:
        page = self.page()
        source_label = page.audio_mode.itemText(1)
        self.assertIn("环境原声", source_label)
        self.assertIn("无人声", source_label)
        confirmation = page.findChild(QCheckBox, "sourceAudioConfirmation")
        self.assertIsNotNone(confirmation)
        assert confirmation is not None

        page.source_list.item(0).setCheckState(Qt.CheckState.Checked)
        page.audio_mode.setCurrentIndex(1)
        self.assertFalse(confirmation.isHidden())
        with self.assertRaises(MontageFailure) as unconfirmed:
            page.build_request()
        self.assertEqual(
            unconfirmed.exception.code,
            "montage_source_audio_confirmation_required",
        )

        confirmation.setChecked(True)
        self.assertTrue(page.build_request().source_audio_confirmed)
        page.source_list.item(1).setCheckState(Qt.CheckState.Checked)
        self.assertFalse(confirmation.isChecked())

    def test_narration_mode_shows_text_and_uses_follow_duration(self) -> None:
        page = self.page()
        page.source_list.item(0).setCheckState(Qt.CheckState.Checked)
        narration_index = page.audio_mode.findData("narration")
        self.assertGreaterEqual(narration_index, 0)
        page.audio_mode.setCurrentIndex(narration_index)

        self.assertFalse(page.narration_panel.isHidden())
        self.assertTrue(page.target_duration.isHidden())
        self.assertFalse(page.target_duration_follow_label.isHidden())
        self.assertTrue(page.source_audio_confirmation.isHidden())

        page.narration_text.setPlainText("  同一条解说  ")
        request = page.build_request()
        self.assertEqual(request.audio_mode, "narration")
        self.assertEqual(request.narration_text, "同一条解说")

        page.audio_mode.setCurrentIndex(page.audio_mode.findData("mute"))
        self.assertEqual(page.narration_text.toPlainText(), "  同一条解说  ")
        self.assertTrue(page.narration_panel.isHidden())

    def test_narration_mode_explains_readback_duration_rule(self) -> None:
        page = self.page()
        page.audio_mode.setCurrentIndex(page.audio_mode.findData("narration"))

        self.assertEqual(
            page.target_duration_follow_label.text(),
            "读回语音 + 300ms（5–180 秒）",
        )

    def test_narration_progress_and_success_show_voice_and_duration(self) -> None:
        page = self.page()
        page._on_progress({"stage": "narration_synthesizing"})
        self.assertIn("系统配音", page.status_label.text())
        page._on_progress({"stage": "narration_readback", "master_duration_ms": 5300})
        self.assertIn("5.3", page.status_label.text())

        result = SimpleNamespace(
            batch_dir=self.root,
            outputs=(),
            narration={"voice_id": "Test Chinese", "master_duration_ms": 5300},
        )
        page._on_success(result)
        self.assertIn("Test Chinese", page.status_label.text())
        self.assertIn("主音轨 5.3 秒", page.status_label.text())

    def test_narration_progress_never_regresses(self) -> None:
        page = self.page()
        progress_values = []
        for event in (
            {"stage": "probing", "current": 1, "total": 1},
            {"stage": "narration_synthesizing"},
            {"stage": "narration_readback", "master_duration_ms": 5300},
            {"stage": "planning", "output_count": 1},
            {"stage": "rendering", "output_index": 1, "output_count": 1},
            {"stage": "output_completed", "output_index": 1, "output_count": 1},
            {"stage": "completed"},
        ):
            page._on_progress(event)
            progress_values.append(page.progress_bar.value())

        self.assertEqual(progress_values, sorted(progress_values))

    def test_rendering_without_output_count_keeps_batch_progress_monotonic(self) -> None:
        page = self.page()
        progress_values = []
        page._on_progress({"stage": "planning", "output_count": 5})
        progress_values.append(page.progress_bar.value())
        for output_index in range(1, 6):
            page._on_progress({"stage": "rendering", "output_index": output_index})
            progress_values.append(page.progress_bar.value())
            page._on_progress(
                {"stage": "output_completed", "output_index": output_index, "output_count": 5}
            )
            progress_values.append(page.progress_bar.value())

        self.assertEqual(progress_values, sorted(progress_values))

    def test_generation_runs_in_runner_and_populates_result_table(self) -> None:
        runner = _ImmediateRunner()
        batch_dir = self.root / "batch"
        batch_dir.mkdir()

        def service(request, *, progress):
            progress({"stage": "planning", "output_count": request.output_count})
            output = batch_dir / "001" / "video.mp4"
            output.parent.mkdir()
            output.write_bytes(b"video")
            return SimpleNamespace(
                status="success",
                batch_dir=batch_dir,
                outputs=(
                    {
                        "index": 1,
                        "status": "success",
                        "title": "标题",
                        "body": "正文内容",
                        "duration_ms": 5_000,
                        "fingerprint": "a" * 64,
                        "output_path": str(output),
                    },
                ),
            )

        page = self.page(batch_service=service, task_runner=runner)
        page.source_list.item(0).setCheckState(Qt.CheckState.Checked)
        page.output_count.setValue(1)
        page.target_duration.setValue(5)
        page.clip_duration.setValue(1)
        page.start_generation()

        self.assertEqual(page.result_table.rowCount(), 1)
        self.assertEqual(page.result_table.item(0, 0).text(), "成功")
        self.assertEqual(page.result_table.item(0, 1).text(), "5 秒")
        self.assertEqual(page.result_table.item(0, 3).text(), "video.mp4")
        self.assertEqual(page.progress_bar.value(), 100)
        self.assertTrue(page.open_output_button.isEnabled())

    def test_shutdown_refuses_to_close_while_rendering(self) -> None:
        runner = _ImmediateRunner()
        runner.running = True
        page = self.page(task_runner=runner)
        with patch("ui.automatic_montage_page.QMessageBox.information") as info:
            self.assertFalse(page.shutdown())
        info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
