"""Firmware version check against ATORCH's download page.

Only reads the public BW600 page on request; flashing is not implemented.
"""

from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass

PAGE_URL = "http://en.atorch.cn/NewsDetail.aspx?ID=92"
SITE = "http://en.atorch.cn"


@dataclass
class FirmwareFile:
    name: str            # link text, e.g. "BW600-6321-APP-2-0-5-UP-2026.07.08.zip"
    url: str
    version: tuple[int, ...]

    @property
    def customised(self) -> bool:
        """Customer-specific builds carry a (Chinese) note in the file name."""
        return any(ord(c) > 127 for c in self.name)

    @property
    def date(self) -> str:
        m = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", self.name)
        return "".join(m.groups()) if m else ""

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version))


def parse_version(text: str) -> tuple[int, ...] | None:
    """'2.0.5' / 'V2.0.5' / 'APP-2-0-5' -> (2, 0, 5)."""
    m = re.search(r"APP-(\d+)-(\d+)-(\d+)", text) or re.search(r"V?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def parse_page(page: str) -> list[FirmwareFile]:
    files = []
    for m in re.finditer(r'<a[^>]+href="([^"]*upload[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        href, text = m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if "BW600" not in text or "APP" not in text or not text.lower().endswith((".zip", ".bin")):
            continue
        version = parse_version(text)
        if version:
            url = SITE + "/upload/" + href.split("upload/", 1)[1]
            files.append(FirmwareFile(text, url, version))
    # newest version first; for equal versions the standard (non-customised), newest build first
    return sorted(files, key=lambda f: (f.version, not f.customised, f.date), reverse=True)


def fetch_available(timeout: float = 20) -> list[FirmwareFile]:
    req = urllib.request.Request(PAGE_URL, headers={"User-Agent": "Mozilla/5.0 bw600-tool"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_page(r.read().decode("utf-8", "replace"))


def download(f: FirmwareFile, dest: str, timeout: float = 120) -> None:
    req = urllib.request.Request(f.url, headers={"User-Agent": "Mozilla/5.0 bw600-tool"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
        while chunk := r.read(65536):
            out.write(chunk)
