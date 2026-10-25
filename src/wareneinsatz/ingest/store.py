"""Content-addressed file store: every original file is kept under the SHA-256 of its content.

    data/documents/3f/3fa9...c1.xml

The same file always gets the same path, so storing it twice is harmless, a stored file
can never be silently replaced by different content, and the database only needs the
relative path. The two-character subfolder keeps any one folder from growing too large.
"""

import os
import tempfile
from pathlib import Path


class DocumentStore:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def relative_path(sha256: str, suffix: str) -> str:
        """Path inside the store, e.g. '3f/3fa9...c1.xml'. Saved in documents.storage_path."""
        return f"{sha256[:2]}/{sha256}{suffix}"

    def save(self, content: bytes, sha256: str, suffix: str) -> str:
        """Write the file if it isn't stored yet and return its relative path."""
        relative = self.relative_path(sha256, suffix)
        target = self.root / relative
        if target.exists():
            return relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file first and rename it: a crash can never leave half a file
        # under the final name, because the rename is atomic.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, target)
        return relative

    def open(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()
