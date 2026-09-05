import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .inspect import inspect_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vpn-inspect",
        description="Inspect the route, interface, MTU, and ICMP path to a VPN peer.",
    )
    parser.add_argument("target", help="peer VPN IPv4 address or hostname")
    parser.add_argument("--timeout", type=_positive_float, default=2.0)
    parser.add_argument(
        "--packet-sizes",
        type=_packet_sizes,
        default=(1200, 1300, 1400, 1472),
        help="comma-separated ICMP payload sizes",
    )
    parser.add_argument("--no-traceroute", action="store_true")
    parser.add_argument(
        "--pcap", metavar="PATH", help="capture diagnostic traffic with tcpdump"
    )
    parser.add_argument(
        "--json", metavar="PATH", help="write JSON report, or - for stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        report = inspect_target(
            args.target,
            args.timeout,
            args.packet_sizes,
            not args.no_traceroute,
            args.pcap,
        )
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from error

    if args.json == "-":
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
            print(f"JSON report: {args.json}")
    raise SystemExit(0 if report["success"] else 1)


def _print_report(report: dict[str, Any]) -> None:
    print(f"Target: {report['target']} ({report['resolved_address'] or 'unresolved'})")
    print(f"Selected source: {report['selected_source'] or 'unknown'}")
    print(f"Selected interface: {report['selected_interface'] or 'unknown'}")
    interface = next(
        (
            item
            for item in report["interfaces"]
            if item["name"] == report["selected_interface"]
        ),
        None,
    )
    if interface:
        print(f"Interface MTU: {interface['mtu']}")
    if report["route"]:
        state = "PASS" if report["route"]["success"] else "WARN"
        print(f"Route lookup: {state}")
        if report["route"]["stdout"]:
            print(report["route"]["stdout"])
    if report["ping"]:
        state = "PASS" if report["ping"]["success"] else "NO REPLY"
        print(f"ICMP: {state}")
    for result in report["packet_size_sweep"]:
        state = "PASS" if result["success"] else "FAIL"
        print(f"ICMP payload {result['payload_size']} bytes: {state}")
    if report["traceroute"]:
        state = "PASS" if report["traceroute"]["success"] else "WARN"
        print(f"Traceroute: {state}")
        if report["traceroute"]["stdout"]:
            print(report["traceroute"]["stdout"])
    for note in report.get("observations", []):
        print(f"Observation: {note}")
    for error in report["errors"]:
        print(f"Error: {error}")


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _packet_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "packet sizes must be comma-separated integers"
        ) from error
    if not sizes or any(size < 0 or size > 65_000 for size in sizes):
        raise argparse.ArgumentTypeError("packet sizes must be between 0 and 65000")
    return sizes


if __name__ == "__main__":
    main(sys.argv[1:])
