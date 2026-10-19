"""Web layer for Readback: the Flask app and its server-rendered views."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
