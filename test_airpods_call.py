"""Offline checks for two-way routing, segmentation, and feedback protection."""

import unittest
from threading import Event
from unittest.mock import patch

import numpy as np

import airpods_call as call
import airpods_translate as speech
import airpods_bench as bench


class EndpointTests(unittest.TestCase):
    @patch.object(bench.sd, 'query_hostapis', return_value=[{
        'name': 'Windows WASAPI', 'devices': [0, 1],
        'default_input_device': -1, 'default_output_device': 0,
    }])
    @patch.object(bench.sd, 'query_devices')
    def test_standard_cable_does_not_match_hifi_cable(self, devices, apis):
        devices.return_value = [dict(
            name=name, hostapi=0, max_input_channels=0, max_output_channels=2,
            default_samplerate=48000, default_low_input_latency=0,
            default_high_input_latency=0, default_low_output_latency=0.01,
            default_high_output_latency=0.1,
        ) for name in ['Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)',
                       'CABLE Input (VB-Audio Virtual Cable)']]
        index, _ = bench.find_endpoint('output', 'CABLE Input')
        self.assertEqual(index, 1)


class RoutingTests(unittest.TestCase):
    def endpoint(self, direction, name):
        ids = {('input', 'AirPods'): 1, ('output', 'AirPods'): 2,
               ('output', 'CABLE Input'): 3, ('input', 'CABLE Output'): 4,
               ('output', 'Hi-Fi Cable Input'): 5, ('input', 'Hi-Fi Cable Output'): 6}
        return ids[direction, name], {'name': name, 'default_samplerate': 48000}

    @patch.object(call, 'find_endpoint')
    def test_translations_have_separate_destinations(self, find):
        find.side_effect = self.endpoint
        routes = call.select_routes()
        self.assertEqual((routes.microphone, routes.to_caller,
                          routes.from_caller, routes.headphones), (1, 3, 6, 2))

    @patch.object(call, 'find_endpoint')
    def test_mismatched_hifi_rates_fail_before_capture(self, find):
        def endpoint(direction, name):
            index, info = self.endpoint(direction, name)
            if name == 'Hi-Fi Cable Output':
                info['default_samplerate'] = 44100
            return index, info
        find.side_effect = endpoint
        with self.assertRaisesRegex(RuntimeError, 'sample rate'):
            call.select_routes()


class SegmentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        voice = speech.load_voice(speech.MODELS / 'en_US-lessac-low.onnx')
        pcm, rate = speech.synthesize('Where is the train station? Hello, my friend.', voice)
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        cls.audio = np.interp(np.arange(int(len(audio) * 16000 / rate)) * rate / 16000,
                              np.arange(len(audio)), audio).astype(np.float32)

    def capture(self, seconds=20):
        return call.SpeechCapture(1, seconds, 0.9, 'test', Event())

    def feed(self, capture, samples, suppressed=False):
        for offset in range(0, len(samples), capture.window):
            chunk = samples[offset:offset + capture.window]
            chunk = np.pad(chunk, (0, capture.window - len(chunk)))
            capture.feed(chunk, suppressed=suppressed)

    def test_continuous_speech_is_not_cut_at_five_seconds(self):
        capture = self.capture()
        self.feed(capture, np.tile(self.audio, 3))
        self.feed(capture, np.zeros(16000, dtype=np.float32))
        phrases = []
        while not capture.pending.empty():
            phrases.append(capture.pending.get_nowait()[0])
        self.assertTrue(any(len(p) > 5 * 16000 for p in phrases))

    def test_guard_discards_playback_then_resumes_real_speech(self):
        capture = self.capture()
        self.feed(capture, self.audio, suppressed=True)
        self.feed(capture, np.zeros(16000, dtype=np.float32))
        self.assertTrue(capture.pending.empty())
        self.feed(capture, self.audio)
        self.feed(capture, np.zeros(16000, dtype=np.float32))
        self.assertFalse(capture.pending.empty())

    def test_long_speech_is_split_without_stopping_capture(self):
        capture = self.capture(seconds=3)
        self.feed(capture, np.tile(self.audio, 3))
        self.feed(capture, np.zeros(16000, dtype=np.float32))
        self.assertGreaterEqual(capture.pending.qsize(), 2)
        self.assertLessEqual(capture.pending.qsize(), 3)


class GuardTests(unittest.TestCase):
    def test_error_releases_guard_but_keeps_echo_tail(self):
        guard = call.PlaybackGuard()
        with patch.object(call, 'perf_counter', return_value=10.0):
            with self.assertRaises(ValueError):
                with guard.hold():
                    self.assertTrue(guard.blocked())
                    raise ValueError('output disconnected')
            self.assertTrue(guard.blocked())
        with patch.object(call, 'perf_counter', return_value=11.0):
            self.assertFalse(guard.blocked())


if __name__ == '__main__':
    unittest.main()
