"""Unit tests for the check-sync comparison logic (no GStreamer needed)."""

from __future__ import annotations

import unittest

from gstreamer_source.sei import frame_grid_us
from gstreamer_source.sync_check import Sample, StreamEnd, SyncChecker

FPS = 30
T0 = 1_789_415_400_000_000  # Unix us, on the 30 fps grid


def grid(k: int) -> int:
    return frame_grid_us(T0 + k * 1_000_000 // FPS, FPS)


def frames(stream: int, first: int, count: int, skip=(), arrival=None):
    out = []
    for k in range(first, first + count):
        if k in skip:
            continue
        out.append(Sample(stream, grid(k), k - first + 1, arrival))
    return out


class FrameGridTest(unittest.TestCase):
    def test_grid_is_stable_under_sub_frame_jitter(self):
        for k in range(0, 1000, 7):
            exact = (T0 * FPS // 1_000_000 + k) * 1_000_000 // FPS
            for jitter in (-16_000, -1, 0, 1, 16_000):
                self.assertEqual(frame_grid_us(exact + jitter, FPS), exact)

    def test_grid_points_are_distinct_and_monotonic(self):
        pts = [grid(k) for k in range(300)]
        self.assertEqual(sorted(set(pts)), pts)


class SyncCheckerTest(unittest.TestCase):
    def check(self, *streams, hold_s=0.5, interleave=True):
        names = [f"s{i}" for i in range(len(streams))]
        chk = SyncChecker(names, FPS, verbose=False, hold_s=hold_s)
        if interleave:
            # Round-robin like live streams arriving together.
            idx = [0] * len(streams)
            while any(i < len(s) for i, s in zip(idx, streams)):
                for n, s in enumerate(streams):
                    if idx[n] < len(s):
                        chk.feed(s[idx[n]])
                        idx[n] += 1
        else:
            # Whole streams one after another, like files read in threads.
            for s in streams:
                for sample in s:
                    chk.feed(sample)
                chk.end(StreamEnd(s[0].stream))
        for n in range(len(streams)):
            chk.end(StreamEnd(n))
        chk.flush()
        return chk

    def test_identical_streams_match(self):
        chk = self.check(frames(0, 0, 60), frames(1, 0, 60))
        self.assertEqual((chk.matched, chk.mismatched), (60, 0))

    def test_late_start_is_not_a_mismatch(self):
        chk = self.check(frames(0, 0, 60), frames(1, 20, 40), frames(2, 35, 25))
        self.assertEqual(chk.mismatched, 0)
        self.assertEqual(chk.matched, 40)  # instants where at least two ran
        self.assertEqual([s.missing for s in chk.streams], [0, 0, 0])

    def test_dropped_frame_is_reported_on_that_stream(self):
        chk = self.check(frames(0, 0, 60), frames(1, 0, 60, skip={30}))
        self.assertEqual((chk.matched, chk.mismatched), (59, 1))
        self.assertEqual(chk.streams[1].missing, 1)
        self.assertEqual(chk.streams[0].stray, 1)

    def test_unaligned_stream_never_matches(self):
        off = [
            Sample(1, s.timestamp_us + 9_000, s.frame_id, None)
            for s in frames(1, 0, 60)
        ]
        chk = self.check(frames(0, 0, 60), off)
        self.assertEqual(chk.matched, 0)
        self.assertGreater(chk.off_grid, 0)

    def test_file_order_whole_stream_first(self):
        a = frames(0, 0, 80)
        b = frames(1, 21, 81)
        chk = self.check(a, b, interleave=False)
        self.assertEqual(chk.mismatched, 0)
        self.assertEqual(chk.matched, 59)  # k = 21..79 present on both
        chk = self.check(b, a, interleave=False)
        self.assertEqual((chk.matched, chk.mismatched), (59, 0))

    def test_early_end_of_one_stream_is_not_missing(self):
        chk = self.check(frames(0, 0, 100), frames(1, 0, 50))
        self.assertEqual((chk.matched, chk.mismatched), (50, 0))

    def test_live_resolution_waits_for_hold(self):
        chk = SyncChecker(["a", "b"], FPS, verbose=False, hold_s=0.5)
        for s in frames(0, 0, 10, arrival=1.0):
            chk.feed(s)
        # b is 5 frames (167 ms) behind: the instants both delivered resolve
        # at once, the ones only a has so far wait within the hold.
        for s in frames(1, 0, 5, arrival=1.2):
            chk.feed(s)
        self.assertEqual((chk.matched, chk.mismatched), (5, 0))
        for s in frames(1, 5, 5, arrival=1.2):
            chk.feed(s)
        for s in frames(0, 10, 30, arrival=1.0):
            chk.feed(s)
        # a is now a full second ahead; b still holds everything a has passed.
        self.assertEqual(chk.mismatched, 0)
        self.assertGreater(chk.matched, 0)
        self.assertAlmostEqual(chk.max_skew_ms, 200.0, places=3)


if __name__ == "__main__":
    unittest.main()
