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
container = '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>'
opf = '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title> Reader &amp; Test </dc:title></metadata><manifest>
<item id="first" href="章一.xhtml" media-type="application/xhtml+xml"/>
<item id="second" href="two.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
</manifest><spine toc="ncx"><itemref idref="second"/><itemref idref="first"/></spine></package>'''
nav = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="landmarks"><a href="two.xhtml">Wrong</a></nav>
<nav epub:type="toc"><a href="%E7%AB%A0%E4%B8%80.xhtml#x"> 第一   章 </a><a href="%E7%AB%A0%E4%B8%80.xhtml#y">第一 章</a><a href="two.xhtml">Second</a><a href="https://example.com/">External</a></nav>
</body></html>'''
with zipfile.ZipFile(root / 'valid.epub', 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('META-INF/container.xml', container)
    archive.writestr('OPS/package.opf', opf)
    archive.writestr('OPS/nav.xhtml', nav)
    archive.writestr('OPS/章一.xhtml', chapter)
    archive.writestr('OPS/two.xhtml', chapter)
    archive.writestr('OPS/é.txt', 'composed')
    archive.writestr('OPS/e\u0301.txt', 'decomposed')
    archive.writestr('OPS/large.bin', b'x' * (8 * 1024 * 1024 + 1), compress_type=zipfile.ZIP_STORED)
for name, navigation in [('ncx', '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint><navLabel><text>NCX label</text></navLabel><content src="two.xhtml#x"/></navPoint></navMap></ncx>'), ('fallback', '')]:
    with zipfile.ZipFile(root / (name + '.epub'), 'w') as archive:
        archive.writestr('META-INF/container.xml', container)
        archive.writestr('OPS/package.opf', opf)
        archive.writestr('OPS/章一.xhtml', chapter)
        archive.writestr('OPS/two.xhtml', chapter)
        archive.writestr('OPS/nav.xhtml', '<broken>')
        archive.writestr('OPS/toc.ncx', navigation)
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
