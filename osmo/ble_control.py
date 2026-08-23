"""Live BLE test harness for the Osmo Pocket 3.

Run with USB webcam mode active, to confirm the two coexist.

    .venv\\Scripts\\python ble_control.py pair            # connect + pair
    .venv\\Scripts\\python ble_control.py telemetry       # stream gimbal angles
    .venv\\Scripts\\python ble_control.py lock            # try gimbal lock (unproven)
    .venv\\Scripts\\python ble_control.py watch --seconds 30

Nothing here asserts success -- it reports what the camera actually did, so a
silently-ignored command shows up as "no telemetry change" rather than a false
"ok".
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from camrig.ble import OsmoBle, find_osmo
from camrig import duml


async def cmd_pair(args):
    async with OsmoBle(address=args.address) as osmo:
        print(f"connected to {osmo.state.name or osmo.address}")
        ok = await osmo.pair(timeout=args.seconds)
        if ok:
            print("PAIRED")
        else:
            print("pairing not confirmed -- if the camera showed a prompt, "
                  "approve it and re-run; some firmware pairs silently")
        await asyncio.sleep(1.0)
        print("state:", osmo.state.as_dict())


async def cmd_telemetry(args):
    async with OsmoBle(address=args.address) as osmo:
        print(f"connected to {osmo.state.name or osmo.address}; pairing...")
        await osmo.pair(timeout=args.seconds)
        print(f"watching gimbal telemetry for {args.seconds:.0f}s "
              f"(move the camera by hand to see it change)\n")
        end = asyncio.get_event_loop().time() + args.seconds
        last = None
        while asyncio.get_event_loop().time() < end:
            g = osmo.state.gimbal
            now = (round(g.pitch, 1), round(g.roll, 1), round(g.yaw, 1), g.mode)
            if now != last:
                print(f"  pitch={g.pitch:7.1f}  roll={g.roll:7.1f}  "
                      f"yaw={g.yaw:7.1f}  mode={g.mode}")
                last = now
            await asyncio.sleep(0.1)
        print(f"\n{osmo.state.messages_in} DUML messages received")
        if osmo.state.messages_in == 0:
            print("no inbound DUML at all -- pairing likely did not complete")


async def cmd_mode(args, mode: int, label: str):
    async with OsmoBle(address=args.address) as osmo:
        print(f"connected; pairing...")
        await osmo.pair(timeout=args.seconds)
        g0 = (osmo.state.gimbal.pitch, osmo.state.gimbal.roll, osmo.state.gimbal.yaw)
        mode0 = osmo.state.gimbal.mode
        print(f"gimbal mode before: {mode0}")
        print(f"sending set_mode({label}={mode}) ...")
        await osmo.set_gimbal_mode(mode)
        await asyncio.sleep(2.0)
        mode1 = osmo.state.gimbal.mode
        print(f"gimbal mode after:  {mode1}")
        if mode1 == mode:
            print(f"CONFIRMED: mode changed to {label} over BLE")
        elif mode1 != mode0:
            print(f"mode changed to {mode1}, not the requested {mode}")
        else:
            print("no change -- consistent with the known 'BLE motor commands "
                  "ignored without an active Wi-Fi session' behaviour")


async def cmd_watch(args):
    addr, name = await find_osmo(timeout=args.seconds)
    if addr:
        print(f"Osmo advertising: {name} @ {addr}")
    else:
        print("no Osmo seen over BLE")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action",
                    choices=["pair", "telemetry", "lock", "follow", "fpv", "watch"])
    ap.add_argument("--address", help="skip scanning, use this BLE address")
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    actions = {
        "pair": cmd_pair,
        "telemetry": cmd_telemetry,
        "watch": cmd_watch,
        "lock": lambda a: cmd_mode(a, duml.GIMBAL_MODE_LOCK, "lock"),
        "follow": lambda a: cmd_mode(a, duml.GIMBAL_MODE_FOLLOW, "follow"),
        "fpv": lambda a: cmd_mode(a, duml.GIMBAL_MODE_FPV, "fpv"),
    }
    asyncio.run(actions[args.action](args))


if __name__ == "__main__":
    main()
