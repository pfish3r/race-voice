import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import threading
import unittest
import numpy as np
from playback import write_audio


class Device:
    def __init__(self, available):
        self.available = iter(available)
        self.reserved = 0
        self.blocks = []
    def _render_available_frames(self):
        return next(self.available, 2048)
    def _render_buffer(self, count):
        self.reserved = count
        return [self]
    def copy(self, target, block, size):
        assert target is self
        assert size == self.reserved * 8
        self.current = np.frombuffer(block, dtype=np.float32).reshape(-1, 2).copy()
    def _render_release(self, count):
        assert count == self.reserved == len(self.current)
        self.blocks.append(self.current)


class PlaybackTests(unittest.TestCase):
    def test_partial_writes_preserve_entire_long_announcement(self):
        samples = np.linspace(-.8, .8, 48000 * 9 * 2 + 246, dtype=np.float32).reshape(-1, 2)
        device = Device([0, 127, 4096, 3, 0, 480, 960, 2000])
        stop = threading.Event()
        for offset in range(0, len(samples), 960):
            block = samples[offset:offset + 960]
            self.assertEqual(write_audio(device, block, stop, device.copy), len(block))
        np.testing.assert_array_equal(np.concatenate(device.blocks), samples)

    def test_stop_unblocks_full_device(self):
        device = Device([0])
        stop = threading.Event()
        stop.set()
        self.assertEqual(write_audio(device, np.ones((960, 2)), stop, device.copy), 0)
        self.assertEqual(device.blocks, [])


if __name__ == "__main__":
    unittest.main()
