"""KaraokeGen core library.

Pure modules (lyrics, render) import with no GPU/heavy deps.
GPU module (align) imports torch/audio-separator lazily inside functions.
"""
from KaraokeGen.config import Settings, settings

__all__ = ["Settings", "settings"]
