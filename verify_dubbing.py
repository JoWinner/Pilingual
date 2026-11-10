"""Run both real offline pipelines with synthetic speech; never use audio hardware."""

import sys
import time
from unittest.mock import patch

import numpy as np

import airpods_call as call
import airpods_translate as speech


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    fixtures = {}
    for device, target, text in [
        (101, 'en', 'Where is the train station?'),
        (103, 'zh', '你好，我的朋友。'),
    ]:
        _, _, path = speech.prepare(target, prefer_cached=True)
        pcm, rate = speech.synthesize(text, speech.load_voice(path))
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        audio = np.interp(np.arange(int(len(audio) * 16000 / rate)) * rate / 16000,
                          np.arange(len(audio)), audio).astype(np.float32)
        fixtures[device] = (np.concatenate([
            np.zeros(4000), audio, np.zeros(24000),
        ]) * 32767).astype('<i2').tobytes()

    class Input:
        def __init__(self, device):
            self.data = fixtures[device]
            self.offset = 0
            self.began = time.monotonic()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, frames):
            if time.monotonic() - self.began > 50:
                raise RuntimeError('Integration test timed out')
            time.sleep(frames / 16000)
            chunk = self.data[self.offset:self.offset + frames * 2]
            self.offset += frames * 2
            return chunk.ljust(frames * 2, b'\0'), False

    results = {}

    def output(audio, rate, device, stopped, guard=None):
        assert device in (102, 104), device
        assert len(audio) > rate // 2
        assert np.max(np.abs(np.frombuffer(audio, dtype='<i2').astype(float))) > 100
        assert (guard is not None) == (device == 104)
        results[device] = (len(audio), rate)
        if len(results) == 2:
            stopped.set()
        return time.perf_counter()

    with patch.object(call, 'open_input', side_effect=lambda device, settings: Input(device)), \
            patch.object(call, 'play', side_effect=output):
        call.run_call(call.Routes(101, 102, 103, 104))
    assert set(results) == {102, 104}, results
    print('PASS: both real model pipelines ran concurrently and produced speech on '
          'separate destinations. No hardware audio was recorded or played.')


if __name__ == '__main__':
    main()
