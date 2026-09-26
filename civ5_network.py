"""HTTPS downloads with certificate roots available inside standalone builds."""
from __future__ import annotations

import ssl
import urllib.request


def https_context() -> ssl.SSLContext:
    # Import on demand so source-only tools such as the menu renderer do not
    # need network dependencies. PyInstaller collects certifi and its CA file.
    import certifi

    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def urlopen(request: str | urllib.request.Request, timeout: float = 30):
    return urllib.request.urlopen(request, timeout=timeout, context=https_context())
