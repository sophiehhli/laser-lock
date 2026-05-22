#!/usr/bin/env python3
"""
monitor.py — Live terminal frequency reporter for the HighFinesse WS6-200.

Displays frequency, deviation from setpoint, and achieved sample rate.
Optionally logs timestamped data to CSV.  Press Ctrl+C to stop cleanly.

Usage examples
--------------
# Simulation (auto on macOS/Linux):
    python monitor.py

# Specify channel and setpoint on Windows:
    python monitor.py --channel 1 --setpoint 384.2300

# Auto-named CSV log saved to logs/YYYY-MM-DD/wlm_HH-MM-SS.csv:
    python monitor.py --setpoint 384.2300 --log

# Custom CSV log filename (still saved inside logs/YYYY-MM-DD/):
    python monitor.py --setpoint 384.2300 --log my_run.csv

# Force simulation on Windows for testing:
    python monitor.py --debug
"""

import sys
import os
import time
import argparse
import csv
from datetime import datetime
from pathlib import Path

from acquisition import Acquisition


def parse_args():
    p = argparse.ArgumentParser(
        description="WS6-200 single-channel frequency monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--channel", type=int, default=1,
        help="Wavemeter channel to monitor (1-based)",
    )
    p.add_argument(
        "--setpoint", type=float, default=None, metavar="THz",
        help="Frequency setpoint in THz; enables deviation display",
    )
    p.add_argument(
        "--log", nargs="?", const="_auto", default=None, metavar="FILE",
        help="Write timestamped CSV log. Omit FILE for an auto-generated name"
             " (e.g. wlm_2026-05-20_14-32-05.csv).",
    )
    p.add_argument(
        "--exposure", type=int, default=None, metavar="MS",
        help="Set a fixed manual exposure time in ms (e.g. 1). "
             "Shorter = faster rate, but needs sufficient signal power. "
             "Omit to leave the wavemeter in auto-exposure mode.",
    )
    p.add_argument(
        "--debug", action="store_true", default=False,
        help="Force simulation mode (auto on non-Windows)",
    )
    p.add_argument(
        "--dll", type=str, default=None, metavar="PATH",
        help="Custom path to wlmData.dll (Windows only)",
    )
    return p.parse_args()


def main():
    args = parse_args()

    # None → auto-detect platform; True → force simulation
    debug_flag = True if args.debug else None

    acq = Acquisition(
        channel=args.channel,
        dll_path=args.dll,
        debug=debug_flag,
        exposure_ms=args.exposure,
    )
    acq.start()

    mode_str = "SIMULATION" if acq.debug else "LIVE"
    print(f"WS6-200 monitor  |  channel {args.channel}  |  {mode_str}")

    if not acq.debug:
        sw_mode = acq._reader.switcher_mode
        sw_ch   = acq._reader.switcher_channel
        mode_label = "SINGLE-CHANNEL (OK)" if sw_mode == 0 else "MULTI-CHANNEL (WARNING: rate will be reduced)"
        print(f"Switcher mode    : {mode_label}")
        print(f"Switcher channel : {sw_ch}" + ("" if sw_ch == args.channel else f"  ← WARNING: expected {args.channel}"))

    if args.setpoint is not None:
        print(f"Setpoint         : {args.setpoint:.6f} THz")
    print("Press Ctrl+C to stop.\n")

    # --- CSV setup -------------------------------------------------------
    log_file   = None
    csv_writer = None
    log_path   = None
    if args.log is not None:
        now      = datetime.now()
        log_dir  = Path(__file__).parent / "logs" / now.strftime("%Y-%m-%d")
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            now.strftime("wlm_%H-%M-%S.csv")
            if args.log == "_auto"
            else args.log
        )
        log_path = log_dir / filename
        print(f"Logging to : {log_path}")
        log_file   = open(log_path, "w", newline="", buffering=1)  # line-buffered
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(["time_ns", "frequency_THz"])
        # Register callback so every sample is written at full acquisition rate,
        # not just the subset visible to the display loop.
        acq.set_log_callback(
            lambda ts, freq: csv_writer.writerow([ts, f"{freq:.9f}"])
        )

    # --- Display loop ----------------------------------------------------
    try:
        last_ts = None
        while True:
            sample = acq.latest

            # No new data yet — back off briefly to avoid busy-spinning
            # the display loop (the acquisition thread is still flat-out).
            if sample is None or sample[0] == last_ts:
                time.sleep(0.0005)
                continue

            ts, freq = sample
            last_ts  = ts

            # Build display line (CSV is written via callback at full rate)
            rate = acq.sample_rate
            line = (
                f"\r  Freq: {freq:.6f} THz"
                f"  |  Rate: {rate:6.1f} Hz"
            )
            if args.setpoint is not None:
                dev_MHz = (freq - args.setpoint) * 1e6  # THz → MHz
                line += f"  |  Dev: {dev_MHz:+9.3f} MHz"

            sys.stdout.write(line)
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\nStopping acquisition...")

    finally:
        acq.stop()
        if log_file is not None:
            log_file.flush()
            log_file.close()
            print(f"Log saved to: {log_path}")
        print("Done.")


if __name__ == "__main__":
    main()
