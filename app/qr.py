"""
A small QR code maker for the live lesson join link - so students scan instead of typing.
Draws the code as an SVG image on the server: no extra package, no internet, prints cleanly.

A Python port of Kazuhiko Arase's "QRCode for JavaScript" (MIT licence, (c) 2009 Kazuhiko
Arase, http://www.d-project.com/). "QR Code" is a registered trademark of DENSO WAVE
INCORPORATED. Only what a join link needs is kept: byte mode, sizes (versions) 1-10, all
four error-correction levels. tools/test_qr.py checks it, square for square, against the
JavaScript original.
"""

LEVEL_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}
_LEVEL_ROW = {"L": 0, "M": 1, "Q": 2, "H": 3}
MAX_VERSION = 10

# (count, total, data) groups per version 1-10 and level L, M, Q, H - from the original's RS_BLOCK_TABLE
_RS_BLOCKS = [
    [1, 26, 19], [1, 26, 16], [1, 26, 13], [1, 26, 9],
    [1, 44, 34], [1, 44, 28], [1, 44, 22], [1, 44, 16],
    [1, 70, 55], [1, 70, 44], [2, 35, 17], [2, 35, 13],
    [1, 100, 80], [2, 50, 32], [2, 50, 24], [4, 25, 9],
    [1, 134, 108], [2, 67, 43], [2, 33, 15, 2, 34, 16], [2, 33, 11, 2, 34, 12],
    [2, 86, 68], [4, 43, 27], [4, 43, 19], [4, 43, 15],
    [2, 98, 78], [4, 49, 31], [2, 32, 14, 4, 33, 15], [4, 39, 13, 1, 40, 14],
    [2, 121, 97], [2, 60, 38, 2, 61, 39], [4, 40, 18, 2, 41, 19], [4, 40, 14, 2, 41, 15],
    [2, 146, 116], [3, 58, 36, 2, 59, 37], [4, 36, 16, 4, 37, 17], [4, 36, 12, 4, 37, 13],
    [2, 86, 68, 2, 87, 69], [4, 69, 43, 1, 70, 44], [6, 43, 19, 2, 44, 20], [6, 43, 15, 2, 44, 16],
]
_PATTERN_POSITIONS = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]]
_G15 = 0b10100110111
_G18 = 0b1111100100101
_G15_MASK = 0b101010000010010

_EXP = [0] * 256
_LOG = [0] * 256
for _i in range(8):
    _EXP[_i] = 1 << _i
for _i in range(8, 256):
    _EXP[_i] = _EXP[_i - 4] ^ _EXP[_i - 5] ^ _EXP[_i - 6] ^ _EXP[_i - 8]
for _i in range(255):
    _LOG[_EXP[_i]] = _i


def _gexp(n):
    return _EXP[n % 255]


def _poly(num, shift=0):
    offset = 0
    while offset < len(num) and num[offset] == 0:
        offset += 1
    return list(num[offset:]) + [0] * shift


def _poly_multiply(a, b):
    out = [0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            if x and y:
                out[i + j] ^= _gexp(_LOG[x] + _LOG[y])
    return _poly(out)


def _poly_mod(a, b):
    while len(a) - len(b) >= 0:
        ratio = _LOG[a[0]] - _LOG[b[0]]
        a = list(a)
        for x in range(len(b)):
            a[x] ^= _gexp(_LOG[b[x]] + ratio) if b[x] else 0
        a = _poly(a)
        if not a:
            break
    return a


def _bch_digit(data):
    digit = 0
    while data:
        digit += 1
        data >>= 1
    return digit


def _bch_type_info(data):
    d = data << 10
    while _bch_digit(d) - _bch_digit(_G15) >= 0:
        d ^= _G15 << (_bch_digit(d) - _bch_digit(_G15))
    return ((data << 10) | d) ^ _G15_MASK


def _bch_type_number(data):
    d = data << 12
    while _bch_digit(d) - _bch_digit(_G18) >= 0:
        d ^= _G18 << (_bch_digit(d) - _bch_digit(_G18))
    return (data << 12) | d


_MASKS = [
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0,
]


def _rs_blocks(version, level):
    row = _RS_BLOCKS[(version - 1) * 4 + _LEVEL_ROW[level]]
    blocks = []
    for k in range(0, len(row), 3):
        blocks += [(row[k + 1], row[k + 2])] * row[k]
    return blocks  # [(total, data), ...]


class _Bits:
    def __init__(self):
        self.buffer, self.length = [], 0

    def put(self, num, length):
        for i in range(length):
            self.put_bit(((num >> (length - i - 1)) & 1) == 1)

    def put_bit(self, bit):
        if len(self.buffer) <= self.length // 8:
            self.buffer.append(0)
        if bit:
            self.buffer[self.length // 8] |= 0x80 >> (self.length % 8)
        self.length += 1


def _length_bits(version):
    return 8 if version < 10 else 16  # byte mode


def _data_bits(data, version):
    bits = _Bits()
    bits.put(4, 4)  # byte mode
    bits.put(len(data), _length_bits(version))
    for b in data:
        bits.put(b, 8)
    return bits


def _create_data(version, level, data):
    blocks = _rs_blocks(version, level)
    bits = _data_bits(data, version)
    capacity = sum(d for _, d in blocks) * 8
    if bits.length > capacity:
        raise ValueError("too long for this QR size")
    if bits.length + 4 <= capacity:
        bits.put(0, 4)
    while bits.length % 8:
        bits.put_bit(False)
    while bits.length < capacity:
        bits.put(0xEC, 8)
        if bits.length >= capacity:
            break
        bits.put(0x11, 8)
    offset, dc, ec = 0, [], []
    for total, count in blocks:
        dc.append([b & 0xFF for b in bits.buffer[offset:offset + count]])
        offset += count
        rs = [1]
        for i in range(total - count):
            rs = _poly_multiply(rs, [1, _gexp(i)])
        mod = _poly_mod(_poly(dc[-1], len(rs) - 1), rs)
        ec.append([mod[x + len(mod) - (len(rs) - 1)] if x + len(mod) - (len(rs) - 1) >= 0 else 0 for x in range(len(rs) - 1)])
    out = []
    for z in range(max(len(d) for d in dc)):
        out += [d[z] for d in dc if z < len(d)]
    for z in range(max(len(e) for e in ec)):
        out += [e[z] for e in ec if z < len(e)]
    return out


def _build(version, level, codewords, mask, test):
    n = version * 4 + 17
    m = [[None] * n for _ in range(n)]

    def probe(row, col):
        for r in range(-1, 8):
            for c in range(-1, 8):
                if 0 <= row + r < n and 0 <= col + c < n:
                    m[row + r][col + c] = ((0 <= r <= 6 and c in (0, 6)) or (0 <= c <= 6 and r in (0, 6)) or (2 <= r <= 4 and 2 <= c <= 4))
    probe(0, 0)
    probe(n - 7, 0)
    probe(0, n - 7)
    pos = _PATTERN_POSITIONS[version - 1]
    for row in pos:
        for col in pos:
            if m[row][col] is not None:
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    m[row + r][col + c] = abs(r) == 2 or abs(c) == 2 or (r == 0 and c == 0)
    for i in range(8, n - 8):
        if m[i][6] is None:
            m[i][6] = i % 2 == 0
        if m[6][i] is None:
            m[6][i] = i % 2 == 0
    bits = _bch_type_info((LEVEL_BITS[level] << 3) | mask)
    for v in range(15):
        mod = (not test) and ((bits >> v) & 1) == 1
        if v < 6:
            m[v][8] = mod
        elif v < 8:
            m[v + 1][8] = mod
        else:
            m[n - 15 + v][8] = mod
    for h in range(15):
        mod = (not test) and ((bits >> h) & 1) == 1
        if h < 8:
            m[8][n - h - 1] = mod
        elif h < 9:
            m[8][15 - h] = mod
        else:
            m[8][15 - h - 1] = mod
    m[n - 8][8] = not test
    if version >= 7:
        tbits = _bch_type_number(version)
        for i in range(18):
            mod = (not test) and ((tbits >> i) & 1) == 1
            m[i // 3][i % 3 + n - 11] = mod
            m[i % 3 + n - 11][i // 3] = mod
    inc, row, bit, byte = -1, n - 1, 7, 0
    col = n - 1
    while col > 0:
        if col == 6:
            col -= 1
        while True:
            for c in range(2):
                if m[row][col - c] is None:
                    dark = byte < len(codewords) and ((codewords[byte] >> bit) & 1) == 1
                    if _MASKS[mask](row, col - c):
                        dark = not dark
                    m[row][col - c] = dark
                    bit -= 1
                    if bit == -1:
                        byte += 1
                        bit = 7
            row += inc
            if row < 0 or row >= n:
                row -= inc
                inc = -inc
                break
        col -= 2
    return m


def _lost_points(m):
    n = len(m)
    lost = 0
    for row in range(n):
        for col in range(n):
            same = 0
            dark = m[row][col]
            for r in (-1, 0, 1):
                if not 0 <= row + r < n:
                    continue
                for c in (-1, 0, 1):
                    if not 0 <= col + c < n or (r == 0 and c == 0):
                        continue
                    same += dark == m[row + r][col + c]
            if same > 5:
                lost += 3 + same - 5
    for row in range(n - 1):
        for col in range(n - 1):
            count = m[row][col] + m[row + 1][col] + m[row][col + 1] + m[row + 1][col + 1]
            if count in (0, 4):
                lost += 3
    pattern = [True, False, True, True, True, False, True]
    for row in range(n):
        for col in range(n - 6):
            if [m[row][col + k] for k in range(7)] == pattern:
                lost += 40
    for col in range(n):
        for row in range(n - 6):
            if [m[row + k][col] for k in range(7)] == pattern:
                lost += 40
    dark = sum(sum(r) for r in m)
    lost += abs(100 * dark / n / n - 50) / 5 * 10
    return lost


def qr_matrix(text: str, level: str = "M") -> list[list[bool]]:
    """The QR code for `text` as rows of True (dark) / False (light), smallest size that fits."""
    data = text.encode("utf-8")
    for version in range(1, MAX_VERSION + 1):
        if _data_bits(data, version).length <= sum(d for _, d in _rs_blocks(version, level)) * 8:
            break
    else:
        raise ValueError("The link is too long for a QR code.")
    codewords = _create_data(version, level, data)
    best, best_mask = None, 0
    for mask in range(8):
        points = _lost_points(_build(version, level, codewords, mask, True))
        if mask == 0 or points < best:
            best, best_mask = points, mask
    return _build(version, level, codewords, best_mask, False)


def qr_svg(text: str, size_px: int = 260, level: str = "M", border: int = 4) -> str:
    """An SVG image of the QR code (with the quiet border scanners need)."""
    m = qr_matrix(text, level)
    n = len(m) + 2 * border
    path = "".join(f"M{c + border},{r + border}h1v1h-1z" for r, row in enumerate(m) for c, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{size_px}" height="{size_px}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code to join"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
