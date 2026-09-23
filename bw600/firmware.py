"""Firmware version check against ATORCH's download page.

Only reads the public BW600 page on request; flashing is not implemented.
"""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass

# BW600 and BW600-DK pages; both models report "BW600" over USB and run the same firmware.
PAGES = {
    "BW600": "http://en.atorch.cn/NewsDetail.aspx?ID=92",
    "BW600-DK": "http://en.atorch.cn/NewsDetail.aspx?ID=96",
}
PAGE_URL = PAGES["BW600"]
SITE = "http://en.atorch.cn"


@dataclass
class FirmwareFile:
    name: str            # link text, e.g. "BW600-6321-APP-2-0-5-UP-2026.07.08.zip"
    url: str
    version: tuple[int, ...]
    page: str = "BW600"

    @property
    def customised(self) -> bool:
        """Customer-specific builds carry a (Chinese) note in the file name."""
        return any(ord(c) > 127 for c in self.name)

    @property
    def date(self) -> str:
        """Upload date (YYYYMMDD) from the URL, e.g. .../upload/file/20260708/..."""
        m = re.search(r"/upload/file/(\d{8})/", self.url) or re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", self.name)
        return "".join(m.groups()) if m else ""

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version))


def parse_version(text: str) -> tuple[int, ...] | None:
    """'2.0.5' / 'V2.0.5' / 'APP-2-0-5' -> (2, 0, 5)."""
    m = re.search(r"APP-(\d+)-(\d+)-(\d+)", text) or re.search(r"V?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def parse_page(page: str, source: str = "BW600") -> list[FirmwareFile]:
    files = []
    for m in re.finditer(r'<a[^>]+href="([^"]*upload[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        href, text = m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if "BW600" not in text or "APP" not in text or not text.lower().endswith((".zip", ".bin")):
            continue
        version = parse_version(text)
        if version:
            url = SITE + "/upload/" + href.split("upload/", 1)[1]
            files.append(FirmwareFile(text, url, version, source))
    # newest version first; for equal versions the standard (non-customised), newest build first
    return sorted(files, key=lambda f: (f.version, not f.customised, f.date), reverse=True)


def fetch_available(timeout: float = 30) -> tuple[list[FirmwareFile], list[str]]:
    """Firmware files from both vendor pages, newest first (standard builds before customised),
    and the names of pages that could not be read."""
    files, errors, failed = [], [], []
    for source, url in PAGES.items():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 bw600-tool"})
        for attempt in range(2):  # the vendor site is slow and occasionally resets connections
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    files += parse_page(r.read().decode("utf-8", "replace"), source)
                break
            except OSError as e:
                errors.append(e)
        else:
            failed.append(source)
    if not files and errors:
        raise errors[0]
    return sorted(files, key=lambda f: (f.version, not f.customised, f.date), reverse=True), failed


def download(f: FirmwareFile, dest: str, timeout: float = 120) -> None:
    req = urllib.request.Request(f.url, headers={"User-Agent": "Mozilla/5.0 bw600-tool"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
        while chunk := r.read(65536):
            out.write(chunk)
