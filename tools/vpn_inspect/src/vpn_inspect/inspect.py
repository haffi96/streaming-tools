import math
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

VPN_INTERFACE_PREFIXES = ("utun", "tun", "tap", "ppp", "wg", "ipsec")


def inspect_target(
    target: str,
    timeout: float = 2.0,
    packet_sizes: tuple[int, ...] = (1200, 1300, 1400, 1472),
    traceroute: bool = True,
    pcap: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    report: dict[str, Any] = {
        "target": target,
        "resolved_address": None,
        "selected_source": None,
        "selected_interface": None,
        "platform": platform.system(),
        "interfaces": get_interfaces(),
        "route": None,
        "ping": None,
        "packet_size_sweep": [],
        "traceroute": None,
        "capture": None,
        "errors": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
    }

    try:
        address = socket.gethostbyname(target)
        report["resolved_address"] = address
        report["selected_source"] = select_source_address(address)
        report["selected_interface"] = interface_for_address(
            report["interfaces"], report["selected_source"]
        )
    except OSError as error:
        report["errors"].append(f"name or route resolution failed: {error}")
        return _finish(report, started)

    report["route"] = inspect_route(address, timeout)
    capture = _start_capture(pcap, report["selected_interface"], address)
    try:
        report["ping"] = ping(address, timeout)
        report["packet_size_sweep"] = [
            ping(address, timeout, size) for size in packet_sizes
        ]
        if traceroute:
            report["traceroute"] = trace_route(address, timeout)
    finally:
        report["capture"] = _stop_capture(capture, pcap)

    report["observations"] = observations(report)
    return _finish(report, started)


def get_interfaces() -> list[dict[str, Any]]:
    addresses = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    interfaces = []
    for name in sorted(addresses):
        ipv4 = [
            address.address
            for address in addresses[name]
            if address.family == socket.AF_INET
        ]
        ipv6 = [
            address.address.split("%", 1)[0]
            for address in addresses[name]
            if address.family == socket.AF_INET6
        ]
        status = stats.get(name)
        interfaces.append(
            {
                "name": name,
                "ipv4": ipv4,
                "ipv6": ipv6,
                "is_up": status.isup if status else None,
                "mtu": status.mtu if status else None,
                "likely_vpn": name.lower().startswith(VPN_INTERFACE_PREFIXES),
            }
        )
    return interfaces


def select_source_address(address: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((address, 9))
        return sock.getsockname()[0]


def interface_for_address(interfaces: list[dict[str, Any]], address: str) -> str | None:
    return next(
        (interface["name"] for interface in interfaces if address in interface["ipv4"]),
        None,
    )


def inspect_route(address: str, timeout: float) -> dict[str, Any]:
    if platform.system() == "Darwin":
        command = ["route", "-n", "get", address]
    else:
        command = ["ip", "-4", "route", "get", address]
    return run_command(command, timeout)


def ping(
    address: str, timeout: float, payload_size: int | None = None
) -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        command = ["ping", "-n", "-c", "1", "-W", str(math.ceil(timeout * 1000))]
        if payload_size is not None:
            command.extend(["-D", "-s", str(payload_size)])
    else:
        command = ["ping", "-n", "-4", "-c", "1", "-W", str(math.ceil(timeout))]
        if payload_size is not None:
            command.extend(["-M", "do", "-s", str(payload_size)])
    command.append(address)
    result = run_command(command, timeout + 1)
    result["payload_size"] = payload_size
    return result


def trace_route(address: str, timeout: float) -> dict[str, Any]:
    command = [
        "traceroute",
        "-n",
        "-m",
        "15",
        "-w",
        str(max(1, math.ceil(timeout))),
        address,
    ]
    return run_command(command, 20)


def run_command(command: list[str], timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "command": command,
        "available": shutil.which(command[0]) is not None,
        "success": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    if not result["available"]:
        result["stderr"] = f"{command[0]} is not installed"
        return result
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result.update(
            success=completed.returncode == 0,
            returncode=completed.returncode,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
        )
    except subprocess.TimeoutExpired as error:
        result["stderr"] = f"command timed out after {timeout:g}s"
        if error.stdout:
            output = (
                error.stdout.decode()
                if isinstance(error.stdout, bytes)
                else error.stdout
            )
            result["stdout"] = output.strip()
    return result


def observations(report: dict[str, Any]) -> list[str]:
    notes = []
    interface = next(
        (
            item
            for item in report["interfaces"]
            if item["name"] == report["selected_interface"]
        ),
        None,
    )
    if interface is None:
        notes.append("The selected source address could not be mapped to an interface.")
    elif interface["likely_vpn"]:
        notes.append(
            f"Traffic selects likely VPN interface {interface['name']} with MTU {interface['mtu']}."
        )
    else:
        notes.append(
            f"Traffic selects {interface['name']}, which is not recognized as a typical VPN interface."
        )

    if not report["ping"]["success"]:
        notes.append(
            "ICMP did not receive a reply; this may be filtering rather than peer failure."
        )
    successful_sizes = [
        item["payload_size"] for item in report["packet_size_sweep"] if item["success"]
    ]
    if successful_sizes:
        notes.append(
            f"Largest successful non-fragmenting ICMP payload: {max(successful_sizes)} bytes."
        )
    elif report["ping"]["success"]:
        notes.append(
            "Small ICMP works, but every tested non-fragmenting payload size failed."
        )
    return notes


def _start_capture(
    path: str | None, interface: str | None, address: str
) -> subprocess.Popen[str] | None:
    if path is None:
        return None
    if interface is None:
        raise RuntimeError("cannot capture because the selected interface is unknown")
    if shutil.which("tcpdump") is None:
        raise RuntimeError("cannot capture because tcpdump is not installed")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["tcpdump", "-i", interface, "-n", "-U", "-w", path, "host", address],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Give tcpdump time to attach before generating the diagnostic packets.
    time.sleep(0.2)
    return process


def _stop_capture(
    process: subprocess.Popen[str] | None, path: str | None
) -> dict[str, Any] | None:
    if process is None:
        return None
    process.terminate()
    try:
        _, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate()
    return {
        "path": path,
        "returncode": process.returncode,
        "stderr": stderr.strip(),
        "created": bool(path and Path(path).exists()),
    }


def _finish(report: dict[str, Any], started: float) -> dict[str, Any]:
    report["duration_seconds"] = round(time.time() - started, 3)
    report["success"] = bool(report["resolved_address"] and report["selected_source"])
    return report
