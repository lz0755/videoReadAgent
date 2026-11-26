"""Transactional observation, evidence and provenance storage."""

import sqlite3
from pathlib import Path

from video_agent.application.observations import WindowResult


class SQLiteObservations:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def save(self, result: WindowResult) -> None:
        raise NotImplementedError


def write_window(conn: sqlite3.Connection, result: WindowResult) -> None:
    raise NotImplementedError
