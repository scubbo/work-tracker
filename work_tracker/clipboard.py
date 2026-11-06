"""macOS clipboard support without a shell or terminal OSC 52 dependency."""

import subprocess


def copy_macos(text):
    subprocess.run(['/usr/bin/pbcopy'], input=text.encode('utf-8'), check=True, timeout=3)
