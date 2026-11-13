"""The dependency update engine (see updates/README.md).

Standard library only, so it runs on any runner or laptop with Python 3.11+.
Reading YAML is the one exception: it uses `yq` (preinstalled on GitHub's
Ubuntu runners) or PyYAML, whichever is available.
"""


class UpdateError(Exception):
    """A check that can't complete: its job fails and says why."""
