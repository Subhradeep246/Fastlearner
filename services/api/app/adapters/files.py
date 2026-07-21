"""File-screening adapters implementing the :class:`FileScanner` port.

Uploads are untrusted content. These adapters screen bytes for known-unsafe
signatures without executing, rendering, or interpreting the file, satisfying
the untrusted-content boundary in Requirement 19.13. A cleaner runtime scanner
(for example an antivirus daemon) can implement the same port later without
changing the memory service.
"""

from __future__ import annotations

from app.domain.memory import FileUpload, FileScanner, ScanResult

# The industry-standard EICAR antivirus test string. Detecting it lets tests and
# operators verify the screening path without shipping a real malware sample.
_EICAR = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)

# Byte signatures that indicate an executable or script masquerading as a
# document. These are rejected outright before any ingestion occurs.
_UNSAFE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "windows_executable"),
    (b"\x7fELF", "elf_executable"),
    (b"#!", "shell_script"),
    (b"<?php", "php_script"),
    (b"<script", "embedded_script"),
)


class SignatureFileScanner(FileScanner):
    """A deterministic, dependency-free signature scanner for local operation."""

    def scan(self, upload: FileUpload) -> ScanResult:
        content = upload.content or b""
        if _EICAR in content:
            return ScanResult(clean=False, detail="eicar_test_signature")
        head = content[:16]
        lowered = head.lower()
        for signature, label in _UNSAFE_SIGNATURES:
            if head.startswith(signature) or lowered.startswith(signature.lower()):
                return ScanResult(clean=False, detail=label)
        return ScanResult(clean=True)


class AllowCleanFileScanner(FileScanner):
    """A permissive scanner that treats every upload as clean.

    Intended only for tests or environments where an external scanner is not
    configured. Production composition should prefer a real scanning adapter.
    """

    def scan(self, upload: FileUpload) -> ScanResult:  # noqa: ARG002 - always clean
        return ScanResult(clean=True)
