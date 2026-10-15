"""
Fetches specification content from a URL instead of requiring a file upload.

SECURITY NOTE: fetching a server-supplied URL from server-side code is a classic SSRF
(server-side request forgery) vector - without protections, someone could point this at
internal network services, cloud metadata endpoints (e.g. 169.254.169.254), or localhost.
This is mitigated by:
- only allowing http/https schemes
- resolving the hostname and rejecting private/loopback/link-local/reserved IP ranges
- a request timeout and a hard cap on response size
This is on top of the fact that spec upload is already restricted to admin/curriculum_manager
roles - defence in depth, not a replacement for that restriction.
"""
import os
import socket
import ipaddress
import uuid
import tempfile
from urllib.parse import urlparse

import requests

from .docx_annotator import extract_text

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024  # 30MB - generous for a spec PDF, not unlimited
REQUEST_TIMEOUT_SECONDS = 30


class UnsafeURLError(Exception):
    pass


def _is_safe_host(hostname: str) -> bool:
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            return False
    return True


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise UnsafeURLError("Couldn't determine the host from that URL.")
    if not _is_safe_host(parsed.hostname):
        raise UnsafeURLError(
            "That URL resolves to a private, internal, or reserved address and can't be fetched "
            "for security reasons. Use a publicly reachable link instead."
        )
    return url


def fetch_spec_from_url(url: str, upload_dir: str) -> tuple[str, str]:
    """
    Downloads the URL, extracts text (PDF/docx/plain text based on content), and returns
    (extracted_text, suggested_label). Raises UnsafeURLError or requests exceptions on failure -
    the caller should catch these and show a clear message rather than a raw 500.
    """
    _validate_url(url)

    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        stream=True,
        headers={"User-Agent": "TIGARSEC-Assess-SpecFetcher/1.0"},
        allow_redirects=True,
    )
    response.raise_for_status()

    # Re-validate after redirects - the final URL could differ from what was requested.
    _validate_url(response.url)

    content_type = response.headers.get("Content-Type", "").lower()

    total = 0
    chunks = []
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise UnsafeURLError(
                f"That file is larger than the {MAX_DOWNLOAD_BYTES // (1024*1024)}MB limit for URL fetching - "
                f"download it and use the file upload option instead."
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    parsed = urlparse(response.url)
    url_filename = os.path.basename(parsed.path) or "downloaded_spec"

    if "pdf" in content_type or url_filename.lower().endswith(".pdf"):
        filename = url_filename if url_filename.lower().endswith(".pdf") else "downloaded_spec.pdf"
    elif "wordprocessingml" in content_type or url_filename.lower().endswith(".docx"):
        filename = url_filename if url_filename.lower().endswith(".docx") else "downloaded_spec.docx"
    elif "text/plain" in content_type or url_filename.lower().endswith(".txt"):
        filename = url_filename if url_filename.lower().endswith(".txt") else "downloaded_spec.txt"
    else:
        # Assume HTML or something extract_text can't handle directly - strip tags ourselves.
        text = _html_to_text(content.decode("utf-8", errors="ignore"))
        label = url_filename or parsed.netloc
        return text, label

    os.makedirs(upload_dir, exist_ok=True)
    temp_path = os.path.join(upload_dir, f"{uuid.uuid4().hex}_{filename}")
    with open(temp_path, "wb") as f:
        f.write(content)

    text = extract_text(temp_path, filename, include_table_copies=False)  # spec text: no duplicate table copies
    label = os.path.splitext(url_filename)[0] or parsed.netloc
    return text, label


def _html_to_text(html: str) -> str:
    """Minimal HTML-to-text conversion using only the standard library, so a spec published
    as a web page (not a PDF/docx) can still be fetched without adding a heavy HTML-parsing
    dependency. Strips scripts/styles/tags, collapses whitespace."""
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip_depth += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self._skip_depth > 0:
                self._skip_depth -= 1
            if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
                self.parts.append("\n")

        def handle_data(self, data):
            if self._skip_depth == 0:
                self.parts.append(data)

    extractor = _TextExtractor()
    extractor.feed(html)
    text = "".join(extractor.parts)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
