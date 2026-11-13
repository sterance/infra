"""Fetching, with retries, and container registry lookups.

Every request carries the same generic User-Agent: no project or personal
details go to outside services. The GitHub API gets the workflow's
read-only GITHUB_TOKEN (when set) only for its rate limit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import UpdateError

USER_AGENT = "dependency-update-check (+https://docs.github.com/actions)"
TIMEOUT = 60
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class NotFound(UpdateError):
    def __init__(self, url: str):
        super().__init__(f"{url}: not found (404)")
        self.url = url


class Http:
    """The real network. Tests replace it with a fake that has the same
    methods."""

    def __init__(self, tries: int = 3, sleep=time.sleep):
        self.tries = tries
        self.sleep = sleep

    # ---- plain HTTP

    def request(self, url: str, accept: str | None = None, method: str = "GET", headers: dict | None = None):
        """(status, headers, body). 404 raises NotFound; 401 is returned (the
        registry auth flow needs it); other failures retry, then raise."""
        h = {"User-Agent": USER_AGENT}
        if accept:
            h["Accept"] = accept
        token = os.environ.get("GITHUB_TOKEN")
        if token and url.startswith("https://api.github.com/"):
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        last: Exception | None = None
        for attempt in range(self.tries):
            try:
                req = urllib.request.Request(url, headers=h, method=method)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(url) from e
                if e.code == 401:
                    return 401, dict(e.headers), b""
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            if attempt + 1 < self.tries:
                self.sleep(2 * (attempt + 1))
        raise UpdateError(f"couldn't fetch {url}: {last}")

    def get(self, url: str, accept: str | None = None) -> bytes:
        status, _, body = self.request(url, accept)
        if status == 401:
            raise UpdateError(f"{url}: 401 unauthorized")
        return body

    def json(self, url: str, accept: str | None = None):
        try:
            return json.loads(self.get(url, accept))
        except json.JSONDecodeError as e:
            raise UpdateError(f"{url} didn't return JSON: {e}") from e

    def text(self, url: str, accept: str | None = None) -> str:
        return self.get(url, accept).decode("utf-8")

    def sha256(self, url: str) -> tuple[str, int]:
        """Downloads `url`, returning (sha256 hex, size in bytes)."""
        h, size = hashlib.sha256(), 0
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                while chunk := r.read(1 << 20):
                    h.update(chunk)
                    size += len(chunk)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise UpdateError(f"couldn't download {url}: {e}") from e
        return h.hexdigest(), size

    # ---- container registries (Docker Hub, GHCR, Quay, … through the v2 API)

    def _registry(self, url: str, scope: str, accept: str | None, method: str = "GET"):
        status, headers, body = self.request(url, accept, method)
        if status != 401:
            return headers, body
        challenge = next((v for k, v in headers.items() if k.lower() == "www-authenticate"), "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer") or "realm" not in params:
            raise UpdateError(f"{url}: 401 with no bearer challenge")
        q = {"scope": scope}
        if "service" in params:
            q["service"] = params["service"]
        tok = json.loads(self.get(params["realm"] + "?" + urllib.parse.urlencode(q)))
        bearer = tok.get("token") or tok.get("access_token")
        if not bearer:
            raise UpdateError(f"{params['realm']} gave no token")
        status, headers, body = self.request(url, accept, method, {"Authorization": f"Bearer {bearer}"})
        if status == 401:
            raise UpdateError(f"{url}: 401 even with an anonymous token (a private image?)")
        return headers, body

    def image_tag_exists(self, image: str, tag: str) -> bool:
        registry, repo = split_image(image)
        try:
            self._registry(
                f"https://{registry}/v2/{repo}/manifests/{tag}", f"repository:{repo}:pull", MANIFEST_TYPES, "HEAD"
            )
            return True
        except NotFound:
            return False

    def image_tags(self, image: str, max_pages: int = 50) -> list[str]:
        registry, repo = split_image(image)
        url: str | None = f"https://{registry}/v2/{repo}/tags/list?n=1000"
        tags: list[str] = []
        for _ in range(max_pages):
            if url is None:
                return tags
            headers, body = self._registry(url, f"repository:{repo}:pull", "application/json")
            try:
                tags.extend(json.loads(body).get("tags") or [])
            except json.JSONDecodeError as e:
                raise UpdateError(f"{url} didn't return JSON: {e}") from e
            link = next((v for k, v in headers.items() if k.lower() == "link"), "")
            m = re.search(r"<([^>]+)>;\s*rel=\"?next\"?", link)
            url = urllib.parse.urljoin(f"https://{registry}/", m.group(1)) if m else None
        raise UpdateError(f"{image} has more than {max_pages} pages of tags")


def split_image(image: str) -> tuple[str, str]:
    """"rust" -> (registry-1.docker.io, library/rust); "ghcr.io/a/b" ->
    (ghcr.io, a/b)."""
    image = image.split("@", 1)[0]
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "registry-1.docker.io", image
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    return registry, repo
