"""Test bootstrap: put the repo on sys.path; stub zstandard / pygltflib only where they are not installed (the tests never decompress or write glTF)."""
import sys, types
REPO = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, REPO + "/dae_core")
sys.path.insert(0, REPO)
try:
    import zstandard  # noqa
except ImportError:
    class _Z:
        def decompress(self, b): return b
    sys.modules["zstandard"] = types.SimpleNamespace(ZstdDecompressor=_Z)
try:
    import pygltflib  # noqa
except ImportError:                                  # only so parse.gltf_conv imports here; nothing glTF is executed in these tests
    from unittest.mock import MagicMock
    sys.modules["pygltflib"] = MagicMock()
