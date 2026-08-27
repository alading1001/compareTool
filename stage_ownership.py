import os


OWNERSHIP_SUFFIX = ".comparetool_owned"
OWNERSHIP_MAGIC = "CompareTool owned staging item v1\n"


def ownership_marker(path: str) -> str:
    return os.path.abspath(path) + OWNERSHIP_SUFFIX


def mark_owned(path: str):
    marker = ownership_marker(path)
    with open(marker, "x", encoding="ascii") as stream:
        stream.write(OWNERSHIP_MAGIC)
        stream.flush()
        os.fsync(stream.fileno())
    return marker


def is_owned(path: str) -> bool:
    marker = ownership_marker(path)
    try:
        with open(marker, "r", encoding="ascii") as stream:
            return stream.read(len(OWNERSHIP_MAGIC) + 1) == OWNERSHIP_MAGIC
    except (OSError, UnicodeError):
        return False


def remove_ownership_marker(path: str):
    try:
        os.remove(ownership_marker(path))
    except FileNotFoundError:
        pass
    except OSError:
        pass
