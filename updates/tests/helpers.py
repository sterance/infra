"""Test helpers: temporary repositories and a fake network."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

UPDATES = Path(__file__).resolve().parents[1]
INFRA = UPDATES.parent
sys.path.insert(0, str(UPDATES))
sys.dont_write_bytecode = True

from engine import UpdateError  # noqa: E402,F401
from engine.http import NotFound  # noqa: E402


class FakeHttp:
    """`pages` maps URL -> bytes/str/JSON-able (or an exception to raise);
    unknown URLs are 404s. `tags` maps image -> tags, `images` the
    image:tag pairs that exist, `downloads` URL -> (sha256, size)."""

    def __init__(self, pages=None, tags=None, images=None, downloads=None):
        self.pages = pages or {}
        self.tags = tags or {}
        self.images = images
        self.downloads = downloads or {}
        self.fetched: list[str] = []
        self.downloaded: list[str] = []

    def get(self, url, accept=None):
        self.fetched.append(url)
        if url not in self.pages:
            raise NotFound(url)
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            return v.encode()
        return json.dumps(v).encode()

    def json(self, url, accept=None):
        return json.loads(self.get(url, accept))

    def text(self, url, accept=None):
        return self.get(url, accept).decode()

    def sha256(self, url):
        self.downloaded.append(url)
        return self.downloads[url]

    def image_tag_exists(self, image, tag):
        return True if self.images is None else f"{image}:{tag}" in self.images

    def image_tags(self, image):
        return self.tags[image]


class Repo:
    """A temporary git repository with the given files."""

    def __init__(self, files: dict[str, str], git: bool = True):
        self.dir = Path(tempfile.mkdtemp(prefix="updates-test-"))
        for path, text in files.items():
            self.write(path, text)
        if git:
            self.git("init", "-q", "-b", "main")
            self.git("add", "-A")
            self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout

    def write(self, path, text):
        p = self.dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def read(self, path):
        return (self.dir / path).read_text(encoding="utf-8")

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class RepoTest(unittest.TestCase):
    FILES: dict[str, str] = {}

    def setUp(self):
        self.repo = Repo(self.FILES)

    def tearDown(self):
        self.repo.close()


def yaml_available() -> bool:
    import shutil as sh

    if sh.which("yq"):
        return True
    try:
        import yaml  # noqa: F401

        return True
    except ImportError:
        return False
