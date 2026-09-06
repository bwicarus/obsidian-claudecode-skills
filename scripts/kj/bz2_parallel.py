"""单流 bz2 的并行解压：按块切开、每块拼成独立小流、多进程解。

bzip2 的块（block）彼此独立，只是块边界不按字节对齐；bzip2recover 就是靠扫描 48 位块魔数
0x314159265359 把大文件切成一块一个 .bz2。这里做同样的事，但不落盘：

1. ``scan_blocks``：整文件顺序扫一遍，用 8 个位移模式的 ``bytes.find`` 找块魔数与流结束魔数（0x177245385090）
   的**位偏移**。纯 I/O + memchr 级速度，不解压。
2. ``block_stream``：把第 i 块的位区间取出、字节对齐，前面接 ``BZh9``、后面接 EOS 魔数 + 该块 CRC（单块流的流 CRC
   就等于块 CRC）+ 补零到字节边界 → 一个合法的独立 bz2 流，``bz2.decompress`` 直接吃。
3. ``decompress_blocks``：一组相邻块依次解、拼接，输出与顺序解压的对应区间逐字节相同。

单线程 Python bz2 只有约 3 MB/s 压缩输入（bzip2 的 BWT 逆变换天生慢）；20 核机器上按块并行，瓶颈就变成磁盘与 JSON 解析。
"""
from __future__ import annotations

import bz2
from pathlib import Path
from typing import Iterator

BLOCK_MAGIC = 0x314159265359
EOS_MAGIC = 0x177245385090
_MASK48 = (1 << 48) - 1
_MASK40 = (1 << 40) - 1


def _patterns(magic: int) -> list[tuple[int, bytes, int, int, int]]:
    """每个位移 k 一条：(k, 可直接 find 的字节串, 首字节低位期望, 首字节低位位数, 尾字节高位期望)。
    k=0 时 6 字节整体可直接 find；k>0 时 find 中间 40 位（5 字节），再校验首尾半字节。"""
    out = []
    out.append((0, magic.to_bytes(6, "big"), 0, 0, 0))
    for k in range(1, 8):
        middle = ((magic >> k) & _MASK40).to_bytes(5, "big")
        head_bits = 8 - k                      # 首字节低 (8-k) 位 = 魔数最高 (8-k) 位
        head_val = magic >> (40 + k)
        tail_val = magic & ((1 << k) - 1)      # 尾字节高 k 位 = 魔数最低 k 位
        out.append((k, middle, head_val, head_bits, tail_val))
    return out


_BLOCK_PATTERNS = _patterns(BLOCK_MAGIC)
_EOS_PATTERNS = _patterns(EOS_MAGIC)


def _find_all(buf: bytes, base_bit: int, patterns, out: list[int]) -> None:
    n = len(buf)
    for k, pat, head_val, head_bits, tail_val in patterns:
        j = buf.find(pat)
        while j >= 0:
            if k == 0:
                out.append(base_bit + j * 8)
            else:
                i = j - 1
                if i >= 0 and j + 5 < n:
                    if (buf[i] & ((1 << head_bits) - 1)) == head_val and (buf[j + 5] >> (8 - k)) == tail_val:
                        out.append(base_bit + i * 8 + k)
            j = buf.find(pat, j + 1)


def scan_blocks(path: str | Path, *, chunk: int = 64 * 1024 * 1024, limit_bytes: int | None = None) -> tuple[list[int], list[int]]:
    """返回（块起始位偏移升序列表, 流结束魔数位偏移升序列表）。limit_bytes 只扫前若干字节（试跑/基准）。"""
    p = Path(path)
    blocks: list[int] = []
    eos: list[int] = []
    overlap = 8
    with open(p, "rb") as fh:
        pos = 0
        carry = b""
        while True:
            data = fh.read(chunk)
            if not data:
                break
            buf = carry + data
            base = pos - len(carry)
            _find_all(buf, base * 8, _BLOCK_PATTERNS, blocks)
            _find_all(buf, base * 8, _EOS_PATTERNS, eos)
            pos += len(data)
            carry = buf[-overlap:]
            if limit_bytes and pos >= limit_bytes:
                break
    blocks = sorted(set(blocks))
    eos = sorted(set(eos))
    return blocks, eos


def _bits(path_bytes: bytes, start_bit: int, length_bits: int, base_bit: int) -> int:
    """从 path_bytes（其第 0 字节对应文件位偏移 base_bit，base_bit 是 8 的倍数）取 [start_bit, start_bit+length) 的位。"""
    off = start_bit - base_bit
    first = off // 8
    last = (off + length_bits + 7) // 8
    seg = path_bytes[first:last]
    n = int.from_bytes(seg, "big")
    total = len(seg) * 8
    drop_left = off - first * 8
    return (n >> (total - drop_left - length_bits)) & ((1 << length_bits) - 1)


def block_stream(data: bytes, base_bit: int, start_bit: int, end_bit: int) -> bytes:
    """把一个块拼成独立 bz2 流。data 是覆盖 [start_bit, end_bit) 的字节（首字节位偏移 base_bit）。"""
    length = end_bit - start_bit
    body = _bits(data, start_bit, length, base_bit)
    crc = (body >> (length - 48 - 32)) & 0xFFFFFFFF          # 魔数之后的 32 位块 CRC
    payload = (body << 80) | (EOS_MAGIC << 32) | crc
    total_bits = length + 80
    pad = (-total_bits) % 8
    payload <<= pad
    return b"BZh9" + payload.to_bytes((total_bits + pad) // 8, "big")


def block_ends(blocks: list[int], eos: list[int], file_bits: int) -> list[int]:
    """第 i 块的结束位 = 之后最近的块魔数或流结束魔数。"""
    import bisect
    ends = []
    for i, s in enumerate(blocks):
        nxt_block = blocks[i + 1] if i + 1 < len(blocks) else file_bits
        j = bisect.bisect_right(eos, s)
        nxt_eos = eos[j] if j < len(eos) else file_bits
        ends.append(min(nxt_block, nxt_eos))
    return ends


def decompress_blocks(path: str | Path, starts: list[int], ends: list[int], *, tolerate: int = 0) -> tuple[bytes, int]:
    """按序解一组块（starts/ends 为位偏移），返回 (拼接后的明文, 失败块数)。

    块魔数在压缩数据里偶然出现（2^-48/位，全文件期望 ~0.003 次）会把一个真块切成两半：
    前半解不开时，把结束位顺延到后面 1~3 个"块"的结束位再试（真块的 CRC 在它自己的头里，仍然正确）。
    仍失败（如文件末尾被截断）就跳过该块并计数；tolerate 是允许跳过的块数上限，超出抛 OSError。"""
    if not starts:
        return b"", 0
    first_byte = starts[0] // 8
    last_byte = (ends[-1] + 7) // 8
    with open(path, "rb") as fh:
        fh.seek(first_byte)
        data = fh.read(last_byte - first_byte)
    base_bit = first_byte * 8
    out = []
    failed = 0
    i = 0
    while i < len(starts):
        s = starts[i]
        done = False
        for extra in range(0, 4):
            j = i + extra
            if j >= len(ends):
                break
            try:
                out.append(bz2.decompress(block_stream(data, base_bit, s, ends[j])))
                i = j + 1
                done = True
                break
            except (OSError, ValueError):
                continue
        if not done:
            failed += 1
            if failed > tolerate:
                raise OSError(f"bz2 block at bit {s} undecodable")
            i += 1
    return b"".join(out), failed


def iter_groups(blocks: list[int], ends: list[int], per_group: int) -> Iterator[tuple[list[int], list[int]]]:
    for i in range(0, len(blocks), per_group):
        yield blocks[i:i + per_group], ends[i:i + per_group]
