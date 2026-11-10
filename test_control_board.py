"""Regression coverage for control-board settings and persistent activity."""
import tempfile
import unittest
from pathlib import Path

from control_board import Store, validate_settings


class SettingsTests(unittest.TestCase):
    def test_reverse_pair_and_alternate_model(self):
        settings = validate_settings({"mode": "dub", "language": "zh", "model": "small"})
        self.assertEqual((settings["language"], settings["model"]), ("zh", "small"))

    def test_unsupported_settings_are_rejected(self):
        for field, value in [("model", "../other"), ("language", "es"), ("pause", float("nan")),
                             ("seconds", True), ("denoise", "false")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_settings({field: value})


class StoreTests(unittest.TestCase):
    def test_reopened_session_report_links_activity_to_saved_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.sqlite3"
            store = Store(path)
            session = store.create(validate_settings({}))
            store.event(session, "phrase", phrase="a", spoken="Hello.", translation="你好。")
            store.event(session, "playback", phrase="a", delay=2.0)
            store.event(session, "playback", phrase="b", delay=3.0)
            store.finish(session, "stopped")
            report = Store(path).report(session)
            self.assertEqual(report["session"]["status"], "stopped")
            self.assertEqual(report["events"][0]["translation"], "你好。")
            self.assertEqual(report["events"][1]["phrase"], report["events"][0]["phrase"])
            self.assertEqual(report["metrics"]["phrases"], 1)
            self.assertEqual(report["metrics"]["median_delay"], 2.5)
