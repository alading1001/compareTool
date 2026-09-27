"""Task-owned HTML fragments: one private file, bounded and repeatable reads."""
import codecs
from dataclasses import dataclass, field
import hashlib
import os
import tempfile
from typing import Iterator

from vcs.temp_storage import create_temp_dir, remove_temp_dir


@dataclass(frozen=True)
class HtmlFragment:
    store: 'HtmlDetailStore' = field(repr=False, compare=False)
    offset: int
    length: int
    sha256: str

    def iter_text(self) -> Iterator[str]:
        return self.store.iter_fragment(self)


class HtmlDetailStore:
    """Single-worker resource, separate from source/export transaction stages."""
    CHUNK_CHARS = 16384
    CHUNK_BYTES = 65536

    def __init__(self, *, avoid_paths=()):
        self.avoid_paths = tuple(avoid_paths)
        self.root = ''
        self._file = None
        self._closed = self._failed = False
        self.total_bytes = self.fragments = self.read_bytes = 0

    def _check_open(self):
        if self._closed or self._failed:
            raise RuntimeError('HTML 明细存储已关闭或失效')

    def _ensure_file(self):
        self._check_open()
        if self._file is None:
            self.root = create_temp_dir('comparetool_html_', avoid_paths=self.avoid_paths)
            self._file = tempfile.TemporaryFile(mode='w+b', dir=self.root)

    def add_fragment(self, text: str) -> HtmlFragment:
        if not isinstance(text, str):
            raise TypeError('HTML 明细必须是已渲染的字符串')
        return self.add_chunks(text[i:i + self.CHUNK_CHARS]
                               for i in range(0, len(text), self.CHUNK_CHARS))

    def add_chunks(self, chunks) -> HtmlFragment:
        self._ensure_file()
        offset = self.total_bytes
        length = 0
        digest = hashlib.sha256()
        try:
            self._file.seek(offset)
            for text in chunks:
                if not isinstance(text, str):
                    raise TypeError('HTML 片段流必须产生字符串')
                for i in range(0, len(text), self.CHUNK_CHARS):
                    block = text[i:i + self.CHUNK_CHARS].encode('utf-8')
                    if self._file.write(block) != len(block):
                        raise OSError('HTML 明细写入不完整')
                    digest.update(block)
                    length += len(block)
            self._file.flush()
            if os.fstat(self._file.fileno()).st_size != offset + length:
                raise OSError('HTML 明细存储长度异常')
        except BaseException:
            self._failed = True
            raise
        self.total_bytes += length
        self.fragments += 1
        return HtmlFragment(self, offset, length, digest.hexdigest())

    def iter_fragment(self, ref: HtmlFragment) -> Iterator[str]:
        self._check_open()
        if (ref.store is not self or ref.offset < 0 or ref.length < 0
                or ref.offset + ref.length > self.total_bytes):
            raise ValueError('HTML 片段引用不属于本任务或超出范围')
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        position, remaining = ref.offset, ref.length
        try:
            if os.fstat(self._file.fileno()).st_size != self.total_bytes:
                raise OSError('HTML 明细存储被截断或改变')
            while remaining:
                self._check_open()
                self._file.seek(position)
                block = self._file.read(min(remaining, self.CHUNK_BYTES))
                if not block:
                    raise OSError('HTML 片段读取提前结束')
                position += len(block)
                remaining -= len(block)
                self.read_bytes += len(block)
                digest.update(block)
                decoded = decoder.decode(block)
                if decoded:
                    yield decoded
            tail = decoder.decode(b'', final=True)
            if tail:
                yield tail
            if (digest.hexdigest() != ref.sha256
                    or os.fstat(self._file.fileno()).st_size != self.total_bytes):
                raise OSError('HTML 片段内容校验失败')
        except Exception:
            self._failed = True
            raise

    def close(self):
        self._closed = True
        try:
            if self._file is not None:
                self._file.close()
                self._file = None
        finally:
            if self.root:
                remove_temp_dir(self.root)
                self.root = ''

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_exc):
        self.close()
