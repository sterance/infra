"""chocolatey: the newest stable version of a Chocolatey package."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from .. import UpdateError, versions
from . import Latest

API = "https://community.chocolatey.org/api/v2"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
}
MAX_PAGES = 20


def feed_url(package: str) -> str:
    return f"{API}/FindPackagesById()?id='{urllib.parse.quote(package)}'"


def label(check: dict) -> str:
    return check["package"]


def validate_current(current: str) -> None:
    versions.numeric(current)


def parse_feed(xml: bytes) -> tuple[list[tuple[str, bool]], str | None]:
    """(version, is prerelease) per entry, and the next page's URL."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise UpdateError(f"the Chocolatey feed isn't XML: {e}") from e
    out = []
    for entry in root.findall("a:entry", NS):
        props = entry.find("m:properties", NS)
        if props is None:
            continue
        version = props.findtext("d:Version", default="", namespaces=NS)
        pre = props.findtext("d:IsPrerelease", default="false", namespaces=NS) == "true"
        out.append((version, pre))
    nxt = root.find("a:link[@rel='next']", NS)
    return out, None if nxt is None else nxt.get("href")


def latest(check: dict, current: str, http) -> Latest:
    package = check["package"]
    url: str | None = feed_url(package)
    found = []
    for _ in range(MAX_PAGES):
        if url is None:
            break
        entries, url = parse_feed(http.get(url, "application/atom+xml"))
        found.extend(v for v, pre in entries if not pre and versions.is_numeric(v))
    else:
        raise UpdateError(f"the Chocolatey feed for {package} has more than {MAX_PAGES} pages")
    if not found:
        raise UpdateError(f"Chocolatey lists no stable {package} release ({feed_url(package)})")
    v = versions.highest(found)
    return Latest(
        version=v,
        source=f"the Chocolatey `{package}` package",
        release_url=f"https://community.chocolatey.org/packages/{package}/{v}",
    )


def newer(check: dict, new: Latest, current: str, http) -> bool:
    return versions.newer(new.version, current)
