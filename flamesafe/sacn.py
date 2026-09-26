"""flamesafe sACN: one ANSI E1.31 data packet builder, priority 200.

Written here rather than borrowed from ltcplay/output.py because the two
programs share no code (the wall).  Offsets are the ones in the E1.31
standard; the tests assert them independently against the bytes.
"""

from __future__ import annotations

import uuid

from . import rules

HEADER_LEN = 126
CID = uuid.uuid5(uuid.NAMESPACE_DNS, "flamesafe.jeffholmespresents").bytes
SOURCE_NAME = b"flamesafe"
OPT_STREAM_TERMINATED = 0x40


def build_packet(universe, values, seq, priority=rules.SACN_PRIORITY,
                 terminated=False):
    """One E1.31 data packet carrying all 512 slots of `values`.

    `priority` defaults to 200, the maximum, and nothing in this program
    passes anything else.  `terminated` sets the stream-terminated option
    bit, sent on a clean shutdown after zeros have gone out.
    """
    if len(values) != rules.UNIVERSE_SIZE:
        raise ValueError("a flame universe frame is exactly 512 slots")
    if not (0 <= priority <= 200):
        raise ValueError("sACN priority is 0 to 200")
    count = rules.UNIVERSE_SIZE
    total = HEADER_LEN + count
    b = bytearray(total)
    b[0] = 0x00
    b[1] = 0x10                                   # preamble size 0x0010
    b[2] = 0x00
    b[3] = 0x00                                   # post-amble size
    b[4:16] = b"ASC-E1.17\x00\x00\x00"
    b[16] = 0x70 | (((total - 16) >> 8) & 0x0F)   # root flags and length
    b[17] = (total - 16) & 0xFF
    b[18:22] = b"\x00\x00\x00\x04"                # VECTOR_ROOT_E131_DATA
    b[22:38] = CID
    b[38] = 0x70 | (((total - 38) >> 8) & 0x0F)   # framing flags and length
    b[39] = (total - 38) & 0xFF
    b[40:44] = b"\x00\x00\x00\x02"                # VECTOR_E131_DATA_PACKET
    b[44:44 + len(SOURCE_NAME)] = SOURCE_NAME
    b[108] = priority
    b[109] = 0
    b[110] = 0                                    # synchronization address
    b[111] = seq & 0xFF
    b[112] = OPT_STREAM_TERMINATED if terminated else 0
    b[113] = (universe >> 8) & 0xFF
    b[114] = universe & 0xFF
    b[115] = 0x70 | (((total - 115) >> 8) & 0x0F)  # DMP flags and length
    b[116] = (total - 115) & 0xFF
    b[117] = 0x02                                 # DMP set property
    b[118] = 0xA1                                 # address and data type
    b[119] = 0
    b[120] = 0                                    # first property address
    b[121] = 0
    b[122] = 0x01                                 # address increment
    n = count + 1                                 # includes the start code
    b[123] = (n >> 8) & 0xFF
    b[124] = n & 0xFF
    b[125] = 0x00                                 # DMX start code
    b[126:] = bytes(values)
    return bytes(b)
