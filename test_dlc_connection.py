#!/usr/bin/env python3
"""
test_dlc_connection.py — Safe DLC Pro ethernet connection test.

Does two things:
  1. READ-ONLY: connects and prints all key laser parameters.
  2. WRITE TEST (optional, --write-test flag): nudges the piezo by +1 mV
     then immediately restores it, confirming read-write round-trip.

The nudge (+1 mV on the piezo) is completely imperceptible to the laser
and is instantly reverted — it is safe to run while the laser is on.

Usage:
    python test_dlc_connection.py --host 192.168.100.2
    python test_dlc_connection.py --host 192.168.100.2 --write-test
"""

import argparse
import sys

try:
    from toptica.lasersdk.dlcpro.v2_7_2 import DLCpro, NetworkConnection
except ImportError:
    print("ERROR: toptica-lasersdk is not installed.")
    print('  Install with: pip install "toptica-lasersdk>=3.1.0"')
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description="DLC Pro ethernet connection test")
    p.add_argument("--host", required=True, metavar="HOST",
                   help="DLC Pro IP address or hostname (e.g. 192.168.100.2)")
    p.add_argument("--write-test", action="store_true", default=False,
                   help="Also perform a safe +1 mV piezo nudge round-trip test.")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"\nConnecting to DLC Pro at {args.host} ...")
    try:
        with DLCpro(NetworkConnection(args.host)) as dlc:
            print("  Connected.\n")

            # ── Read all key parameters ──────────────────────────────────────
            cc_set  = dlc.laser1.dl.cc.current_set.get()
            cc_act  = dlc.laser1.dl.cc.current_act.get()
            tc_set  = dlc.laser1.dl.tc.temp_set.get()
            tc_act  = dlc.laser1.dl.tc.temp_act.get()
            pc_set  = dlc.laser1.dl.pc.voltage_set.get()
            pc_act  = dlc.laser1.dl.pc.voltage_act.get()

            print("── Current Laser State ─────────────────────────────────────")
            print(f"  Current (set / actual) : {cc_set:.3f} mA  /  {cc_act:.3f} mA")
            print(f"  Temperature (set / act): {tc_set:.3f} °C  /  {tc_act:.3f} °C")
            print(f"  Piezo (set / actual)   : {pc_set:.4f} V   /  {pc_act:.4f} V")
            print("────────────────────────────────────────────────────────────\n")

            # ── Optional write test ──────────────────────────────────────────
            if args.write_test:
                nudge_V = 0.001   # 1 mV — completely imperceptible
                target  = round(pc_set + nudge_V, 6)

                print(f"Write test: nudging piezo {pc_set:.4f} V → {target:.4f} V (+1 mV) ...")
                dlc.laser1.dl.pc.voltage_set.set(target)
                readback = dlc.laser1.dl.pc.voltage_set.get()
                print(f"  Readback after nudge  : {readback:.4f} V")

                print(f"  Restoring original    : {pc_set:.4f} V ...")
                dlc.laser1.dl.pc.voltage_set.set(pc_set)
                readback2 = dlc.laser1.dl.pc.voltage_set.get()
                print(f"  Readback after restore: {readback2:.4f} V")

                ok = abs(readback - target) < 0.001 and abs(readback2 - pc_set) < 0.001
                print()
                if ok:
                    print("  PASS — write/read round-trip successful.")
                else:
                    print("  FAIL — readback did not match written value. Check connection.")

    except ConnectionRefusedError:
        print(f"\nERROR: Connection refused by {args.host}.")
        print("  Check that the DLC Pro software is running and network is reachable.")
        sys.exit(1)
    except OSError as e:
        print(f"\nERROR: {e}")
        print("  Is the IP address correct? Can you ping the DLC Pro?")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
