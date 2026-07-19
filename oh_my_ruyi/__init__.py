"""Oh My Ruyi package manager and device provisioning frontend."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oh-my-ruyi")
except PackageNotFoundError:
    __version__ = "unknown"
