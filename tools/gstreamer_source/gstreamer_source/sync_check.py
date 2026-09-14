"""Verify that several H.264 streams carry the same SEI capture timestamps.

Simulates the check an AV does on its camera set: every camera is triggered by
the same signal, so at any moment all streams must carry the same SEI
timestamp even though each camera (and each gstreamer-source instance) was
started at a different time and its frame ids therefore differ.

Each input (tcp host:port or a file) is read into access units and the SEI
timestamp of every frame is recorded. A timestamp is *matched* when every
stream that was already running at that instant produced it, *missing* on a
stream that had started but skipped it, and *stray* when no other stream has
it at all. Timestamps that lie before a stream's first frame are not held
against that stream. For live inputs the arrival skew (latest minus earliest
receive time of the same frame across streams) is reported as well.

Exit status is 0 when every timestamp inside the common window was seen on
every stream, 1 otherwise.
"""

from __future__ import annotations

import argparse
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .parse import (
    AccessUnitAssembler,
    ArrivalTracker,
    _iso,
    detect_stream_format,
    extract_sei_timestamp,
    split_nalus,
)
from .sei import frame_grid_us

# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    stream: int
    timestamp_us: int
    frame_id: int | None
    arrival: float | None  # time.time(); None for files


@dataclass
class StreamEnd:
    stream: int
    error: str | None = None


Event = Sample | StreamEnd


def _emit_access_units(
    chunks, stream: int, stream_format: str | None, out: queue.Queue, live: bool
) -> None:
    buffer = bytearray()
    arrivals = ArrivalTracker()
    assembler = AccessUnitAssembler()
    detected = stream_format

    def report(au) -> None:
        for payload in au.sei_payloads():
            found = extract_sei_timestamp(payload)
            if found is not None:
                out.put(Sample(stream, found.timestamp_us, found.frame_id, au.arrival))
                return

    for data in chunks:
        buffer.extend(data)
        if live:
            arrivals.extend(len(data), time.time())
        if detected is None:
            if len(buffer) < 8:
                continue
            detected = detect_stream_format(buffer)
            if detected is None:
                if len(buffer) < 65536:
                    continue
                raise ValueError("no H.264 framing detected")
        tracked = len(buffer)
        for nal in split_nalus(buffer, detected):
            arrival = arrivals.consume(tracked - len(buffer)) if live else None
            tracked = len(buffer)
            au = assembler.feed(nal, arrival)
            if au:
                report(au)
        if live:
            arrivals.consume(tracked - len(buffer))
    if detected:
        for nal in split_nalus(buffer, detected, final=True):
            au = assembler.feed(nal)
            if au:
                report(au)
    last = assembler.flush()
    if last:
        report(last)


def _tcp_chunks(sock: socket.socket):
    while True:
        data = sock.recv(65536)
        if not data:
            return
        yield data


def read_stream(
    stream: int, target: str, stream_format: str | None, out: queue.Queue
) -> None:
    """Thread body: feed Samples for one input, then a StreamEnd."""
    error: str | None = None
    try:
        if is_tcp_target(target):
            host, port = target.rsplit(":", 1)
            sock = socket.create_connection((host, int(port)))
            try:
                _emit_access_units(_tcp_chunks(sock), stream, stream_format, out, True)
            finally:
                sock.close()
        else:
            data = Path(target).read_bytes()
            _emit_access_units([data], stream, stream_format, out, False)
    except (OSError, ValueError) as exc:
        error = str(exc)
    out.put(StreamEnd(stream, error))


def is_tcp_target(target: str) -> bool:
    if Path(target).exists():
        return False
    host, sep, port = target.rpartition(":")
    return bool(sep) and bool(host) and port.isdigit()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class StreamState:
    name: str
    first_ts: int | None = None
    last_ts: int | None = None
    frames: int = 0
    missing: int = 0
    stray: int = 0
    ended: bool = False
    error: str | None = None
    first_frame_id: int | None = None
    phase_sum_us: int = 0  # offset from the frame grid, for the summary

    def running_at(self, ts: int) -> bool:
        if self.first_ts is None or ts < self.first_ts:
            return False
        return not self.ended or (self.last_ts is not None and ts <= self.last_ts)


@dataclass
class Entry:
    seen: dict[int, Sample] = field(default_factory=dict)


class SyncChecker:
    def __init__(
        self, names: list[str], fps: int | None, verbose: bool, hold_s: float = 0.5
    ):
        self.streams = [StreamState(n) for n in names]
        self.fps = fps
        self.verbose = verbose
        # A timestamp counts as missing on a stream only once that stream has
        # delivered a frame this far past it, so arrival skew between streams
        # smaller than the hold never reads as a mismatch.
        self.hold_us = int(hold_s * 1_000_000)
        self.entries: dict[int, Entry] = {}
        self.matched = 0
        self.mismatched = 0
        self.max_skew_ms = 0.0
        self.skew_sum_ms = 0.0
        self.skew_count = 0
        self.off_grid = 0
        self.live = False

    # -- feeding ----------------------------------------------------------

    def feed(self, s: Sample) -> None:
        st = self.streams[s.stream]
        st.frames += 1
        if st.first_ts is None:
            st.first_ts = s.timestamp_us
            st.first_frame_id = s.frame_id
        st.last_ts = s.timestamp_us
        if s.arrival is not None:
            self.live = True
        if self.fps:
            phase = s.timestamp_us - frame_grid_us(s.timestamp_us, self.fps)
            st.phase_sum_us += phase
            if phase:
                self.off_grid += 1
        self.entries.setdefault(s.timestamp_us, Entry()).seen[s.stream] = s
        self._finalize_ready()

    def end(self, e: StreamEnd) -> None:
        st = self.streams[e.stream]
        st.ended = True
        st.error = e.error
        self._finalize_ready()

    # -- resolution -------------------------------------------------------

    def _settled(self, ts: int) -> bool:
        """True once no stream can still deliver `ts`: every stream has ended
        or has already produced a frame more than `hold_us` past it. A stream
        that has not produced anything yet blocks, since its frames may still
        be on their way (file inputs arrive in arbitrary order)."""
        for st in self.streams:
            if st.ended:
                continue
            if st.last_ts is None or st.last_ts <= ts + self.hold_us:
                return False
        return True

    def _complete(self, ts: int, entry: Entry) -> bool:
        for i, st in enumerate(self.streams):
            if st.first_ts is None and not st.ended:
                return False
            if st.running_at(ts) and i not in entry.seen:
                return False
        return True

    def _finalize_ready(self) -> None:
        for ts in sorted(self.entries):
            entry = self.entries[ts]
            if not self._complete(ts, entry) and not self._settled(ts):
                break
            self._resolve(ts, entry)
            del self.entries[ts]

    def flush(self) -> None:
        for ts in sorted(self.entries):
            self._resolve(ts, self.entries[ts])
        self.entries.clear()

    def _resolve(self, ts: int, entry: Entry) -> None:
        expected = [i for i, st in enumerate(self.streams) if st.running_at(ts)]
        missing = [i for i in expected if i not in entry.seen]
        if len(entry.seen) < 2 and len(self.streams) > 1 and not missing:
            # Only one stream was running at this instant; nothing to compare.
            return
        if missing:
            self.mismatched += 1
            for i in missing:
                self.streams[i].missing += 1
            if len(entry.seen) == 1:
                self.streams[next(iter(entry.seen))].stray += 1
            have = ",".join(self.streams[i].name for i in entry.seen)
            lack = ",".join(self.streams[i].name for i in missing)
            print(
                f"MISMATCH ts={ts} ({_iso(ts)}): seen on [{have}] missing on [{lack}]"
            )
            return
        self.matched += 1
        arrivals = [s.arrival for s in entry.seen.values() if s.arrival is not None]
        skew_ms = (max(arrivals) - min(arrivals)) * 1000 if len(arrivals) > 1 else 0.0
        if arrivals:
            self.skew_sum_ms += skew_ms
            self.skew_count += 1
            self.max_skew_ms = max(self.max_skew_ms, skew_ms)
        if self.verbose:
            ids = " ".join(
                f"{self.streams[i].name}=#{s.frame_id}"
                for i, s in sorted(entry.seen.items())
            )
            print(f"ok ts={ts}  skew={skew_ms:5.1f}ms  {ids}")

    # -- reporting --------------------------------------------------------

    def progress_line(self) -> str:
        parts = [f"matched={self.matched}", f"mismatched={self.mismatched}"]
        if self.skew_count:
            parts.append(f"skew max={self.max_skew_ms:.1f}ms")
        parts.append("frames=" + "/".join(str(st.frames) for st in self.streams))
        return "  ".join(parts)

    def summary(self) -> int:
        self.flush()
        print()
        print(f"Streams: {len(self.streams)}")
        for st in self.streams:
            line = (
                f"  {st.name}: frames={st.frames} missing={st.missing} stray={st.stray}"
            )
            if st.first_ts is not None:
                line += f" first={_iso(st.first_ts)}"
                if st.first_frame_id is not None:
                    line += f" (id {st.first_frame_id})"
            if self.fps and st.frames:
                line += f" grid-offset={st.phase_sum_us / st.frames:+.0f}us"
            if st.error:
                line += f" error={st.error}"
            print(line)
        print(
            f"Timestamps in common window: {self.matched + self.mismatched}, "
            f"matched on all streams: {self.matched}, mismatched: {self.mismatched}"
        )
        if self.skew_count:
            print(
                f"Arrival skew of the same frame across streams: "
                f"mean {self.skew_sum_ms / self.skew_count:.1f}ms, max {self.max_skew_ms:.1f}ms"
            )
        if self.fps:
            if self.off_grid:
                print(
                    f"{self.off_grid} timestamps are off the {self.fps} fps wall-clock "
                    "grid; sources are probably not running with --align-frames"
                )
            else:
                print(f"All timestamps sit on the {self.fps} fps wall-clock grid")
        if self.matched == 0 and self.mismatched:
            print(
                "No timestamp was shared by all streams: start every "
                "gstreamer-source with --align-frames and the same --fps"
            )
        print(
            "RESULT: "
            + ("IN SYNC" if self.mismatched == 0 and self.matched else "OUT OF SYNC")
        )
        return 0 if self.mismatched == 0 and self.matched else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check-sync",
        description=(
            "Read two or more gstreamer-source streams (tcp host:port or files) "
            "and verify they carry identical SEI capture timestamps."
        ),
    )
    p.add_argument(
        "targets", nargs="+", metavar="HOST:PORT|FILE", help="streams to compare"
    )
    p.add_argument(
        "--stream-format",
        choices=["byte-stream", "avc"],
        default=None,
        help="H.264 framing; auto-detected when omitted",
    )
    p.add_argument(
        "--fps",
        type=int,
        default=30,
        help="frame rate of the sources, used to check that timestamps sit on "
        "the wall-clock frame grid (0 disables; default 30)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop after this long (tcp; default: run until Ctrl+C)",
    )
    p.add_argument(
        "--report-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="print a progress line this often (tcp; 0 disables)",
    )
    p.add_argument(
        "--hold",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="a stream is only reported as missing a timestamp once it has "
        "delivered a frame this far past it, so arrival skew below this "
        "never counts as a mismatch",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="print every matched timestamp"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if len(args.targets) < 2:
        parser.error("give at least two streams to compare")
    for t in args.targets:
        if not is_tcp_target(t) and not Path(t).exists():
            print(f"ERROR: {t} is neither host:port nor an existing file")
            return 1

    checker = SyncChecker(args.targets, args.fps or None, args.verbose, args.hold)
    events: queue.Queue[Event] = queue.Queue()
    threads = [
        threading.Thread(
            target=read_stream, args=(i, t, args.stream_format, events), daemon=True
        )
        for i, t in enumerate(args.targets)
    ]
    for t in threads:
        t.start()
    print("Comparing SEI timestamps across: " + ", ".join(args.targets))

    started = time.monotonic()
    last_report = started
    open_streams = len(threads)
    try:
        while open_streams:
            timeout = 0.25
            if args.duration is not None:
                remaining = args.duration - (time.monotonic() - started)
                if remaining <= 0:
                    break
                timeout = min(timeout, remaining)
            try:
                ev = events.get(timeout=timeout)
            except queue.Empty:
                ev = None
            if isinstance(ev, Sample):
                checker.feed(ev)
            elif isinstance(ev, StreamEnd):
                checker.end(ev)
                open_streams -= 1
                if ev.error:
                    print(f"{args.targets[ev.stream]}: {ev.error}")
            now = time.monotonic()
            if (
                checker.live
                and args.report_interval
                and now - last_report >= args.report_interval
            ):
                print(f"[{now - started:6.1f}s] {checker.progress_line()}")
                last_report = now
    except KeyboardInterrupt:
        print("\nInterrupted")
    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
