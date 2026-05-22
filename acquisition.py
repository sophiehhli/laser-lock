"""
acquisition.py — High-rate single-channel frequency acquisition thread.

Runs a tight polling loop in a daemon thread, detects each new measurement from
the WS6-200 via equality comparison (the DLL holds the previous value until a new
measurement is ready), timestamps it with nanosecond resolution, and stores it in a
lock-free ring buffer.  Thread-safe for a single producer / single consumer.
"""

import time
import threading
import collections
from typing import Optional, Tuple

from wlm_reader import WavemeterReader, WLM_ERR_NO_SIGNAL

# Measurement buffer: ~6 seconds at 1.8 kHz.  Oldest samples are silently discarded.
DEFAULT_BUFFER_SIZE = 12_000


class Acquisition:
    """
    Continuously reads frequency data from one wavemeter channel.

    Parameters
    ----------
    channel : int
        Wavemeter channel (1-based).
    buffer_size : int
        Maximum number of (timestamp_ns, frequency_THz) samples kept in RAM.
    dll_path : str or None
        Custom path to wlmData.dll (Windows only).
    debug : bool or None
        Passed directly to WavemeterReader — None means auto-detect platform.
    """

    def __init__(
        self,
        channel: int = 1,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        dll_path: str = None,
        debug: bool = None,
        exposure_ms: int = None,
    ):
        self._reader  = WavemeterReader(channel=channel, dll_path=dll_path, debug=debug,
                                        exposure_ms=exposure_ms)
        self._buffer  = collections.deque(maxlen=buffer_size)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._log_callback = None  # called with (ts_ns, freq_THz) for every new sample

        # Rate tracking — protected by a simple lock (written rarely, read rarely)
        self._rate_lock    = threading.Lock()
        self._current_rate = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, name="wlm-acquisition", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        """Signal the polling thread to stop and wait for it to finish."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def set_log_callback(self, fn):
        """Register a callable fn(ts_ns: int, freq_THz: float) that will be
        invoked in the acquisition thread for every new measurement.
        Called at the full hardware rate (~1.8 kHz) — keep it fast.
        Pass None to unregister."""
        self._log_callback = fn

    # ------------------------------------------------------------------
    # Polling loop (runs in daemon thread)
    # ------------------------------------------------------------------

    def _poll_loop(self):
        last_freq = None

        # Rate-tracking state
        rate_t0    = time.perf_counter()
        rate_count = 0

        while self._running:
            freq = self._reader.get_frequency()

            # Skip hardware error returns
            if freq < 0.0:
                continue

            # Skip duplicates — DLL returns the same value until a new
            # measurement completes; equality check is the fastest gate.
            if freq == last_freq:
                continue

            last_freq = freq
            ts = time.perf_counter_ns()
            self._buffer.append((ts, freq))

            # Fire log callback on every new sample (used for full-rate CSV writing)
            if self._log_callback is not None:
                self._log_callback(ts, freq)

            # Update rolling sample rate once per second
            rate_count += 1
            now     = time.perf_counter()
            elapsed = now - rate_t0
            if elapsed >= 1.0:
                with self._rate_lock:
                    self._current_rate = rate_count / elapsed
                rate_t0    = now
                rate_count = 0

    # ------------------------------------------------------------------
    # Consumer API
    # ------------------------------------------------------------------

    @property
    def latest(self) -> Optional[Tuple[int, float]]:
        """
        The most recent (timestamp_ns, frequency_THz) sample, or None if
        no data has been collected yet.

        deque[-1] is atomic in CPython under the GIL — no lock needed for
        single-consumer access.
        """
        try:
            return self._buffer[-1]
        except IndexError:
            return None

    def mean_since(self, since_ns: int) -> Optional[Tuple[int, float]]:
        """
        Average all samples collected strictly after `since_ns`.

        Returns (latest_ts_ns, mean_freq_THz), or None if no new samples exist.
        Use this in slow control loops (e.g. 0.5 Hz PI) so that every WLM
        reading collected at the full hardware rate (~40 Hz) contributes to
        each control decision rather than all but the last being discarded.
        """
        # Snapshot under GIL — safe for single-consumer deque access
        samples = [(ts, f) for ts, f in list(self._buffer) if ts > since_ns]
        if not samples:
            return None
        return samples[-1][0], sum(f for _, f in samples) / len(samples)

    @property
    def buffer(self) -> collections.deque:
        """Read-only view of the ring buffer (newest item is at the right)."""
        return self._buffer

    @property
    def sample_rate(self) -> float:
        """Achieved sample rate in Hz, averaged over the most recent second."""
        with self._rate_lock:
            return self._current_rate

    @property
    def debug(self) -> bool:
        return self._reader.debug

    @property
    def channel(self) -> int:
        return self._reader.channel
