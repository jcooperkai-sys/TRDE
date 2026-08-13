"""Value model, record serialization and order-preserving key encoding.

Quarry values are Python ``None``, ``int``, ``float``, ``str`` or ``bytes``,
mapping to SQL NULL, INTEGER, REAL, TEXT and BLOB.

Two independent encodings live here:

``serialize_record`` / ``deserialize_record``
    Compact tagged encoding used for row payloads in heap pages.  Ordering is
    irrelevant, so integers are varint encoded.

``encode_key`` / ``encode_index_key``
    Order-preserving byte strings for B+tree keys: ``a < b`` as SQL values iff
    ``encode_key(a) < encode_key(b)`` as byte strings.  Numbers of both kinds
    share a tag and a leading double so an INTEGER and a REAL holding the same
    magnitude sort next to each other; the exact value follows so distinct
    values never collide.
"""

import struct

from .errors import TypeMismatchError

# Declared column types.
INTEGER = "INTEGER"
REAL = "REAL"
TEXT = "TEXT"
BLOB = "BLOB"
ANY = "ANY"

TYPE_NAMES = {
    "INT": INTEGER, "INTEGER": INTEGER, "BIGINT": INTEGER, "SMALLINT": INTEGER,
    "REAL": REAL, "FLOAT": REAL, "DOUBLE": REAL, "NUMERIC": REAL, "DECIMAL": REAL,
    "TEXT": TEXT, "VARCHAR": TEXT, "CHAR": TEXT, "STRING": TEXT,
    "BLOB": BLOB, "BYTES": BLOB,
    "ANY": ANY, "": ANY,
}

# -- record tags ---------------------------------------------------------
_T_NULL = 0
_T_INT = 1
_T_REAL = 2
_T_TEXT = 3
_T_BLOB = 4
_T_TRUE = 5
_T_FALSE = 6


def normalize_type(name):
    key = (name or "").upper().split("(")[0].strip()
    if key not in TYPE_NAMES:
        raise TypeMismatchError("unknown column type %r" % name)
    return TYPE_NAMES[key]


def coerce(value, declared):
    """Coerce ``value`` into ``declared``; raises TypeMismatchError if impossible."""
    if value is None:
        return None
    if declared == ANY:
        return value
    if declared == INTEGER:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value != int(value):
                raise TypeMismatchError("cannot store %r in an INTEGER column" % value)
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                raise TypeMismatchError("cannot store text %r in an INTEGER column" % value)
        raise TypeMismatchError("cannot store %r in an INTEGER column" % (value,))
    if declared == REAL:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise TypeMismatchError("cannot store text %r in a REAL column" % value)
        raise TypeMismatchError("cannot store %r in a REAL column" % (value,))
    if declared == TEXT:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return format_number(value)
        if isinstance(value, bytes):
            raise TypeMismatchError("cannot store a BLOB in a TEXT column")
        raise TypeMismatchError("cannot store %r in a TEXT column" % (value,))
    if declared == BLOB:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        raise TypeMismatchError("cannot store %r in a BLOB column" % (value,))
    raise TypeMismatchError("unknown declared type %r" % declared)


def format_number(value):
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e16:
            return str(int(value))
        return repr(value)
    return str(value)


def type_of(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return INTEGER
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return REAL
    if isinstance(value, str):
        return TEXT
    return BLOB


_SORT_CLASS = {"NULL": 0, INTEGER: 1, REAL: 1, TEXT: 2, BLOB: 3}


def compare(a, b):
    """Total ordering over values; NULL sorts first, numbers before text."""
    ca = _SORT_CLASS[type_of(a)]
    cb = _SORT_CLASS[type_of(b)]
    if ca != cb:
        return -1 if ca < cb else 1
    if ca == 0:
        return 0
    if a == b:
        return 0
    return -1 if a < b else 1


# -- varint --------------------------------------------------------------
def _put_varint(out, n):
    zig = ((-n) << 1) - 1 if n < 0 else n << 1
    while True:
        byte = zig & 0x7F
        zig >>= 7
        if zig:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _get_varint(buf, pos):
    shift = 0
    zig = 0
    while True:
        byte = buf[pos]
        pos += 1
        zig |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    if zig & 1:
        return -((zig + 1) >> 1), pos
    return zig >> 1, pos


# -- record serialization -------------------------------------------------
def serialize_record(values):
    out = bytearray()
    _put_varint(out, len(values))
    for value in values:
        if value is None:
            out.append(_T_NULL)
        elif value is True:
            out.append(_T_TRUE)
        elif value is False:
            out.append(_T_FALSE)
        elif isinstance(value, int):
            out.append(_T_INT)
            _put_varint(out, value)
        elif isinstance(value, float):
            out.append(_T_REAL)
            out += struct.pack("<d", value)
        elif isinstance(value, str):
            data = value.encode("utf-8")
            out.append(_T_TEXT)
            _put_varint(out, len(data))
            out += data
        elif isinstance(value, bytes):
            out.append(_T_BLOB)
            _put_varint(out, len(value))
            out += value
        else:
            raise TypeMismatchError("cannot serialize %r" % (value,))
    return bytes(out)


def deserialize_record(data):
    count, pos = _get_varint(data, 0)
    values = []
    for _ in range(count):
        tag = data[pos]
        pos += 1
        if tag == _T_NULL:
            values.append(None)
        elif tag == _T_TRUE:
            values.append(True)
        elif tag == _T_FALSE:
            values.append(False)
        elif tag == _T_INT:
            n, pos = _get_varint(data, pos)
            values.append(n)
        elif tag == _T_REAL:
            values.append(struct.unpack_from("<d", data, pos)[0])
            pos += 8
        elif tag == _T_TEXT:
            n, pos = _get_varint(data, pos)
            values.append(data[pos:pos + n].decode("utf-8"))
            pos += n
        elif tag == _T_BLOB:
            n, pos = _get_varint(data, pos)
            values.append(bytes(data[pos:pos + n]))
            pos += n
        else:
            raise TypeMismatchError("unknown record tag %d" % tag)
    return values


# -- order-preserving key encoding ---------------------------------------
_K_NULL = b"\x00"
_K_NUM = b"\x10"
_K_TEXT = b"\x20"
_K_BLOB = b"\x30"


def _double_bits(value):
    bits = struct.unpack("<Q", struct.pack("<d", value))[0]
    if bits & 0x8000000000000000:
        bits ^= 0xFFFFFFFFFFFFFFFF
    else:
        bits |= 0x8000000000000000
    return struct.pack(">Q", bits)


def _int_bits(value):
    clamped = max(-(2 ** 63), min(2 ** 63 - 1, value))
    return struct.pack(">Q", clamped + 2 ** 63)


def _escape(data):
    return data.replace(b"\x00", b"\x00\xff") + b"\x00\x00"


def encode_key(value):
    """Order-preserving, self-delimiting encoding of a single value."""
    if value is None:
        return _K_NULL
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        magnitude = _double_bits(float(value))
        return _K_NUM + magnitude + b"\x00" + _int_bits(value)
    if isinstance(value, float):
        return _K_NUM + _double_bits(value) + b"\x01" + struct.pack(">Q", struct.unpack("<Q", struct.pack("<d", value))[0])
    if isinstance(value, str):
        return _K_TEXT + _escape(value.encode("utf-8"))
    if isinstance(value, bytes):
        return _K_BLOB + _escape(value)
    raise TypeMismatchError("cannot use %r as an index key" % (value,))


def encode_prefix(values):
    """Encoding of a partial key tuple (used for range seeks)."""
    return b"".join(encode_key(v) for v in values)


def encode_index_key(values, rowid):
    """Full index key: the column tuple plus the rowid tiebreaker."""
    return encode_prefix(values) + struct.pack(">Q", rowid)


def split_index_key(data):
    """Return (prefix_bytes, rowid) for a key produced by encode_index_key."""
    return data[:-8], struct.unpack(">Q", data[-8:])[0]


def numeric_prefix(value):
    """Prefix shared by an INTEGER and a REAL of the same magnitude."""
    if value is None:
        return _K_NULL
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return _K_NUM + _double_bits(float(value))
    return encode_key(value)
