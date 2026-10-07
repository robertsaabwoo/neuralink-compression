"""MSB-first bit packing, mirroring the RTL bit packer.

Convention used by every mode (this is part of the bitstream contract with the RTL):
  * bits are appended MSB-first into bytes,
  * multi-bit fields are written MSB-first,
  * unary(q) is q '1' bits followed by a terminating '0',
  * packets are zero-padded to a byte boundary.
"""

from __future__ import annotations


class BitWriter:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._acc = 0      # pending bits not yet forming a full byte
        self._n = 0        # number of pending bits (< 8 between calls)
        self.nbits = 0     # total bits written, including padding

    def write(self, value: int, n: int) -> None:
        if n == 0:
            return
        if value < 0 or value >> n:
            raise ValueError(f"value {value} does not fit in {n} bits")
        self._acc = (self._acc << n) | value
        self._n += n
        self.nbits += n
        while self._n >= 8:
            self._n -= 8
            self._buf.append((self._acc >> self._n) & 0xFF)
        self._acc &= (1 << self._n) - 1

    def write_unary(self, q: int) -> None:
        """q ones then a zero."""
        self.write((1 << (q + 1)) - 2, q + 1)

    def write_truncated_unary(self, q: int, qmax: int) -> None:
        """q ones then a zero, except q == qmax which drops the terminator."""
        if q > qmax:
            raise ValueError(f"{q} > {qmax}")
        if q == qmax:
            self.write((1 << q) - 1, q)
        else:
            self.write_unary(q)

    def write_bytes(self, data: bytes) -> None:
        for b in data:
            self.write(b, 8)

    def align(self) -> None:
        if self._n:
            self.write(0, 8 - self._n)

    def getvalue(self) -> bytes:
        if self._n:
            raise RuntimeError("bitstream not byte aligned; call align() first")
        return bytes(self._buf)


class BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0  # bit position

    @property
    def bits_left(self) -> int:
        return len(self._data) * 8 - self.pos

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        end = self.pos + n
        if end > len(self._data) * 8:
            raise EOFError("read past end of bitstream")
        first, last = self.pos >> 3, (end + 7) >> 3
        chunk = int.from_bytes(self._data[first:last], "big")
        self.pos = end
        return (chunk >> (last * 8 - end)) & ((1 << n) - 1)

    def read_unary(self, limit: int | None = None) -> int:
        """Count '1' bits up to the terminating '0'. If `limit` ones are read,
        stop without consuming a terminator (used for escapes / truncated unary)."""
        q = 0
        while limit is None or q < limit:
            if not self.read(1):
                return q
            q += 1
        return q

    def read_bytes(self, n: int) -> bytes:
        return bytes(self.read(8) for _ in range(n))

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7
