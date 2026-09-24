"""Real ZIP bytes for native archive compatibility and corrupt-input checks."""
import pathlib
import stat
import struct
import sys
import zipfile

root = pathlib.Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
chapter = '<p>①エイズ <ruby>結核<rt>けっかく</rt></ruby> &amp; SARS</p>'.encode()
(root / 'chapter.bin').write_bytes(chapter)
with zipfile.ZipFile(root / 'valid.epub', 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('META-INF/container.xml', '<container/>')
    archive.writestr('OPS/章一.xhtml', chapter)
    archive.writestr('OPS/é.txt', 'composed')
    archive.writestr('OPS/e\u0301.txt', 'decomposed')
    archive.writestr('OPS/large.bin', b'x' * (8 * 1024 * 1024 + 1), compress_type=zipfile.ZIP_STORED)
for filename, entries in [
    ('duplicate', [('OPS/x', 'a'), ('OPS/./x', 'b')]),
    ('traversal', [('../secret', 'x')]),
    ('bomb', [('x', 'a' * (2 * 1024 * 1024))]),
]:
    with zipfile.ZipFile(root / (filename + '.epub'), 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
with zipfile.ZipFile(root / 'symlink.epub', 'w') as archive:
    link = zipfile.ZipInfo('OPS/link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive.writestr(link, '../outside')
with zipfile.ZipFile(root / 'corrupt.epub', 'w') as archive:
    archive.writestr('chapter', chapter)
corrupt = bytearray((root / 'corrupt.epub').read_bytes())
offset = 30 + struct.unpack_from('<H', corrupt, 26)[0] + struct.unpack_from('<H', corrupt, 28)[0]
corrupt[offset] ^= 1
(root / 'corrupt.epub').write_bytes(corrupt)
