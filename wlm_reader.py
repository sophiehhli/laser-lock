"""
wlm_reader.py — Thin single-channel wrapper for the HighFinesse WS6-200 wlmData.dll.

On Windows the real DLL is loaded; on any other platform (macOS, Linux) debug/simulation
mode is activated automatically, producing a realistic 1.8 kHz synthetic signal so the
full pipeline can be developed and tested without the hardware.
"""

import sys
import math
import random
import time

# ctypes is stdlib on all platforms; WinDLL only exists on Windows.
import ctypes

PLATFORM_WINDOWS = sys.platform == "win32"

# WLM DLL error sentinels (GetFrequencyNum returns these on failure)
WLM_ERR_NO_SIGNAL   = -1.0
WLM_ERR_BAD_SIGNAL  = -2.0
WLM_ERR_LOW_SIGNAL  = -3.0
WLM_ERR_BIG_SIGNAL  = -4.0

_SIM_FREQ_THz       = 298.0044   # ~1006 nm, change to match your laser
_SIM_NOISE_AMP_THz  = 1e-6       # 1 MHz RMS noise
_SIM_DRIFT_AMP_THz  = 5e-7       # slow 0.1 Hz sinusoidal drift
_SIM_RATE_HZ        = 1800.0     # simulated measurement rate (matches hardware max)
_SIM_INTERVAL_S     = 1.0 / _SIM_RATE_HZ


class WavemeterReader:
    """
    Single-channel frequency reader for the HighFinesse WS6-200.

    Parameters
    ----------
    channel : int
        Wavemeter channel number (1-based). Default 1.
    dll_path : str or None
        Full path to wlmData.dll. Defaults to the standard Windows system location.
    debug : bool or None
        True  → always simulate.
        False → always try to load the DLL (will raise on non-Windows).
        None  → auto-detect: simulate on non-Windows, real DLL on Windows.
    """

    DEFAULT_DLL_PATH = r"C:\Windows\System32\wlmData.dll"

    def __init__(self, channel: int = 1, dll_path: str = None, debug: bool = None,
                 exposure_ms: int = None):
        self.channel = channel
        self._exposure_ms = exposure_ms  # None = leave auto-exposure as-is

        # Determine mode
        if debug is None:
            self._debug = not PLATFORM_WINDOWS
        else:
            self._debug = bool(debug)

        if not self._debug:
            self._init_dll(dll_path or self.DEFAULT_DLL_PATH)
        else:
            self._init_simulation()

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_dll(self, path: str):
        if not PLATFORM_WINDOWS:
            raise RuntimeError(
                "wlmData.dll requires Windows. Pass debug=True or run on Windows."
            )
        self._dll = ctypes.WinDLL(path)

        # Set return types explicitly to avoid silent truncation
        self._dll.GetFrequencyNum.restype    = ctypes.c_double
        self._dll.GetFrequencyNum.argtypes   = [ctypes.c_long, ctypes.c_double]
        self._dll.GetSwitcherMode.restype    = ctypes.c_long
        self._dll.GetSwitcherMode.argtypes   = [ctypes.c_long]
        self._dll.SetSwitcherMode.restype    = ctypes.c_long
        self._dll.SetSwitcherMode.argtypes   = [ctypes.c_long]
        self._dll.SetSwitcherChannel.restype  = ctypes.c_long
        self._dll.SetSwitcherChannel.argtypes = [ctypes.c_long]
        self._dll.GetExposureNum.restype  = ctypes.c_long
        self._dll.GetExposureNum.argtypes = [ctypes.c_long, ctypes.c_long]
        self._dll.SetExposureNum.restype  = ctypes.c_long
        self._dll.SetExposureNum.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_long]
        self._dll.GetExposureMode.restype  = ctypes.c_long
        self._dll.GetExposureMode.argtypes = [ctypes.c_long]
        self._dll.SetExposureMode.restype  = ctypes.c_long
        self._dll.SetExposureMode.argtypes = [ctypes.c_long]

        # Disable multi-channel switching — required to reach 1.8 kHz on one channel
        self._dll.SetSwitcherMode(ctypes.c_long(0))
        # Pin the switcher to exactly our channel
        self._dll.SetSwitcherChannel(ctypes.c_long(self.channel))

        # Verify the calls actually took effect (WLM software can override them)
        actual_mode = int(self._dll.GetSwitcherMode(ctypes.c_long(0)))
        actual_ch   = int(self._dll.GetSwitcherChannel(ctypes.c_long(0)))
        if actual_mode != 0:
            print(
                f"WARNING: SetSwitcherMode(0) did not take effect "
                f"(GetSwitcherMode returned {actual_mode}). "
                f"Disable multi-channel switching in the WLM software UI to reach 1.8 kHz."
            )
        if actual_ch != self.channel:
            print(
                f"WARNING: SetSwitcherChannel({self.channel}) did not take effect "
                f"(GetSwitcherChannel returned {actual_ch})."
            )

        # Exposure setup
        current_exp = int(self._dll.GetExposureNum(ctypes.c_long(self.channel), ctypes.c_long(1)))
        if self._exposure_ms is not None:
            # Switch to manual exposure and set requested time
            self._dll.SetExposureMode(ctypes.c_long(0))  # 0 = manual
            self._dll.SetExposureNum(
                ctypes.c_long(self.channel), ctypes.c_long(1),
                ctypes.c_long(self._exposure_ms)
            )
            actual_exp = int(self._dll.GetExposureNum(ctypes.c_long(self.channel), ctypes.c_long(1)))
            print(f"Exposure         : {actual_exp} ms (manual, was {current_exp} ms)")
        else:
            mode = int(self._dll.GetExposureMode(ctypes.c_long(0)))
            mode_str = "auto" if mode else "manual"
            print(f"Exposure         : {current_exp} ms ({mode_str})")
            if current_exp > 50:
                print(
                    f"NOTE: Exposure is {current_exp} ms → max rate ~{1000/current_exp:.0f} Hz. "
                    f"Use --exposure <ms> to set a shorter time (needs sufficient signal power)."
                )

    def _init_simulation(self):
        """Set up state for the synthetic 1.8 kHz signal source."""
        self._sim_t0           = time.perf_counter()
        self._sim_last_update  = self._sim_t0 - _SIM_INTERVAL_S  # trigger immediately
        self._sim_last_freq    = _SIM_FREQ_THz

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_frequency(self) -> float:
        """
        Return the current frequency in THz for the configured channel.

        On real hardware the DLL returns the same value until a new measurement
        is ready; the acquisition loop detects this via equality comparison.
        The simulation replicates this behaviour — same value is returned while
        the synthetic 556 µs measurement window has not yet elapsed.

        Returns negative sentinels (WLM_ERR_*) on hardware errors.
        """
        if not self._debug:
            return self._dll.GetFrequencyNum(
                ctypes.c_long(self.channel), ctypes.c_double(0)
            )
        return self._sim_get_frequency()

    def _sim_get_frequency(self) -> float:
        now = time.perf_counter()
        if (now - self._sim_last_update) < _SIM_INTERVAL_S:
            return self._sim_last_freq  # no new measurement yet

        t     = now - self._sim_t0
        noise = random.gauss(0, _SIM_NOISE_AMP_THz)
        drift = _SIM_DRIFT_AMP_THz * math.sin(2.0 * math.pi * 0.1 * t)
        freq  = _SIM_FREQ_THz + drift + noise

        self._sim_last_freq   = freq
        self._sim_last_update = now
        return freq

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def switcher_channel(self) -> int:
        """Active switcher channel reported by the hardware (1-based). Always returns
        self.channel in simulation mode."""
        if not self._debug:
            return int(self._dll.GetSwitcherChannel(ctypes.c_long(0)))
        return self.channel

    @property
    def switcher_mode(self) -> int:
        """0 = single-channel (switching disabled), 1 = multi-channel switching.
        Always returns 0 in simulation mode."""
        if not self._debug:
            return int(self._dll.GetSwitcherMode(ctypes.c_long(0)))
        return 0

    @property
    def debug(self) -> bool:
        return self._debug
