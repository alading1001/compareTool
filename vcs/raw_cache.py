"""Task-owned raw files with stream-time content and file-identity verification."""
from dataclasses import dataclass
import hashlib
import os
import uuid
from path_safety import (ensure_no_link_components, open_new_tree_file,
                         open_regular_file_no_links, regular_file_handle_identity,
                         regular_file_path_identity)
from .temp_storage import create_temp_dir, remove_temp_dir

@dataclass(frozen=True)
class RawEntry:
    path: str
    identity: tuple
    size: int
    digest: str

class _DigestWriter:
    def __init__(self, stream):
        self.stream=stream;self.digest=hashlib.sha256();self.size=0
    def write(self, block):
        written=self.stream.write(block)
        if written!=len(block):raise OSError("原始缓存写入不完整")
        self.digest.update(block);self.size+=written
        return written

class RawEndpointCache:
    """Only successful downloads are published. No cross-task/global cache."""
    def __init__(self):
        self.root=create_temp_dir(prefix='comparetool_svn_raw_')
        self.entries={}

    def get(self, key):
        return self.entries.get(key)

    def acquire(self, key, producer):
        if key in self.entries:return self.entries[key]
        if not self.root:raise RuntimeError("原始缓存已经关闭")
        path=os.path.join(self.root,uuid.uuid4().hex+'.raw')
        ensure_no_link_components(self.root,path,'原始缓存')
        path,stream=open_new_tree_file(self.root,path,label='原始缓存')
        try:
            with stream:
                initial_id=regular_file_handle_identity(stream)[0]
                writer=_DigestWriter(stream)
                producer(writer)
                stream.flush()
                completed=regular_file_handle_identity(stream)
                if completed[0]!=initial_id or completed[1]!=writer.size:
                    raise RuntimeError("原始缓存生成期间身份或长度变化")
            identity=regular_file_path_identity(path)
            if identity!=completed:raise RuntimeError("原始缓存发布前被替换")
            entry=RawEntry(path,identity,writer.size,writer.digest.hexdigest())
            self.entries[key]=entry
            return entry
        except BaseException:
            # The containing owned tree is cleaned by this task, not by the
            # report/exporter. A failed partial file is never a cache hit.
            raise

    def iter_bytes(self, entry):
        ensure_no_link_components(self.root,entry.path,'原始缓存')
        digest=hashlib.sha256();size=0
        with open_regular_file_no_links(entry.path,deny_writes=True) as source:
            if regular_file_handle_identity(source)!=entry.identity:
                raise RuntimeError("原始缓存文件身份已变化")
            for block in iter(lambda:source.read(1024*1024),b''):
                digest.update(block);size+=len(block)
                yield block
            if (regular_file_handle_identity(source)!=entry.identity
                    or regular_file_path_identity(entry.path)!=entry.identity
                    or size!=entry.size or digest.hexdigest()!=entry.digest):
                raise RuntimeError("原始缓存内容已变化，已中止生成")

    def read_bytes(self, entry):
        return b''.join(self.iter_bytes(entry))

    def copy_to(self, entry, target_path):
        if os.path.exists(target_path) and os.path.samefile(entry.path,target_path):
            raise RuntimeError("交付文件不得覆盖原始缓存")
        with open(target_path,'wb') as target:
            for block in self.iter_bytes(entry):
                if target.write(block)!=len(block):raise OSError("交付复制不完整")

    def signature(self, entry):
        for _ in self.iter_bytes(entry):pass
        return entry.size,entry.digest

    def close(self):
        if self.root:
            remove_temp_dir(self.root)
            self.root=''
        self.entries.clear()
