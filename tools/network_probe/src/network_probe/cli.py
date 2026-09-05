import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .client import run_probe
from .server import ProbeServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-probe",
        description="Test authenticated TCP and UDP connectivity between VPN peers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="listen for TCP and UDP probes")
    serve.add_argument("--bind", default="0.0.0.0", help="IPv4 address to bind")
    serve.add_argument("--tcp-port", type=_port, default=45000)
    serve.add_argument("--udp-port", type=_port, default=45001)
    serve.add_argument("--token", help="shared token; defaults to NETWORK_PROBE_TOKEN")

    test = subparsers.add_parser("test", help="probe a VPN peer")
    test.add_argument("host", help="peer VPN IPv4 address or hostname")
    test.add_argument("--tcp-port", type=_port, default=45000)
    test.add_argument("--udp-port", type=_port, default=45001)
    test.add_argument("--token", help="shared token; defaults to NETWORK_PROBE_TOKEN")
    test.add_argument("--count", type=_positive_int, default=5)
    test.add_argument("--interval", type=_non_negative_float, default=0.2)
    test.add_argument("--timeout", type=_positive_float, default=2.0)
    test.add_argument("--packet-size", type=_packet_size, default=256)
    test.add_argument(
        "--json", metavar="PATH", help="write JSON report, or - for stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    token = args.token or os.environ.get("NETWORK_PROBE_TOKEN")
    if args.command == "serve":
        token = token or secrets.token_urlsafe(18)
        print(f"Shared token: {token}")
        try:
            asyncio.run(_serve(args.bind, args.tcp_port, args.udp_port, token))
        except KeyboardInterrupt:
            print("\nProbe server stopped.")
        return

    if not token:
        raise SystemExit("--token or NETWORK_PROBE_TOKEN is required for test mode")
    report = run_probe(
        args.host,
        args.tcp_port,
        args.udp_port,
        token,
        args.count,
        args.interval,
        args.timeout,
        args.packet_size,
    )
    if args.json == "-":
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
            print(f"JSON report: {args.json}")
    raise SystemExit(0 if report["success"] else 1)


async def _serve(bind: str, tcp_port: int, udp_port: int, token: str) -> None:
    server = ProbeServer(bind, tcp_port, udp_port, token)
    await server.start()
    print(f"TCP listening on {bind}:{server.tcp_port}")
    print(f"UDP listening on {bind}:{server.udp_port}")
    await server.serve_forever()


def _print_report(report: dict[str, Any]) -> None:
    print(f"Target: {report['target']} ({report['resolved_address'] or 'unresolved'})")
    print(f"Selected source: {report['source_address'] or 'unknown'}")
    for name in ("tcp", "udp"):
        result = report[name]
        state = "PASS" if result["reachable"] else "FAIL"
        line = (
            f"{name.upper()} {result['port']}: {state}; "
            f"received {result['received']}/{result['sent']}; "
            f"loss {result['loss_percent']:.2f}%"
        )
        if result["rtt_ms"]:
            line += (
                f"; RTT avg {result['rtt_ms']['average']:.3f} ms; "
                f"jitter {result['jitter_ms']:.3f} ms"
            )
        print(line)
        if result["error"]:
            print(f"  Last error: {result['error']}")


def _port(value: str) -> int:
    number = int(value)
    if not 0 < number < 65_536:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _packet_size(value: str) -> int:
    number = int(value)
    if not 192 <= number <= 65_000:
        raise argparse.ArgumentTypeError("packet size must be between 192 and 65000")
    return number


if __name__ == "__main__":
    main(sys.argv[1:])
