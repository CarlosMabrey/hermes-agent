"""Object-bound Windows deletion; unsupported deletion fails closed."""
import contextlib
import ctypes
from ctypes import wintypes
import os
import threading
from pathlib import Path

if os.name == "nt":
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k.CreateFileW.restype = wintypes.HANDLE
    _k.ReOpenFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    _k.ReOpenFile.restype = wintypes.HANDLE
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k.CloseHandle.restype = wintypes.BOOL
    _k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _k.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k.SetFileInformationByHandle.restype = wintypes.BOOL


def _checked(handle):
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


class OwnedFile:
    def __init__(self, stream):
        self._lock = threading.RLock()
        self.handle = None
        if os.name == "nt":
            import msvcrt
            # No DELETE access while readers use the pathname: CRT readers do not
            # share DELETE. ReOpenFile later upgrades THIS object, never its name.
            self.handle = _checked(_k.ReOpenFile(msvcrt.get_osfhandle(stream.fileno()), 0x80000000, 7, 0))

    def cleanup(self):
        with self._lock:
            if self.handle is None:
                return  # POSIX has no portable unlink-by-open-file capability.
            try:
                deletion = _checked(_k.ReOpenFile(self.handle, 0x10000, 7, 0))
                try:
                    pending = wintypes.BOOL(True)
                    if not _k.SetFileInformationByHandle(deletion, 4, ctypes.byref(pending), ctypes.sizeof(pending)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    _k.CloseHandle(deletion)
            except OSError:
                pass  # Fail closed: never fall back to pathname unlink.
            finally:
                self.close()

    def close(self):
        with self._lock:
            if self.handle is not None:
                _k.CloseHandle(self.handle)
                self.handle = None

    def __del__(self):
        self.close()


@contextlib.contextmanager
def protected_directory(root):
    """Pin each Windows ancestor against rename/reparse replacement during writes."""
    root = Path(root).absolute()
    handles = []
    try:
        if os.name == "nt":
            for part in reversed([root, *root.parents]):
                try:
                    handle = _checked(_k.CreateFileW(str(part), 0x80000000, 1, None, 3, 0x02200000, None))
                except OSError as exc:
                    if exc.winerror not in (2, 3):
                        raise
                    part.mkdir()  # Its parent is already pinned.
                    handle = _checked(_k.CreateFileW(str(part), 0x80000000, 1, None, 3, 0x02200000, None))
                handles.append(handle)
                # FILE_ATTRIBUTE_TAG_INFO reads the OPENED directory itself.
                info = (wintypes.DWORD * 2)()
                if not _k.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info[0] & 0x400 or not info[0] & 0x10:
                    raise ValueError("attachment directory is a link or reparse point")
            yield None
        else:
            fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            handles.append(fd)
            for name in root.parts[1:]:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            yield fd
    finally:
        for handle in reversed(handles):
            if os.name == "nt":
                _k.CloseHandle(handle)
            else:
                os.close(handle)


def create_file(path, directory_fd):
    if directory_fd is None:
        return path.open("xb")
    fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    return os.fdopen(fd, "wb")
