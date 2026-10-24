"""Bounded writes for the pinned SoundCard 0.4.5 Windows backend."""
import time
import numpy as np


def write_audio(player, samples, stop, copy_bytes=None):
    """Submit every sample once; release only frames actually copied to WASAPI."""
    if copy_bytes is None:
        from soundcard.mediafoundation import _ffi
        copy_bytes = _ffi.memmove
    pcm = np.ascontiguousarray(samples, dtype=np.float32)
    if pcm.ndim != 2 or pcm.shape[1] != 2:
        raise ValueError("Playback requires stereo frames")
    cursor = 0
    while cursor < len(pcm) and not stop.is_set():
        available = int(player._render_available_frames())
        count = min(max(0, available), len(pcm) - cursor)
        if not count:
            stop.wait(.001)
            continue
        block = pcm[cursor:cursor + count].tobytes()
        target = player._render_buffer(count)
        copy_bytes(target[0], block, len(block))
        player._render_release(count)
        cursor += count
    return cursor
