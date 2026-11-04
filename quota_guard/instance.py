"""Per-folder kernel lock; stale lock files do not prevent crash recovery."""
from pathlib import Path
import msvcrt
from .core import GuardError

class FolderLock:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.file = (Path(folder) / '.运行锁').open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise GuardError('此数据文件夹已由另一个程序实例打开。') from None
    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
