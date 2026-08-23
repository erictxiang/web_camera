"""Is the Pocket 3 visible over BLE, and does it expose the DUML service?

The go/no-go question for BLE control: if the camera stops advertising while
USB webcam mode is active, the sidecar idea collapses. Run this WHILE the
camera is streaming over USB.

    .venv\\Scripts\\python ble_scan.py            # scan and identify
    .venv\\Scripts\\python ble_scan.py --probe    # also connect and list GATT
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

# lib-osmo-ble: the DUML service on the Pocket 3.
DUML_SERVICE = "fff0"
INTERESTING = ("osmo", "dji", "pocket")


def looks_like_osmo(name: str | None, mfr: dict) -> bool:
    if name and any(k in name.lower() for k in INTERESTING):
        return True
    # DJI's Bluetooth SIG company identifier.
    return 0x08EA in mfr


async def scan(seconds: float):
    print(f"scanning {seconds:.0f}s ...")
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for device, adv in found.values():
        rows.append(
            {
                "address": device.address,
                "name": adv.local_name or device.name,
                "rssi": adv.rssi,
                "services": [u[4:8] for u in adv.service_uuids],
                "mfr": adv.manufacturer_data,
                "osmo": looks_like_osmo(adv.local_name or device.name,
                                        adv.manufacturer_data),
            }
        )
    rows.sort(key=lambda r: (not r["osmo"], -(r["rssi"] or -200)))
    return rows


async def probe(address: str):
    print(f"\nconnecting to {address} ...")
    async with BleakClient(address, timeout=20.0) as client:
        print(f"connected: {client.is_connected}")
        for service in client.services:
            print(f"  service {service.uuid}")
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                print(f"    char {ch.uuid}  [{props}]")


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--probe", action="store_true",
                    help="connect to the Osmo and enumerate GATT services")
    ap.add_argument("--address", help="skip scanning, probe this address")
    args = ap.parse_args()

    target = args.address
    if not target:
        rows = await scan(args.seconds)
        if not rows:
            print("no BLE devices at all -- is the radio on?")
            return 1
        print(f"\n{'addr':>18} {'rssi':>5}  {'osmo?':>5}  name / services")
        for r in rows[:15]:
            mark = "OSMO" if r["osmo"] else ""
            svc = ",".join(r["services"]) if r["services"] else "-"
            print(f"{r['address']:>18} {r['rssi']:>5}  {mark:>5}  "
                  f"{r['name'] or '(unnamed)'}  [{svc}]")
            if r["osmo"] and r["mfr"]:
                for cid, data in r["mfr"].items():
                    print(f"{'':>34} mfr 0x{cid:04X}: {data.hex()}")

        osmo = [r for r in rows if r["osmo"]]
        if not osmo:
            print("\nNO Osmo advertisement seen. Either BLE is off in this "
                  "camera mode, or it stops advertising once paired/connected "
                  "to another host.")
            return 1
        target = osmo[0]["address"]
        duml = DUML_SERVICE in "".join(osmo[0]["services"])
        print(f"\nOsmo FOUND at {target} while scanning"
              f"{' -- advertises fff0 (DUML)' if duml else ''}")

    if args.probe and target:
        await probe(target)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
