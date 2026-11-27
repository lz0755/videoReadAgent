"""Read the published M3 evidence index for one bound analysis."""

import json
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Literal

from video_agent.adapters.storage.connection import connect_sqlite
from video_agent.application.indexing import DOCUMENT_VERSION
from video_agent.application.retrieval import nearby_window, question_times
from video_agent.domain.errors import DomainValidationError, ErrorCode, StorageError
from video_agent.domain.models import TimeRangeMs
from video_agent.domain.ports import SearchHit
from video_agent.domain.tokenizer import (
    DEFAULT_SUBSTRING_FALLBACK_SCAN_LIMIT,
    TOKENIZER_VERSION,
    validate_search_query,
)

_COLUMNS = "d.doc_id,d.source_kind,d.source_id,d.original_text,d.start_ms,d.end_ms"
_SCOPE = "d.asset_id=? AND d.analysis_id=?"
type _Row = tuple[str, Literal["transcript", "observation"], str, str, int, int, float]

_OBSERVATION_REFS = (
    " JOIN observations o ON o.asset_id=d.asset_id AND o.analysis_id=d.analysis_id "
    "AND o.observation_id=d.source_id JOIN observation_evidence oe USING(observation_id,asset_id)"
    " JOIN evidence e USING(evidence_id,asset_id)"
)


def _temporal_rows(
    conn: sqlite3.Connection,
    asset: str,
    analysis: str,
    anchors: tuple[int, int],
    window: TimeRangeMs,
    lexical: list[_Row],
    limit: int,
) -> list[_Row]:
    # The document span can cover an entire coarse window. Use the linked frame's actual PTS
    # and the original utterance's interval to locate a visual and a spoken source at each end.
    params = {
        "asset": asset,
        "analysis": analysis,
        "lo": window.start_ms,
        "hi": window.end_ms,
        "ranked": json.dumps([row[0] for row in lexical]),
        "limit": limit,
    }
    modalities = (
        (
            _OBSERVATION_REFS,
            "abs(e.point_ms-:anchor)",
            "d.source_kind='observation' AND o.kind='observed' AND e.kind='frame' "
            "AND e.point_ms >= :lo AND e.point_ms < :hi",
        ),
        (
            "JOIN evidence e ON e.asset_id=d.asset_id AND e.evidence_id=d.source_id",
            "max(e.start_ms-:anchor,:anchor-(e.end_ms-1),0)",
            "d.source_kind='transcript' AND e.kind='transcript' "
            "AND e.end_ms>:lo AND e.start_ms<:hi",
        ),
    )
    endpoints: list[list[_Row]] = []
    extras: list[_Row] = []
    for anchor in dict.fromkeys(anchors):
        nearest: list[_Row] = []
        for joins, distance, modality in modalities:
            rows: list[_Row] = conn.execute(
                f"SELECT {_COLUMNS},1.0/(1+min({distance})) AS proximity FROM search_docs d "
                f"{joins} WHERE d.asset_id=:asset AND d.analysis_id=:analysis "
                f"AND d.end_ms>:lo AND d.start_ms<:hi AND {modality} GROUP BY d.doc_id "
                "ORDER BY proximity DESC, coalesce((SELECT key FROM json_each(:ranked) "
                "WHERE value=d.doc_id),:limit),d.start_ms,d.doc_id LIMIT :limit",
                {**params, "anchor": anchor},
            ).fetchall()
            if rows:
                nearest.append(rows[0])
                extras.extend(rows[1:])
        endpoints.append(sorted(nearest, key=lambda row: -row[6]))
    order = [group[i] for i in (0, 1) for group in endpoints if len(group) > i]
    unique: dict[str, _Row] = {}
    for row in [*order, *lexical, *sorted(extras, key=lambda row: -row[6])]:
        unique.setdefault(row[0], row)
    return list(unique.values())[:limit]


def _check_index(conn: sqlite3.Connection, asset: str, analysis: str) -> int:
    binding = conn.execute(
        "SELECT a.duration_ms FROM analysis_runs r JOIN assets a USING(asset_id) "
        "JOIN stage_results s USING(asset_id,analysis_id) "
        "WHERE r.asset_id=? AND r.analysis_id=? AND r.index_state='ready' "
        "AND s.stage='index' AND s.item_key='all' AND s.status='committed'",
        (asset, analysis),
    ).fetchone()
    versions = conn.execute(
        "SELECT DISTINCT index_version FROM search_docs WHERE asset_id=? AND analysis_id=? LIMIT 2",
        (asset, analysis),
    ).fetchall()
    if binding is None or versions != [(f"{TOKENIZER_VERSION}/{DOCUMENT_VERSION}",)]:
        raise StorageError(ErrorCode.INDEX_NOT_READY, "analysis index is unavailable or outdated")
    return int(binding[0])


def _references(
    conn: sqlite3.Connection, asset: str, analysis: str, kind: str, src: str
) -> list[str]:
    if kind == "transcript":
        rows = conn.execute(
            "SELECT evidence_id FROM evidence WHERE asset_id=? AND evidence_id=? "
            "AND kind='transcript'",
            (asset, src),
        )
    else:
        rows = conn.execute(
            "SELECT e.evidence_id FROM observations o JOIN observation_evidence e "
            "USING(observation_id,asset_id) JOIN evidence USING(evidence_id,asset_id) "
            "WHERE o.asset_id=? AND o.analysis_id=? AND o.observation_id=? ORDER BY e.evidence_id",
            (asset, analysis, src),
        )
    ids = [row[0] for row in rows]
    if not ids:
        raise StorageError(ErrorCode.INDEX_NOT_READY, "indexed source has no bound evidence")
    return ids


class SQLiteSearch:
    def __init__(self, db_path: Path) -> None:
        self._db = db_path

    def search(
        self,
        asset_id: str,
        analysis_id: str,
        query: str,
        *,
        limit: int = 5,
        time_range: TimeRangeMs | None = None,
    ) -> list[SearchHit]:
        normalized, tokens, fallback = validate_search_query(query)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise DomainValidationError(ErrorCode.SCHEMA_VALIDATION_FAILED, "limit must be 1..10")
        with closing(connect_sqlite(self._db)) as conn:
            conn.execute("BEGIN")
            duration = _check_index(conn, asset_id, analysis_id)
            where = _SCOPE
            params: list[str | int] = [asset_id, analysis_id]
            if time_range is not None:
                time_range.validate_within(duration)
            anchors = question_times(normalized)
            if anchors is not None:
                bound = time_range or TimeRangeMs(start_ms=0, end_ms=duration)
                time_range = nearby_window(anchors, bound)
                if time_range is None:
                    return []
            if time_range is not None:
                where += " AND d.end_ms>? AND d.start_ms<?"
                params.extend((time_range.start_ms, time_range.end_ms))
            if anchors is not None and time_range is not None:
                # A matching word in a broad observation still requires an actual source in range.
                where += (
                    " AND (d.source_kind='transcript' OR EXISTS (SELECT 1 FROM observations o "
                    "JOIN observation_evidence oe USING(observation_id,asset_id) "
                    "JOIN evidence e USING(evidence_id,asset_id) "
                    "WHERE o.observation_id=d.source_id AND o.analysis_id=d.analysis_id "
                    "AND o.asset_id=d.asset_id "
                    "AND ((e.point_ms>=? AND e.point_ms<?) OR (e.end_ms>? AND e.start_ms<?)) ))"
                )
                params.extend((time_range.start_ms, time_range.end_ms) * 2)
            if fallback:
                cap = DEFAULT_SUBSTRING_FALLBACK_SCAN_LIMIT
                count = conn.execute(
                    f"SELECT count(*) FROM (SELECT 1 FROM search_docs d WHERE {where} LIMIT ?)",
                    (*params, cap + 1),
                ).fetchone()
                if count[0] > cap:
                    raise DomainValidationError(
                        ErrorCode.SCHEMA_VALIDATION_FAILED,
                        "narrow the single-character search",
                        details={"scan_limit": cap},
                    )
                rows = [
                    row
                    for row in conn.execute(
                        f"SELECT {_COLUMNS},0.0 FROM search_docs d WHERE {where} "
                        "ORDER BY d.start_ms,d.source_kind,d.doc_id LIMIT ?",
                        (*params, cap),
                    )
                    if normalized in unicodedata.normalize("NFKC", row[3]).lower()
                ][:limit]
            else:
                expression = " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens))
                center = (time_range.start_ms + time_range.end_ms) // 2 if time_range else 0
                rows = conn.execute(
                    f"SELECT {_COLUMNS},-bm25(search_fts) FROM search_fts JOIN search_docs d "
                    f"ON d.doc_id=search_fts.doc_id WHERE {where} AND search_fts MATCH ? "
                    "ORDER BY bm25(search_fts),abs(d.start_ms-?),d.source_kind,d.doc_id LIMIT ?",
                    (*params, expression, center, limit),
                ).fetchall()
                if anchors is not None and time_range is not None:
                    rows = _temporal_rows(
                        conn, asset_id, analysis_id, anchors, time_range, rows, limit
                    )
            return [
                SearchHit(
                    doc_id=doc,
                    asset_id=asset_id,
                    analysis_id=analysis_id,
                    source_kind=kind,
                    source_id=src,
                    text=original,
                    time_range=TimeRangeMs(start_ms=start, end_ms=end),
                    evidence_ids=_references(conn, asset_id, analysis_id, kind, src),
                    score=score,
                )
                for doc, kind, src, original, start, end, score in rows
            ]
