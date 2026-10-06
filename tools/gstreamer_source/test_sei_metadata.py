import struct
import unittest
from pathlib import Path

from gstreamer_source.parse import (
    H265,
    AccessUnitAssembler,
    detect_codec,
    detect_stream_format,
    extract_sei_timestamp,
    parse_packet_trailer,
    parse_user_data,
    remove_emulation_prevention,
    split_annexb,
    split_avc,
)
from gstreamer_source.sei import (
    SEI_UUID,
    add_emulation_prevention,
    append_packet_trailer,
    create_aud_nalu,
    create_sei_nalu,
)

SAMPLES = Path(__file__).parent / "samples"


class SeiMetadataTests(unittest.TestCase):
    def test_append_packet_trailer_round_trip(self):
        timestamp_us = 1_746_000_000_123_456
        frame_id = 123
        trailer = append_packet_trailer(timestamp_us, frame_id)
        metadata = parse_packet_trailer(trailer)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["timestamp_us"], timestamp_us)
        self.assertEqual(metadata["frame_id"], frame_id)

    def test_create_sei_nalu_annex_b_contains_lkts_trailer(self):
        timestamp_us = 42
        sei = create_sei_nalu(timestamp_us=timestamp_us, stream_format="byte-stream")
        self.assertTrue(sei.startswith(b"\x00\x00\x00\x01"))
        payload = sei[5:]
        user_data = payload[2:-1]
        parsed = parse_user_data(user_data[:16], user_data[16:])
        self.assertEqual(parsed, (timestamp_us, "lkts"))

    def test_create_sei_nalu_annex_b_contains_frame_id(self):
        timestamp_us = 42
        frame_id = 7
        sei = create_sei_nalu(
            timestamp_us=timestamp_us,
            frame_id=frame_id,
            stream_format="byte-stream",
        )
        payload = sei[5:]
        user_data = payload[2:-1]
        metadata = parse_packet_trailer(user_data[16:])
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["timestamp_us"], timestamp_us)
        self.assertEqual(metadata["frame_id"], frame_id)

    def test_legacy_payload_still_parses(self):
        timestamp_us = 99
        legacy_payload = struct.pack(">Q", timestamp_us)
        parsed = parse_user_data(SEI_UUID, legacy_payload)
        self.assertEqual(parsed, (timestamp_us, "legacy"))


class TimestampOverlayTests(unittest.TestCase):
    def test_format_clock_ms(self):
        from gstreamer_source.overlay import format_clock_ms

        self.assertEqual(format_clock_ms(0), "0:00:00.000")
        self.assertEqual(format_clock_ms(1_522), "0:00:01.522")
        self.assertEqual(format_clock_ms(61_005), "0:01:01.005")
        self.assertEqual(format_clock_ms(3_661_999), "1:01:01.999")


def _h264_sei_reference(timestamp_us: int, frame_id: int) -> bytes:
    """The H.264 SEI NAL as built before H.265 support (no escaping needed)."""
    payload = SEI_UUID + append_packet_trailer(timestamp_us, frame_id)
    return bytes([0x06, 5, len(payload)]) + payload + b"\x80"


class H264UnchangedTests(unittest.TestCase):
    def test_h264_sei_bytes_unchanged(self):
        ts, fid = 1_746_000_000_123_456, 77
        nal = _h264_sei_reference(ts, fid)
        self.assertEqual(
            create_sei_nalu(ts, fid, "byte-stream"), b"\x00\x00\x00\x01" + nal
        )
        self.assertEqual(
            create_sei_nalu(ts, fid, "avc"), struct.pack(">I", len(nal)) + nal
        )

    def test_h264_aud_bytes_unchanged(self):
        self.assertEqual(create_aud_nalu("byte-stream"), b"\x00\x00\x00\x01\x09\xf0")
        self.assertEqual(create_aud_nalu("avc"), b"\x00\x00\x00\x02\x09\xf0")

    def test_h264_samples_detected_as_h264(self):
        for name in ("generated_sei.h264", "generated_sei_avc.h264"):
            data = (SAMPLES / name).read_bytes()
            fmt = detect_stream_format(data)
            self.assertIn(fmt, ("byte-stream", "avc"), name)
            self.assertEqual(detect_codec(data, fmt), "h264", name)


class HevcSeiTests(unittest.TestCase):
    def test_hevc_sei_layout(self):
        ts, fid = 1_746_000_000_123_456, 5
        sei = create_sei_nalu(ts, fid, "byte-stream", codec="h265")
        payload = SEI_UUID + append_packet_trailer(ts, fid)
        self.assertEqual(
            sei,
            b"\x00\x00\x00\x01\x4e\x01" + bytes([5, len(payload)]) + payload + b"\x80",
        )

    def test_hevc_sei_round_trip(self):
        ts, fid = 1_746_000_000_123_456, 42
        for fmt in ("byte-stream", "avc", "hvc1", "hev1"):
            sei = create_sei_nalu(ts, fid, fmt, codec="h265")
            nal = sei[4:]
            if fmt != "byte-stream":
                self.assertEqual(struct.unpack(">I", sei[:4])[0], len(nal))
            self.assertEqual(H265.nal_type(nal), 39)
            found = extract_sei_timestamp(remove_emulation_prevention(nal[2:]))
            self.assertIsNotNone(found)
            self.assertEqual((found.timestamp_us, found.frame_id), (ts, fid))

    def test_hevc_aud_bytes(self):
        self.assertEqual(
            create_aud_nalu("byte-stream", "h265"), b"\x00\x00\x00\x01\x46\x01\x50"
        )
        self.assertEqual(
            create_aud_nalu("avc", "h265"), b"\x00\x00\x00\x03\x46\x01\x50"
        )
        self.assertEqual(
            create_aud_nalu("hev1", "h265"), b"\x00\x00\x00\x03\x46\x01\x50"
        )
        self.assertEqual(H265.nal_type(b"\x46\x01"), 35)

    def test_emulation_prevention_round_trip(self):
        # 0x...FFFF.. in the timestamp XORs to 00 00 in the trailer.
        ts = 0x0006_2B3C_FFFF_FC10
        for codec in ("h264", "h265"):
            sei = create_sei_nalu(ts, 1, "byte-stream", codec=codec)
            body = sei[4:]
            self.assertNotIn(b"\x00\x00\x00", body)
            self.assertNotIn(b"\x00\x00\x01", body)
            self.assertIn(b"\x00\x00\x03", body)
            header_len = 2 if codec == "h265" else 1
            found = extract_sei_timestamp(
                remove_emulation_prevention(body[header_len:])
            )
            self.assertEqual(found.timestamp_us, ts)
        self.assertEqual(
            add_emulation_prevention(b"\x00\x00\x00\x00"), b"\x00\x00\x03\x00\x00"
        )
        self.assertEqual(
            remove_emulation_prevention(b"\x00\x00\x03\x00\x00"), b"\x00\x00\x00\x00"
        )


# Minimal HEVC NALs: VPS, SPS, PPS, IDR_N_LP / TRAIL_R slices with
# first_slice_segment_in_pic_flag set, suffix SEI.
VPS = b"\x40\x01\x0c\x01"
SPS = b"\x42\x01\x01\x01"
PPS = b"\x44\x01\xc1\x72"
IDR = b"\x28\x01\xaf\x10\x20"
TRAIL = b"\x02\x01\xd0\x10"
SUFFIX_SEI = b"\x50\x01\x05\x00\x80"


def _hevc_au(slice_nal: bytes, ts: int, fid: int, fmt: str, params: bool) -> bytes:
    nals = [VPS, SPS, PPS] if params else []
    nals.append(slice_nal)
    sc = b"\x00\x00\x00\x01"
    if fmt == "byte-stream":
        body = b"".join(sc + n for n in nals)
    else:
        body = b"".join(struct.pack(">I", len(n)) + n for n in nals)
    return (
        create_sei_nalu(ts, fid, fmt, codec="h265")
        + body
        + create_aud_nalu(fmt, codec="h265")
    )


class HevcParseTests(unittest.TestCase):
    def _stream(self, fmt: str) -> bytes:
        return _hevc_au(IDR, 1000, 1, fmt, True) + _hevc_au(TRAIL, 2000, 2, fmt, False)

    def test_detect_hevc(self):
        for fmt in ("byte-stream", "avc"):
            data = self._stream(fmt)
            self.assertEqual(detect_stream_format(data), fmt)
            self.assertEqual(detect_codec(data, fmt), "h265")

    def test_hevc_aud_completes_without_next_start_code(self):
        buf = bytearray(_hevc_au(TRAIL, 1, 1, "byte-stream", False))
        nals = list(split_annexb(buf, final=False, syntax=H265))
        # SEI and the slice complete when the next start code arrives; the
        # 3-byte AUD completes on its own.
        self.assertEqual([H265.nal_type(n) for n in nals], [39, 1, 35])
        self.assertEqual(nals[-1], b"\x46\x01\x50")
        self.assertEqual(buf, b"")

    def test_hevc_access_units(self):
        for fmt in ("byte-stream", "avc"):
            buf = bytearray(self._stream(fmt))
            if fmt == "avc":
                nals = list(split_avc(buf))
            else:
                nals = list(split_annexb(buf, final=True, syntax=H265))
            asm = AccessUnitAssembler(H265)
            aus = [au for au in (asm.feed(n) for n in nals) if au]
            aus.append(asm.flush())
            # The terminating AUD of AU N opens AU N+1, as for H.264.
            self.assertEqual(
                [au.type_summary() for au in aus],
                ["SEI,VPS,SPS,PPS,IDR_N_LP", "AUD,SEI,TRAIL_R", "AUD"],
            )
            self.assertTrue(aus[0].is_keyframe)
            self.assertEqual(aus[0].keyframe_label, "IDR")
            self.assertFalse(aus[1].is_keyframe)
            stamps = [
                extract_sei_timestamp(p).timestamp_us
                for au in aus[:2]
                for p in au.sei_payloads()
            ]
            self.assertEqual(stamps, [1000, 2000])

    def test_suffix_sei_stays_in_access_unit(self):
        asm = AccessUnitAssembler(H265)
        for nal in (IDR, SUFFIX_SEI):
            self.assertIsNone(asm.feed(nal))
        done = asm.feed(TRAIL)
        self.assertEqual(done.type_summary(), "IDR_N_LP,suffix-SEI")
        # A second slice of the same picture (first_slice_segment flag 0) too.
        self.assertIsNone(asm.feed(b"\x02\x01\x40\x10"))
        self.assertEqual(asm.flush().type_summary(), "TRAIL_Rx2")


if __name__ == "__main__":
    unittest.main()
