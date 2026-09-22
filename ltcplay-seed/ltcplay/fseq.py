"""FSEQ v1/v2 reader.

Format read directly from the xLights source of record:
  src-core/render/FSEQFile.cpp  (header layout ~line 440-460, V2 block table
  ~line 2210-2265, write side ~line 2076-2130) and the constants at line 838:
  V2FSEQ_HEADER_SIZE=32, V2FSEQ_SPARSE_RANGE_SIZE=6, V2FSEQ_COMPRESSION_BLOCK_SIZE=8.

Fixed header (both versions), little endian:
  0..3   'PSEQ' (or 'ESEQ' for the single-model variant)
  4..5   channel data offset (uint16)
  6      version minor
  7      version major
  8..9   header/standard size (v1) or fixed header size (v2)
  10..13 channel count per frame (uint32)
  14..17 number of frames (uint32)
  18     step time in milliseconds
  19     flags
  V2 only:
  20     high nibble = bits 11..8 of block count, low nibble = compression type
         (0 none, 1 zstd, 2 zlib)
  21     block count low 8 bits
  22     number of sparse ranges
  23     reserved
  24..31 uuid
  then   block table   (block count * 8 bytes: uint32 firstFrame, uint32 length)
  then   sparse ranges (count * 6 bytes: uint24 start, uint24 length)
  then   variable headers, then channel data at offset from bytes 4..5
"""
import struct, zlib, os

NONE, ZSTD, ZLIB = 0, 1, 2
_COMP_NAMES = {NONE: "none", ZSTD: "zstd", ZLIB: "zlib"}


def _u16(b, o): return b[o] | (b[o + 1] << 8)
def _u24(b, o): return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16)
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]


class FSEQError(Exception):
    pass


class FSEQ:
    """Random-access reader. Decompresses one block at a time and caches it."""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "rb")
        self._file_size = os.path.getsize(path)
        head = self._f.read(32)
        if len(head) < 32:
            raise FSEQError(f"{path}: shorter than a header")
        magic = bytes(head[0:4])
        if magic not in (b"PSEQ", b"ESEQ"):
            raise FSEQError(f"{path}: not an FSEQ, magic is {magic!r}")

        self.chan_data_offset = _u16(head, 4)
        self.version_minor = head[6]
        self.version_major = head[7]
        self.channel_count = _u32(head, 10)
        self.frame_count = _u32(head, 14)
        self.step_time_ms = head[18]
        if self.step_time_ms == 0:
            raise FSEQError(f"{path}: step time is 0")
        if self.channel_count == 0:
            raise FSEQError(f"{path}: channel count is 0")

        self.compression = NONE
        self.sparse_ranges = []
        self._blocks = []  # (first_frame, file_offset, length)

        if self.version_major >= 2:
            self.compression = head[20] & 0x0F
            if self.compression not in _COMP_NAMES:
                raise FSEQError(f"{path}: unknown compression type {self.compression}")
            n_blocks = ((head[20] & 0xF0) << 4) | head[21]
            n_sparse = head[22]

            tbl_len = n_blocks * 8 + n_sparse * 6
            self._f.seek(32)
            tbl = self._f.read(tbl_len)
            if len(tbl) < tbl_len:
                raise FSEQError(f"{path}: block/sparse table truncated")

            off = self.chan_data_offset
            pos = 0
            last_first = 0
            for i in range(n_blocks):
                first_frame = _u32(tbl, pos)
                length = _u32(tbl, pos + 4)
                pos += 8
                if length == 0:
                    continue
                # xLights clamps an out-of-order table rather than trusting it
                # (FSEQFile.cpp ~2243). Same rule here: a corrupt table must not
                # index off the front of a decompressed block.
                if first_frame < last_first or (not self._blocks and first_frame != 0):
                    first_frame = last_first
                self._blocks.append((first_frame, off, length))
                last_first = first_frame
                off += length
            for i in range(n_sparse):
                s = _u24(tbl, pos); ln = _u24(tbl, pos + 3); pos += 6
                self.sparse_ranges.append((s, ln))

        if not self._blocks:
            # v1, or v2 stored uncompressed as a single span
            self._blocks = [(0, self.chan_data_offset,
                             self._file_size - self.chan_data_offset)]
            if self.version_major >= 2 and self.compression != NONE:
                raise FSEQError(f"{path}: compressed but the block table is empty")

        # Variable headers sit between the tables and the channel data. They
        # are how a sequence says what it actually is: xLights writes the media
        # file it was rendered against ("mf") and its own version ("sp"). A
        # filename can be anything; the media path inside the file is evidence.
        self.variables = {}
        self._read_variables()

        self._cache_idx = -1
        self._cache = b""

    def _read_variables(self):
        if self.version_major < 2:
            return
        n_blocks = 0
        n_sparse = 0
        self._f.seek(20)
        h = self._f.read(3)
        if len(h) < 3:
            return
        n_blocks = ((h[0] & 0xF0) << 4) | h[1]
        n_sparse = h[2]
        start = 32 + n_blocks * 8 + n_sparse * 6
        if start >= self.chan_data_offset:
            return
        self._f.seek(start)
        blob = self._f.read(self.chan_data_offset - start)
        o = 0
        while o + 4 <= len(blob):
            ln = _u16(blob, o)
            code = bytes(blob[o + 2:o + 4])
            if ln < 4 or o + ln > len(blob):
                break
            try:
                txt = blob[o + 4:o + ln].split(b"\x00")[0].decode("utf-8")
            except UnicodeDecodeError:
                txt = None
            if txt is not None:
                self.variables.setdefault(code.decode("ascii", "replace"), txt)
            o += ln

    @property
    def media_file(self):
        """The audio this sequence was rendered against, as xLights wrote it.

        This is the one field that proves identity. The filename on disk is
        whatever somebody typed; this is what the renderer was actually looking
        at when it produced the frames."""
        return self.variables.get("mf") or None

    @property
    def renderer(self):
        return self.variables.get("sp") or None

    # -- properties -------------------------------------------------------
    @property
    def compression_name(self): return _COMP_NAMES[self.compression]

    @property
    def duration_ms(self): return self.frame_count * self.step_time_ms

    def close(self):
        if self._f:
            self._f.close(); self._f = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # -- frame access -----------------------------------------------------
    def _block_for(self, frame):
        """Index of the block containing `frame`. Blocks are ordered by first frame."""
        lo, hi = 0, len(self._blocks) - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._blocks[mid][0] <= frame:
                best = mid; lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _load_block(self, idx):
        if idx == self._cache_idx:
            return self._cache
        first, off, length = self._blocks[idx]
        self._f.seek(off)
        raw = self._f.read(length)
        if len(raw) != length:
            raise FSEQError(f"{self.path}: block {idx} truncated "
                            f"({len(raw)} of {length} bytes)")
        if self.compression == ZSTD:
            import zstandard
            raw = zstandard.ZstdDecompressor().decompress(
                raw, max_output_size=self.channel_count * (self.frame_count + 1))
        elif self.compression == ZLIB:
            raw = zlib.decompress(raw)
        self._cache_idx = idx
        self._cache = raw
        return raw

    def frame(self, n):
        """Channel bytes for frame n. Returns exactly channel_count bytes.

        For a sparse file these are the sparse channels concatenated, which is
        what the sparse_ranges describe; the caller maps them out."""
        if n < 0 or n >= self.frame_count:
            raise IndexError(f"frame {n} outside 0..{self.frame_count - 1}")
        idx = self._block_for(n)
        blk = self._load_block(idx)
        rel = n - self._blocks[idx][0]
        start = rel * self.channel_count
        end = start + self.channel_count
        if end > len(blk):
            raise FSEQError(f"{self.path}: frame {n} runs past the end of block "
                            f"{idx} ({end} > {len(blk)})")
        return blk[start:end]

    def verify(self):
        """Prove the file can actually be READ, not just opened.

        Opening an FSEQ reads the header and the block table, which xLights
        writes early. The channel data comes after, so a render that is still
        being written opens perfectly and then fails on the first frame with
        a zstd error. That is how a half-written Monster Mash reached the rig
        and produced 810 read errors in twenty seconds.

        Two checks. The block table must not claim more bytes than the file
        has, which catches a truncated write instantly and for free; and the
        first, middle and last frames must decompress, which catches the rest.
        Raises FSEQError; the caller treats that as "not ready yet".
        """
        try:
            size = os.path.getsize(self.path)
        except OSError as e:
            raise FSEQError(f"{self.path}: {e}")
        for i, (first, off, length) in enumerate(self._blocks):
            if off + length > size:
                raise FSEQError(
                    f"{os.path.basename(self.path)}: block {i} runs past the "
                    f"end of the file ({off + length} bytes claimed, "
                    f"{size} on disk). The render is not finished.")
        if self.frame_count <= 0:
            raise FSEQError(f"{os.path.basename(self.path)}: no frames")
        for n in {0, self.frame_count // 2, self.frame_count - 1}:
            try:
                got = self.frame(n)
            except FSEQError:
                raise
            except Exception as e:
                raise FSEQError(
                    f"{os.path.basename(self.path)}: frame {n} will not "
                    f"decompress ({e}). The render is not finished.")
            if len(got) != self.channel_count:
                raise FSEQError(
                    f"{os.path.basename(self.path)}: frame {n} came back "
                    f"{len(got)} bytes, not {self.channel_count}.")
        # Drop whatever the probe pulled in. An uncompressed or v1 FSEQ is one
        # block -- the whole file -- so verifying every cue at startup pinned
        # a whole set in RAM: 428MB of renders became 420MB of resident
        # memory before a frame was played. Found by round 2 of the audit,
        # 2026-09-13. The first real frame will load what it needs.
        self._cache_idx = -1
        self._cache = b""
        return True

    def describe(self):
        return (f"{os.path.basename(self.path)}: v{self.version_major}.{self.version_minor} "
                f"{self.channel_count} ch x {self.frame_count} frames @ {self.step_time_ms}ms "
                f"({self.duration_ms/1000:.1f}s) compression={self.compression_name} "
                f"blocks={len(self._blocks)} sparse={len(self.sparse_ranges)}")
