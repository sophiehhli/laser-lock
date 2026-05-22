#!/usr/bin/env python3
"""
lock.py — PI frequency lock: WS6-200 wavemeter → Toptica DLC Pro piezo.

Reads the laser frequency from the wavemeter acquisition thread, runs a
discrete-time PI controller, and writes piezo voltage corrections to the
DLC Pro via USB serial using the Toptica Python Laser SDK.

Requires: pip install "toptica-lasersdk>=3.1.0"
DLC Pro firmware 2.7.2 is explicitly supported by SDK 3.1.0.

Tuning guidance (derived from Allan deviation data, τ_min ≈ 2.37 s):
  --rate  : keep at or below ~0.5 Hz to avoid chasing white noise
  --ki    : integrator time constant τ_i = Kp/Ki should be 2–10 s
  --kp    : start small (0.0005 V/MHz); increase until you see clean correction
  Start with --dry-run to verify the error signal before touching the laser.

Usage examples
--------------
# Dry run — verifies error signal, prints corrections, does NOT touch laser:
    python lock.py --setpoint 298.0044 --dry-run

# Live lock via ethernet (DLC Pro IP visible at bottom of DLC Pro software):
    python lock.py --host 192.168.100.2 --setpoint 298.0044

# Custom gains and log:
    python lock.py --host 192.168.100.2 --setpoint 298.0044 --kp 0.0005 --ki 0.002 --log

# Simulation + dry-run (for testing the full pipeline on macOS):
    python lock.py --setpoint 298.0044 --debug --dry-run
"""

import sys
import time
import signal
import argparse
import csv
from datetime import datetime
from pathlib import Path

from acquisition import Acquisition

# ---------------------------------------------------------------------------
# Toptica SDK import — requires toptica-lasersdk >= 3.1.0
# DLC pro firmware 2.7.2 → module toptica.lasersdk.dlcpro.v2_7_2
# ---------------------------------------------------------------------------
try:
    from toptica.lasersdk.dlcpro.v2_7_2 import DLCpro, NetworkConnection
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


# ---------------------------------------------------------------------------
# PI controller with integrator anti-windup
# ---------------------------------------------------------------------------

class PIController:
    """
    Discrete-time PI controller with clamp-based anti-windup.

    Parameters
    ----------
    kp : float
        Proportional gain [V / MHz].
    ki : float
        Integral gain [V / (MHz·s)].
    dt : float
        Nominal update interval [s].
    v_min, v_max : float
        Output clamp limits [V].
    """

    def __init__(self, kp: float, ki: float, dt: float,
                 v_min: float, v_max: float):
        self.kp    = kp
        self.ki    = ki
        self.dt    = dt
        self.v_min = v_min
        self.v_max = v_max
        self._integrator = 0.0  # [V]

    def update(self, error_MHz: float, dt: float = None) -> tuple:
        """
        Compute new output voltage given the current frequency error.

        Parameters
        ----------
        error_MHz : float
            f_measured - f_setpoint in MHz. Positive means laser is too high.
        dt : float or None
            Actual elapsed time since last call [s]. Uses nominal dt if None.

        Returns
        -------
        v_out : float
            Clamped output voltage [V].
        integrator : float
            Current integrator state [V] (log / diagnostic use).
        saturated : bool
            True if the output hit a clamp limit.
        """
        step = dt if dt is not None else self.dt

        p_term        = self.kp * error_MHz
        new_integrator = self._integrator + self.ki * error_MHz * step
        v_raw          = p_term + new_integrator

        saturated = not (self.v_min < v_raw < self.v_max)
        if saturated:
            # Clamp output, freeze integrator (anti-windup)
            v_out = max(self.v_min, min(self.v_max, v_raw))
        else:
            self._integrator = new_integrator
            v_out = v_raw

        return v_out, self._integrator, saturated

    def reset(self, v_init: float = 0.0):
        """Pre-load integrator to a known voltage (e.g. current piezo voltage)."""
        self._integrator = v_init


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="PI frequency lock: WS6-200 wavemeter → DLC Pro piezo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--host", type=str, default=None, metavar="HOST",
        help="DLC Pro hostname or IP address (e.g. 192.168.100.2). "
             "Required unless --dry-run is used.",
    )
    p.add_argument(
        "--setpoint", type=float, required=True, metavar="THz",
        help="Target laser frequency in THz.",
    )
    p.add_argument(
        "--channel", type=int, default=1,
        help="Wavemeter channel (1-based).",
    )
    p.add_argument(
        "--kp", type=float, default=0.0005, metavar="V/MHz",
        help="Proportional gain. Start small and increase slowly.",
    )
    p.add_argument(
        "--ki", type=float, default=0.002, metavar="V/(MHz·s)",
        help="Integral gain. τ_i = Kp/Ki should be 2–10 s initially.",
    )
    p.add_argument(
        "--rate", type=float, default=0.5, metavar="Hz",
        help="PI update rate in Hz. Keep at or below 0.5 Hz to avoid "
             "chasing white noise (Allan minimum τ ≈ 2.37 s).",
    )
    p.add_argument(
        "--vcenter", type=float, default=0.0, metavar="V",
        help="Starting piezo voltage. Read the current value from the DLC Pro "
             "front panel before running and pass it here.",
    )
    p.add_argument(
        "--vrange", type=float, default=1.0, metavar="V",
        help="Allowed piezo excursion ±V around --vcenter. "
             "Default is ±1 V — increase only once you are confident the lock is working.",
    )
    p.add_argument(
        "--log", action="store_true", default=False,
        help="Write lock telemetry to logs/YYYY-MM-DD/lock_HH-MM-SS.csv.",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Compute and display corrections but do NOT write to the DLC Pro. "
             "Use this first to verify gains and error signal.",
    )
    p.add_argument(
        "--exposure", type=int, default=None, metavar="MS",
        help="Set wavemeter manual exposure time in ms.",
    )
    p.add_argument(
        "--debug", action="store_true", default=False,
        help="Force wavemeter simulation mode (auto on non-Windows).",
    )
    p.add_argument(
        "--dll", type=str, default=None, metavar="PATH",
        help="Custom path to wlmData.dll (Windows only).",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if not args.dry_run and args.host is None:
        print("ERROR: --host is required unless --dry-run is used.")
        sys.exit(1)

    if not args.dry_run and not _SDK_AVAILABLE:
        print(
            "ERROR: toptica-lasersdk is not installed.\n"
            '  Install with: pip install "toptica-lasersdk>=3.1.0"'
        )
        sys.exit(1)

    dt    = 1.0 / args.rate
    v_min = args.vcenter - args.vrange
    v_max = args.vcenter + args.vrange

    # -- Wavemeter acquisition -------------------------------------------
    acq = Acquisition(
        channel=args.channel,
        dll_path=args.dll,
        debug=True if args.debug else None,
        exposure_ms=args.exposure,
    )
    acq.start()

    wlm_mode = "SIMULATION" if acq.debug else "LIVE"
    dlc_mode = "DRY RUN — no output" if args.dry_run else f"DLC Pro at {args.host}"
    print(f"\nFrequency lock  |  ch {args.channel}  |  WLM: {wlm_mode}  |  {dlc_mode}")
    print(f"Setpoint        : {args.setpoint:.6f} THz")
    print(f"Gains           : Kp={args.kp} V/MHz   Ki={args.ki} V/(MHz·s)")
    print(f"Piezo range     : [{v_min:.1f}, {v_max:.1f}] V  (center {args.vcenter:.1f} V)")
    print(f"Update rate     : {args.rate} Hz  (Δt = {dt:.2f} s)")
    if args.dry_run:
        print("  *** DRY RUN — laser will not be touched ***")
    print("Press Ctrl+C to stop.\n")

    # -- CSV log ---------------------------------------------------------
    log_file   = None
    csv_writer = None
    if args.log:
        now     = datetime.now()
        log_dir = Path(__file__).parent / "logs" / now.strftime("%Y-%m-%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / now.strftime("lock_%H-%M-%S.csv")
        log_file   = open(log_path, "w", newline="", buffering=1)
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(
            ["time_s", "frequency_THz", "error_MHz",
             "integrator_V", "output_V", "saturated"]
        )
        print(f"Lock log        : {log_path}\n")

    # -- Safety check: verify setpoint is close to actual laser frequency --
    SAFETY_WINDOW_MHz = 30.0
    print(f"Waiting for first wavemeter reading to verify setpoint ...")
    for _ in range(50):          # wait up to ~5 s
        sample = acq.latest
        if sample is not None:
            break
        time.sleep(0.1)

    if sample is None:
        print("ERROR: No wavemeter reading after 5 s. Is the WLM software running?")
        acq.stop()
        sys.exit(1)

    _ts_ns, first_freq = sample
    initial_error_MHz = (first_freq - args.setpoint) * 1e6
    print(f"  Measured frequency : {first_freq:.6f} THz")
    print(f"  Setpoint           : {args.setpoint:.6f} THz")
    print(f"  Initial error      : {initial_error_MHz:+.3f} MHz  (limit ±{SAFETY_WINDOW_MHz:.0f} MHz)\n")

    if abs(initial_error_MHz) > SAFETY_WINDOW_MHz:
        print(
            f"ERROR: Setpoint is {abs(initial_error_MHz):.1f} MHz away from the measured frequency.\n"
            f"  This exceeds the ±{SAFETY_WINDOW_MHz:.0f} MHz safety window — possible typo in --setpoint.\n"
            f"  Measured: {first_freq:.6f} THz  →  try --setpoint {first_freq:.6f}"
        )
        acq.stop()
        sys.exit(1)

    print(f"Safety check passed. Engaging lock.\n")

    # -- PI controller ---------------------------------------------------
    pi = PIController(kp=args.kp, ki=args.ki, dt=dt, v_min=v_min, v_max=v_max)
    pi.reset(args.vcenter)

    # -- Graceful Ctrl+C -------------------------------------------------
    running = [True]
    def _sigint(sig, frame):
        running[0] = False
    signal.signal(signal.SIGINT, _sigint)

    t_start      = time.monotonic()
    last_update  = t_start - dt  # fire on first iteration
    v_out        = args.vcenter

    # -- Inner control loop (shared by dry-run and live) -----------------
    def _run_loop(dlc_or_none):
        nonlocal last_update, v_out

        if dlc_or_none is not None:
            # Pre-load integrator with current piezo voltage so there's
            # no step at lock-on.
            try:
                v_current = dlc_or_none.laser1.dl.pc.voltage_set.get()
                pi.reset(float(v_current))
                print(f"Current piezo voltage: {v_current:.4f} V (pre-loaded into integrator)\n")
            except Exception as e:
                print(f"WARNING: Could not read current piezo voltage: {e}")

        while running[0]:
            now_t   = time.monotonic()
            elapsed = now_t - last_update

            if elapsed < dt:
                time.sleep(min(0.02, dt - elapsed))
                continue

            actual_dt   = elapsed
            last_update = now_t
            t_rel       = now_t - t_start

            sample = acq.latest
            if sample is None:
                time.sleep(0.1)
                continue

            _ts_ns, freq = sample
            error_MHz    = (freq - args.setpoint) * 1e6
            v_out, integrator, saturated = pi.update(error_MHz, dt=actual_dt)

            # Write to DLC Pro
            if dlc_or_none is not None:
                dlc_or_none.laser1.dl.pc.voltage_set.set(v_out)

            sat_str = " [SATURATED]" if saturated else ""
            print(
                f"\r  t={t_rel:7.1f}s  f={freq:.6f} THz  "
                f"err={error_MHz:+7.3f} MHz  "
                f"V={v_out:+7.4f} V  "
                f"I={integrator:+7.4f} V  "
                f"wlm={acq.sample_rate:.0f} Hz{sat_str}   ",
                end="", flush=True,
            )

            if csv_writer is not None:
                csv_writer.writerow([
                    f"{t_rel:.4f}", f"{freq:.9f}", f"{error_MHz:.4f}",
                    f"{integrator:.6f}", f"{v_out:.6f}", int(saturated),
                ])

        print()  # newline after \r

    # -- Connect (or skip) and run ---------------------------------------
    try:
        if args.dry_run:
            _run_loop(None)
        else:
            with DLCpro(NetworkConnection(args.host)) as dlc:
                _run_loop(dlc)
    finally:
        acq.stop()
        if log_file is not None:
            log_file.close()
            print(f"Lock log saved: {log_file.name}")
        print("Lock stopped.")


if __name__ == "__main__":
    main()
