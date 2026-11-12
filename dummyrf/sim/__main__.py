"""Run the simulated rack standalone:  python -m dummyrf.sim [--host 0.0.0.0]"""
import argparse
import asyncio

from .devices import DEMO_RACK, start_rack


async def main(host: str) -> None:
    servers = await start_rack(DEMO_RACK, host)
    for spec in DEMO_RACK:
        proto = "UDP" if spec.model.startswith("SR") else "TCP"
        print(f"  {spec.label:<24} {spec.model:<10} band {spec.band:<5} {proto} {host}:{spec.port}")
    print("Simulators running — Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        for s in servers:
            await s.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    try:
        asyncio.run(main(ap.parse_args().host))
    except KeyboardInterrupt:
        pass
