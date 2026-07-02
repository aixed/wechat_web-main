"""Raw Protobuf message parser for WeChat Hook RecvType=2 callbacks.

When the Hook is started with RecvType=2, it sends raw protobuf hex data
instead of pre-parsed JSON.  This module decodes that data into the same
message-dict format that the rest of the backend expects.

Based on the parsing example provided by Xed (解析消息.py).
"""

import binascii
import time
from typing import Optional, Dict, List


# ═══════════════════════════════════════════════════════════════════
# PBDecode — lightweight protobuf decoder (no .proto file needed)
# ═══════════════════════════════════════════════════════════════════

class FieldValue:
    __slots__ = ("varint", "data", "sub")

    def __init__(self):
        self.varint: int = 0
        self.data: bytes = b""
        self.sub: Optional["PBDecode"] = None


class PBPathNode:
    __slots__ = ("field", "has_index", "index", "is_loop")

    def __init__(self, field: int, has_index=False, index=0, is_loop=False):
        self.field = field
        self.has_index = has_index
        self.index = index
        self.is_loop = is_loop


class PBDecode:
    def __init__(self):
        self.fields: Dict[int, List[FieldValue]] = {}

    # ─── Entry ───────────────────────────────────────────────────

    def parser(self, data: bytes) -> bool:
        self.fields.clear()
        if not data:
            return False
        return self._parse_internal(data, 0, len(data))

    # ─── Internal ────────────────────────────────────────────────

    def _parse_internal(self, buf: bytes, start: int, length: int) -> bool:
        p = start
        end = start + length
        while p < end:
            key, p = self._read_varint(buf, p, end)
            field = key >> 3
            wire = key & 0x7
            fv = FieldValue()
            if wire == 0:  # varint
                fv.varint, p = self._read_varint(buf, p, end)
            elif wire == 2:  # length-delimited
                l, p = self._read_varint(buf, p, end)
                if p + l > end:
                    return False
                fv.data = buf[p:p + l]
                sub = PBDecode()
                if sub._parse_internal(buf, p, l):
                    fv.sub = sub
                p += l
            else:
                return False
            if field not in self.fields:
                self.fields[field] = []
            self.fields[field].append(fv)
        return True

    # ─── Path parsing ────────────────────────────────────────────

    @staticmethod
    def _parse_path(path: str) -> List[PBPathNode]:
        nodes = []
        i = 0
        n = len(path)
        while i < n:
            field = 0
            while i < n and path[i].isdigit():
                field = field * 10 + int(path[i])
                i += 1
            node = PBPathNode(field)
            if i < n and path[i] == "[":
                i += 1
                if i < n and path[i] in ("i", "n"):
                    node.is_loop = True
                    node.has_index = True
                    node.index = -1
                    i += 1
                else:
                    idx = 0
                    while i < n and path[i].isdigit():
                        idx = idx * 10 + int(path[i])
                        i += 1
                    node.has_index = True
                    node.index = idx
                if i < n and path[i] == "]":
                    i += 1
            nodes.append(node)
            if i < n and path[i] == ".":
                i += 1
        return nodes

    def _get_field(self, nodes: List[PBPathNode], loop_index=None):
        cur = self
        fv = None
        for i, node in enumerate(nodes):
            if node.field not in cur.fields:
                return None
            arr = cur.fields[node.field]
            if node.is_loop:
                if loop_index is None:
                    return None
                idx = loop_index
            else:
                idx = node.index if node.has_index else 0
            if idx >= len(arr):
                return None
            fv = arr[idx]
            if i + 1 < len(nodes):
                if not fv.sub:
                    return None
                cur = fv.sub
        return fv

    # ─── Public API ──────────────────────────────────────────────

    def getVarint(self, path: str, loop_index=None) -> int:
        nodes = self._parse_path(path)
        fv = self._get_field(nodes, loop_index)
        return fv.varint if fv else 0

    def getUtf8Str(self, path: str, loop_index=None) -> str:
        nodes = self._parse_path(path)
        fv = self._get_field(nodes, loop_index)
        if not fv:
            return ""
        try:
            return fv.data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def getBin(self, path: str, loop_index=None) -> bytes:
        nodes = self._parse_path(path)
        fv = self._get_field(nodes, loop_index)
        return fv.data if fv else b""

    def getHex(self, path: str, loop_index=None) -> str:
        return binascii.hexlify(self.getBin(path, loop_index)).decode()

    def getInc(self, path: str) -> int:
        nodes = self._parse_path(path)
        if not nodes:
            return 0
        cur = self
        for node in nodes[:-1]:
            if node.field not in cur.fields:
                return 0
            idx = node.index if node.has_index else 0
            if idx >= len(cur.fields[node.field]):
                return 0
            fv = cur.fields[node.field][idx]
            if not fv.sub:
                return 0
            cur = fv.sub
        last = nodes[-1]
        return len(cur.fields.get(last.field, []))

    @staticmethod
    def _read_varint(buf: bytes, p: int, end: int):
        v = 0
        shift = 0
        while p < end:
            b = buf[p]
            p += 1
            v |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return v, p


# ═══════════════════════════════════════════════════════════════════
# Utility helpers
# ═══════════════════════════════════════════════════════════════════

def hex_to_bytes(hex_str: str) -> bytes:
    return binascii.unhexlify(hex_str)


def bytes_to_hex(data: bytes) -> str:
    return binascii.hexlify(data).decode()


def bytes_to_unicode(data: bytes) -> str:
    try:
        return data.decode('utf-8', errors='ignore')
    except Exception:
        return data.decode('gbk', errors='ignore')


def unix_timestamp_to_str(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def signed_to_unsigned_long(value: int) -> str:
    return str(value & 0xFFFFFFFFFFFFFFFF)


# ═══════════════════════════════════════════════════════════════════
# Main entry point: parse raw pb hex → list of message dicts
# ═══════════════════════════════════════════════════════════════════

def parse_raw_pb(pb_hex: str, self_wxid: str = "") -> list[dict]:
    """Parse raw protobuf hex data into a list of message dicts.

    The returned dicts have the same keys as RecvType=1 callback messages:
        cmdId, msgsvrid, msgtype, time, fromid, toid, fromgid,
        fromtype, msg, msgsource, voice_len, voice_hex, ...

    Parameters
    ----------
    pb_hex : str
        Hex-encoded protobuf data from the ``pb_msg`` callback field.
    self_wxid : str
        The logged-in user's wxid (used for sender detection).

    Returns
    -------
    list[dict]
        Parsed messages (may be empty if nothing useful in the pb data).
    """
    try:
        data_bytes = hex_to_bytes(pb_hex)
    except Exception:
        return []

    pb = PBDecode()
    if not pb.parser(data_bytes):
        return []

    msg_count = pb.getInc("2.2")
    if msg_count <= 0:
        return []

    messages: list[dict] = []

    for i in range(msg_count):
        cmd_id = pb.getVarint(f"2.2[{i}].1")

        # cmd_id == 5 → regular chat message
        if cmd_id != 5:
            continue

        msg_id = pb.getVarint(f"2.2[{i}].2.2.12")
        msg_type = pb.getVarint(f"2.2[{i}].2.2.4")
        msg_time = pb.getVarint(f"2.2[{i}].2.2.9")

        if msg_id == 0 and msg_time == 0:
            continue

        from_id = pb.getUtf8Str(f"2.2[{i}].2.2.2.1")
        to_id = pb.getUtf8Str(f"2.2[{i}].2.2.3.1")
        msg_bytes = pb.getBin(f"2.2[{i}].2.2.5.1")
        msg_data_len = pb.getVarint(f"2.2[{i}].2.2.8.1")
        msg_data_content = pb.getBin(f"2.2[{i}].2.2.8.2")
        msg_source = pb.getUtf8Str(f"2.2[{i}].2.2.10")

        # Decode message content
        msg = bytes_to_unicode(msg_bytes) if msg_bytes else ""

        # Determine if group message
        is_group = bool(from_id and from_id.endswith("@chatroom"))
        from_type = "2" if is_group else "1"
        g_from_wxid = ""

        # Parse actual sender from group message content
        if is_group and msg_bytes:
            msg_hex = bytes_to_hex(msg_bytes)
            addr = msg_hex.find("3A000A00")
            if addr > 0:
                g_from_wxid_hex = msg_hex[:addr]
                try:
                    g_from_wxid = bytes.fromhex(g_from_wxid_hex).decode('utf-8', errors='ignore')
                    msg_hex = msg_hex[addr + len("3A000A00"):]
                    msg_bytes = bytes.fromhex(msg_hex)
                    msg = bytes_to_unicode(msg_bytes)
                except Exception:
                    pass

        # Build message dict — same keys as RecvType=1 callback format
        time_str = unix_timestamp_to_str(msg_time) if msg_time else ""

        msg_item: dict = {
            "cmdId": cmd_id,
            "index": str(i + 1),
            "time": time_str,
            "msgtype": str(msg_type),
            "msgsvrid": signed_to_unsigned_long(msg_id),
            "fromtype": from_type,
            "toid": to_id or "",
        }

        if msg_source:
            msg_item["msgsource"] = msg_source

        if is_group:
            msg_item["fromid"] = g_from_wxid if g_from_wxid else from_id
            msg_item["fromgid"] = from_id or ""
            msg_item["msg"] = msg
        else:
            msg_item["fromid"] = from_id or ""
            msg_item["msg"] = msg

        # Type-specific fields
        if msg_type == 50:
            # 语音通话 — skip
            continue

        if msg_type == 34:
            # 语音
            msg_item["voice_len"] = str(msg_data_len) if msg_data_len else "0"
            msg_item["voice_hex"] = bytes_to_hex(msg_data_content) if msg_data_content else ""

        messages.append(msg_item)

    return messages
