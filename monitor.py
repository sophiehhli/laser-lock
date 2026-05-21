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

# With CSV logging:
    python monitor.py --setpoint 384.2300 --log run_001.csv

# Force simulation on Windows for testing:
    python monitor.py --debug
"""

import sys
import time
import argparse
import csv

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
        "--log", type=str, default=None, metavar="FILE",
        help="Write timestamped CSV log to this file",
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
    )
    acq.start()

    mode_str = "SIMULATION" if acq.debug else "LIVE"
    print(f"WS6-200 monitor  |  channel {args.channel}  |  {mode_str}")
    if args.setpoint is not None:
        print(f"Setpoint : {args.setpoint:.6f} THz")
    print("Press Ctrl+C to stop.\n")

    # --- CSV setup -------------------------------------------------------
    log_file   = None
    csv_writer = None
    if args.log:
        log_file   = open(args.log, "w", newline="", buffering=1)  # line-buffered
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(["time_ns", "frequency_THz"])

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

            # Write to CSV every sample
            if csv_writer is not None:
                csv_writer.writerow([ts, f"{freq:.9f}"])

            # Build display line
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
            print(f"Log saved to: {args.log}")
        print("Done.")


if __name__ == "__main__":
    main()
