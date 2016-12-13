"""Module for working with id Software style PAK files

Supported Games:
    - QUAKE
"""

import io
import os
import shutil
import struct

try:
    import threading
except ImportError:
    import dummy_threading as threading


__all__ = ['BadPakFile', 'is_pakfile', 'PakInfo', 'PakFile']


class BadPakFile(Exception):
    pass


# The main PAK file structure
header_struct = '<4s2i'
header_magic_number = b'PACK'
header_size = struct.calcsize(header_struct)

# Indexes of entries in the header structure
_HEADER_SIGNATURE = 0
_HEADER_DIRECTORY_OFFSET = 1
_HEADER_DIRECTORY_SIZE = 2

# The local file structure
local_file_struct = '<56s2i'
local_file_size = struct.calcsize(local_file_struct)

# Indexes for the local file structure
_FILE_NAME = 0
_FILE_OFFSET = 1
_FILE_SIZE = 2


def _check_pakfile(fp):
    fp.seek(0)
    data = fp.read(header_size)

    return data == header_magic_number


def is_pakfile(filename):
    """Quickly see if a file is a pak file by checking the magic number.

    The filename argument may be a file for file-like object.
    """
    result = False

    try:
        if hasattr(filename, 'read'):
            return _check_pakfile(fp=filename)
        else:
            with open(filename, 'rb') as fp:
                return _check_pakfile(fp)

    except:
        pass

    return result


class PakInfo(object):
    """Class with attributes describing each entry in the pak file archive."""

    __slots__ = (
        'filename',
        'file_offset',
        'file_size'
    )

    def __init__(self, filename, file_offset, file_size):
        self.filename = filename
        self.file_offset = file_offset
        self.file_size = file_size


class _SharedFile:
    def __init__(self, file, position, size, close, lock):
        self._file = file
        self._position = position
        self._start = position
        self._end = position + size
        self._close = close
        self._lock = lock

    def read(self, n=-1):
        with self._lock:
            self._file.seek(self._position)

            if n < 0 or n > self._end:
                n = self._end - self._position

            data = self._file.read(n)
            self._position = self._file.tell()
            return data

    def close(self):
        if self._file is not None:
            file_object = self._file
            self._file = None
            self._close(file_object)

    def seek(self, n):
        n = min(self._start + n, self._end)
        self._file.seek(n)
        self._position = self._file.tell()


class PakExtFile(io.BufferedIOBase):
    """A file-like object for reading an entry.

    It is returned by PakFile.open()
    """

    MAX_N = 1 << 31 - 1
    MIN_READ_SIZE = 4096

    def __init__(self, file_object, mode, pak_info, close_file_object=False):
        self._file_object = file_object
        self._close_file_object = close_file_object
        self._bytes_left = pak_info.file_size

        self._eof = False
        self._readbuffer = b''
        self._offset = 0
        self._size = pak_info.file_size

        self.mode = mode
        self.name = pak_info.filename

    def read(self, n=-1):
        """Read and return up to n bytes.

        If the argument n is omitted, None, or negative, data will be read
        until EOF.
        """

        if n is None or n < 0:
            buffer = self._readbuffer[self._offset:]
            self._readbuffer = b''
            self._offset = 0

            while not self._eof:
                buffer += self._read_internal(self.MAX_N)

            return buffer

        end = n + self._offset

        if end < len(self._readbuffer):
            buffer = self._readbuffer[self._offset:end]
            self._offset = end

            return buffer

        n = end - len(self._readbuffer)
        buffer = self._readbuffer[self._offset:]
        self._readbuffer = b''
        self._offset = 0

        while n > 0 and not self._eof:
            data = self._read_internal(n)

            if n < len(data):
                self._readbuffer = data
                self._offset = n
                buffer += data[:n]
                break

            buffer += data
            n -= len(data)

        return buffer

    def _read_internal(self, n):
        """Read up to n bytes with at most one read() system call"""

        if self._eof or n <= 0:
            return b''

        # Read from file.
        n = max(n, self.MIN_READ_SIZE)
        data = self._file_object.read(n)

        if not data:
            raise EOFError

        data = data[:self._bytes_left]
        self._bytes_left -= len(data)

        if self._bytes_left <= 0:
            self._eof = True

        return data

    def peek(self, n=1):
        """Returns buffered bytes without advancing the position."""

        if n > len(self._readbuffer) - self._offset:
            chunk = self.read(n)
            if len(chunk) > self._offset:
                self._readbuffer = chunk + self._readbuffer[self._offset:]
                self._offset = 0
            else:
                self._offset -= len(chunk)

        # Return up to 512 bytes to reduce allocation overhead for tight loops.
        return self._readbuffer[self._offset: self._offset + 512]

    def seek(self, n):
        self._file_object.seek(n)
        self._readbuffer = b''
        self._offset = 0

    def close(self):
        try:
            if self._close_file_object:
                self._file_object.close()
        finally:
            super().close()


class PakFile(object):
    """Class with methods to open, read, close, and list pak files.

     p = PakFile(file, mode='r')

    file: Either the path to the file, or a file-like object. If it is a path,
        the file will be opened and closed by PakFile.

    mode: Currently the only supported mode is 'r'
    """

    fp = None
    _windows_illegal_name_trans_table = None

    def __init__(self, file, mode='r'):
        if mode not in ('r',):
            raise RuntimeError("PakFile requires mode 'r'")

        self.NameToInfo = {}
        self.file_list = []
        self.mode = mode

        self._file_reference_count = 1
        self._lock = threading.RLock()

        if isinstance(file, str):
            self.filename = file
            self.fp = io.open(file, 'rb')
            self._file_passed = 0
        else:
            self.fp = file
            self.filename = getattr(file, 'name', None)
            self._file_passed = 1

        self._load_archive_content()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def _load_archive_content(self):
        """Read in the directory information for the pak file."""

        fp = self.fp
        _windows_illegal_name_trans_table = None

        fp.seek(0)
        header = fp.read(header_size)
        header = struct.unpack(header_struct, header)

        if header[_HEADER_SIGNATURE] != header_magic_number:
            raise BadPakFile('Bad magic number: %r' % header[_HEADER_SIGNATURE])

        start_of_directory = header[_HEADER_DIRECTORY_OFFSET]
        size_of_directory = header[_HEADER_DIRECTORY_SIZE]

        fp.seek(start_of_directory)
        data = fp.read(size_of_directory)
        fp = io.BytesIO(data)

        total_bytes_read = 0

        while total_bytes_read < size_of_directory:
            local_file = fp.read(local_file_size)
            local_file = struct.unpack(local_file_struct, local_file)

            filename = local_file[_FILE_NAME].split(b'\00')[0].decode('ascii')
            file_offset = local_file[_FILE_OFFSET]
            file_size = local_file[_FILE_SIZE]

            info = PakInfo(filename, file_offset, file_size)

            self.file_list.append(info)
            self.NameToInfo[info.filename] = info

            total_bytes_read += local_file_size

    def namelist(self):
        """Return a list of file names in the pak file."""

        return [data.filename for data in self.file_list]

    def infolist(self):
        """Return a list of PakInfo instances for all of the files in the
        pak file."""

        return self.file_list

    def getinfo(self, name):
        """Return an instance of PakInfo given 'name'."""

        info = self.NameToInfo.get(name)

        if info is None:
            raise KeyError('There is no item named %r in the pak file' % name)

        return info

    def read(self, name):
        """Return file bytes (as a string) for 'name'."""

        with self.open(name, 'r') as fp:
            return fp.read()

    def open(self, name, mode='r'):
        """Return a file-like object for 'name'."""

        if not self.fp:
            raise RuntimeError('Attempt to read PAK archive that was already closed')

        if isinstance(name, PakInfo):
            info = name
        else:
            info = self.getinfo(name)

        self._file_reference_count += 1
        shared_file = _SharedFile(self.fp, info.file_offset, info.file_size, self._fpclose, self._lock)
        try:
            return PakExtFile(shared_file, mode, info, True)
        except:
            shared_file.close()
            raise

    def extract(self, member, path=None):
        """Extract a member from the pak file to the current working directory
        using its full name. Note: pak files do not store file metadata.

        member: Either the name of the member to extract or a PakInfo instance.

        path: The directory to extract to. The current working directory will
        be used if None.
        """

        if not isinstance(member, PakInfo):
            member = self.getinfo(member)

        if path is None:
            path = os.getcwd()

        return self._extract_member(member, path)

    def extractall(self, path=None, members=None):
        """Extract all members from the pak file to the current working
        directory.

        path: The directory to extract to. The current working directory will
            be used if None.

        members: The names of the members to extract. This must be a subset of
            the list returned by namelist(). All members will be extracted if
            None.
        """

        if members is None:
            members = self.namelist()

        for pakinfo in members:
            self.extract(pakinfo, path)

    @classmethod
    def _sanitize_windows_name(cls, archive_name, path_separator):
        """Replace bad characters and remove trailing dots from parts."""

        table = cls._windows_illegal_name_trans_table

        if not table:
            illegal = ':<>|"?*'
            table = str.maketrans(illegal, '_' * len(illegal))
            cls._windows_illegal_name_trans_table = table

        archive_name = archive_name.translate(table)

        # Remove trailing dots
        archive_name = (x.rstrip('.') for x in archive_name.split(path_separator))

        # Rejoin, removing empty parts.
        archive_name = path_separator.join(x for x in archive_name if x)

        return archive_name

    def _extract_member(self, member, target_path):
        """Extract the PakInfo object 'member' to a physical file on the path
        target_path.
        """

        # Build the destination pathname, replacing forward slashes to
        # platform specific separators.
        archive_name = member.filename.replace('/', os.path.sep)

        if os.path.altsep:
            archive_name = archive_name.replace(os.path.altsep, os.path.sep)

        # Interpret absolute pathname as relative, remove drive letter or
        # UNC path, redundant separators, "." and ".." components.
        archive_name = os.path.splitdrive(archive_name)[1]
        invalid_path_parts = ('', os.path.curdir, os.path.pardir)
        archive_name = os.path.sep.join(x for x in archive_name.split(os.path.sep)
                                   if x not in invalid_path_parts)
        if os.path.sep == '\\':
            # Filter illegal characters on Windows
            archive_name = self._sanitize_windows_name(archive_name, os.path.sep)

        target_path = os.path.join(target_path, archive_name)
        target_path = os.path.normpath(target_path)

        # Create all upper directories if necessary.
        upperdirs = os.path.dirname(target_path)
        if upperdirs and not os.path.exists(upperdirs):
            os.makedirs(upperdirs)

        if member.filename[-1] == '/':
            if not os.path.isdir(target_path):
                os.mkdir(target_path)
            return target_path

        with self.open(member) as source, open(target_path, "wb") as target:
            shutil.copyfileobj(source, target)

        return target_path

    def close(self):
        """Close the file."""

        if self.fp is None:
            return

        fp = self.fp
        self.fp = None
        fp.close()

    def _fpclose(self, fp):
        assert self._file_reference_count > 0
        self._file_reference_count -= 1

        if not self._file_reference_count and not self._file_passed:
            fp.close()