"""Binary file extensions to skip for text-based operations.

These files can't be meaningfully compared as text and are often large.
Ported from free-code src/constants/files.ts.
"""
BINARY_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff', '.tif', '.mp4', '.mov', '.avi', '.mkv', '.webm', '.wmv', '.flv', '.m4v', '.mpeg', '.mpg', '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma', '.aiff', '.opus', '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar', '.xz', '.z', '.tgz', '.iso', '.exe', '.dll', '.so', '.dylib', '.bin', '.o', '.a', '.obj', '.lib', '.app', '.msi', '.deb', '.rpm', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.ttf', '.otf', '.woff', '.woff2', '.eot', '.pyc', '.pyo', '.class', '.jar', '.war', '.ear', '.node', '.wasm', '.rlib', '.sqlite', '.sqlite3', '.db', '.mdb', '.idx', '.psd', '.ai', '.eps', '.sketch', '.fig', '.xd', '.blend', '.3ds', '.max', '.swf', '.fla', '.lockb', '.dat', '.data'})

def has_binary_extension(path: str) -> bool:
    """Check if a file path has a binary extension. Pure string check, no I/O."""
    dot = path.rfind('.')
    if dot == -1:
        return False
    return path[dot:].lower() in BINARY_EXTENSIONS