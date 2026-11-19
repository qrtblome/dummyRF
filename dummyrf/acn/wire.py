"""ACN wire format: root layer, SDT and DMP PDUs (ANSI E1.17).

Every PDU starts with flags+length (12 bits, or 20 with the L flag); the V, H
and D flags say whether vector, header and data are present — absent parts are
inherited from the previous PDU in the same block.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PREAMBLE = b"\x00\x10\x00\x00ASC-E1.17\x00\x00\x00"

# root layer vectors / client protocols
PROTO_SDT = 1
PROTO_DMP = 2

# SDT vectors
REL_WRAP, UNREL_WRAP = 1, 2
JOIN, JOIN_REFUSE, JOIN_ACCEPT, LEAVE, LEAVING = 4, 5, 6, 7, 8
CONNECT, CONNECT_ACCEPT, CONNECT_REFUSE, DISCONNECT, DISCONNECTING = 9, 10, 11, 12, 13
ACK, NAK = 14, 15

# DMP vectors
GET, SET, GET_REPLY, EVENT = 1, 2, 3, 4
SUBSCRIBE, UNSUBSCRIBE = 7, 8
GET_FAIL, SET_FAIL = 9, 10
SUBSCRIBE_ACCEPT, SUBSCRIBE_REJECT = 12, 13
SYNC_EVENT = 17

NO_MEMBER = 0xFFFF
# channel parameters WWB uses: expiry 5 s, no NAK outbound, NAK holdoff 2 ms,
# NAK modulus 50, NAK max wait 20 ms
CHANNEL_PARAMS = bytes([0x05, 0x00]) + struct.pack("!HHH", 2, 50, 20)
DMP_ADDR4 = 0x02        # DMP header: single 4-byte absolute address
DMP_RANGE_ARRAY4 = 0x22  # DMP header: 4-byte address, range, array of values


# ------------------------------------------------------------------ building
def pdu(vector: bytes, header: bytes, data: bytes) -> bytes:
    length = 2 + len(vector) + len(header) + len(data)
    if length < 0x1000:
        return struct.pack("!H", 0x7000 | length) + vector + header + data
    length += 1
    return bytes([0xF0 | (length >> 16), (length >> 8) & 0xFF, length & 0xFF]) + vector + header + data


def data_only(data: bytes) -> bytes:
    """A PDU that inherits vector and header from the previous one."""
    return struct.pack("!H", 0x1000 | (2 + len(data))) + data


def root(cid: bytes, sdt: bytes) -> bytes:
    return PREAMBLE + pdu(struct.pack("!I", PROTO_SDT), cid, sdt)


def sdt(vector: int, data: bytes) -> bytes:
    return pdu(bytes([vector]), b"", data)


def join(device_cid: bytes, member: int, channel: int, seq: int) -> bytes:
    return sdt(JOIN, device_cid + struct.pack("!HHHII", member, channel, 0, seq, seq)
               + b"\x00" + CHANNEL_PARAMS + b"\xff")


def join_accept(leader_cid: bytes, channel: int, member: int, rel_seq: int, reciprocal: int) -> bytes:
    return sdt(JOIN_ACCEPT, leader_cid + struct.pack("!HHIH", channel, member, rel_seq, reciprocal))


def wrapper(kind: int, channel: int, total: int, rel: int, oldest: int, blocks: bytes,
            first: int = NO_MEMBER, last: int = NO_MEMBER) -> bytes:
    return sdt(kind, struct.pack("!HIIIHHH", channel, total, rel, oldest, first, last, 0) + blocks)


def client_block(member: int, protocol: int, inner: bytes, association: int = 0) -> bytes:
    return pdu(struct.pack("!H", member), struct.pack("!IH", protocol, association), inner)


def dmp(vector: int, items: list[bytes], header: int = DMP_ADDR4) -> bytes:
    """Several DMP PDUs of one kind; items are address (+ value) bytes."""
    out = b""
    for i, item in enumerate(items):
        out += pdu(bytes([vector]), bytes([header]), item) if i == 0 else data_only(item)
    return out


def addr(a: int) -> bytes:
    return struct.pack("!I", a)


# ------------------------------------------------------------------ parsing
def split(buf: bytes, vsize: int, hsize: int):
    """Yield (vector, header, data) for each PDU, applying inheritance."""
    p, vec, hdr, data = 0, b"", b"", b""
    while p + 2 <= len(buf):
        fl = buf[p]
        if fl & 0x80:
            length = ((fl & 0x0F) << 16) | (buf[p + 1] << 8) | buf[p + 2]
            q = p + 3
        else:
            length = ((fl & 0x0F) << 8) | buf[p + 1]
            q = p + 2
        if length < 2 or p + length > len(buf):
            return
        end = p + length
        if fl & 0x40:
            vec = buf[q:q + vsize]
            q += vsize
        if fl & 0x20:
            hdr = buf[q:q + hsize]
            q += hsize
        if fl & 0x10:
            data = buf[q:end]
        yield vec, hdr, data
        p = end


@dataclass
class DmpItem:
    op: int            # DMP vector
    address: int
    increment: int = 0
    count: int = 1
    value: bytes = b""     # raw value bytes (array: count * size)
    reason: int | None = None   # for *_FAIL / SUBSCRIBE_REJECT


_NO_VALUE = {GET, SUBSCRIBE, UNSUBSCRIBE, SUBSCRIBE_ACCEPT}
_REASON = {GET_FAIL, SET_FAIL, SUBSCRIBE_REJECT}


def parse_dmp(vector: int, header: int, data: bytes) -> list[DmpItem]:
    """Decode one DMP PDU.  Values of single-address messages take the rest of
    the PDU (WWB and the SLX-D send one property per PDU)."""
    asz = {0: 1, 1: 2, 2: 4}.get(header & 3, 4)
    rng = (header >> 4) & 3
    out, p = [], 0
    while p + asz <= len(data):
        a = int.from_bytes(data[p:p + asz], "big")
        p += asz
        inc, cnt = 0, 1
        if rng:
            inc = int.from_bytes(data[p:p + asz], "big")
            cnt = int.from_bytes(data[p + asz:p + 2 * asz], "big")
            p += 2 * asz
        if vector in _NO_VALUE:
            out.append(DmpItem(vector, a, inc, cnt))
        elif vector in _REASON:
            out.append(DmpItem(vector, a, inc, cnt, reason=data[p] if p < len(data) else None))
            p += 1
        else:
            out.append(DmpItem(vector, a, inc, cnt, data[p:]))
            p = len(data)
    return out


def int_value(b: bytes, signed: bool = False) -> int:
    return int.from_bytes(b, "big", signed=signed)
