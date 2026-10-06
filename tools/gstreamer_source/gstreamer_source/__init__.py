"""gstreamer-source: cross-platform GStreamer camera / test-pattern source.

Produces H.264 or H.265 (Annex B byte-stream or 4-byte length-prefixed) or raw
NV12 over a TCP server socket or into a file, with optional SEI timestamp /
frame-id metadata injected before every access unit.
"""

from .sei import SEI_UUID, SeiInjector, append_packet_trailer, create_sei_nalu

__all__ = ["SEI_UUID", "SeiInjector", "append_packet_trailer", "create_sei_nalu"]
