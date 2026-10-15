"""Transcript sources. Each module exposes NAME, files(cfg), digest(path), hits(path, query, deep)."""
from . import claude, codex

SOURCES = {claude.NAME: claude, codex.NAME: codex}
