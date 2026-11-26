"""Atomic publication of a search index from the complete set of committed windows."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import astuple
from datetime import UTC, datetime
from pathlib import Path

from video_agent.adapters.storage.connection import connect_sqlite, immediate_transaction
from video_agent.application.indexing import DOCUMENT_VERSION, SearchDocument, documents_for
from video_agent.application.observations import timeline_summary
from video_agent.application.prompt_registry import canonical_json_sha256
from video_agent.application.stages import WindowStages
from video_agent.domain.errors import ErrorCode, StorageError
from video_agent.domain.tokenizer import TOKENIZER_VERSION


def _source_stages(
    conn: sqlite3.Connection, asset: str, analysis: str
) -> dict[str, tuple[str, str]]:
    return {
        item: (key, checksum)
        for item, key, checksum in conn.execute(
            "SELECT item_key,config_hash,checksum FROM stage_results "
            "WHERE analysis_id=? AND asset_id=? "
            "AND stage='observe' AND status='committed'",
            (analysis, asset),
        )
    }


class SQLiteSearchIndex:
    def __init__(self, db_path: Path, stages: WindowStages) -> None:
        self._db = db_path
        self._stages = stages

    def publish(
        self,
        asset_id: str,
        analysis_id: str,
        windows: dict[str, str],
        *,
        document_version: str = DOCUMENT_VERSION,
        partial: bool = False,
    ) -> int:
        scope = (analysis_id, asset_id)
        version = f"{TOKENIZER_VERSION}/{document_version}"
        with closing(connect_sqlite(self._db)) as conn:
            expected = _source_stages(conn, asset_id, analysis_id)
            if not windows or windows != {item: key for item, (key, _) in expected.items()}:
                raise StorageError(
                    ErrorCode.INDEX_NOT_READY, "complete committed window set required"
                )
            docs: dict[str, SearchDocument] = {}
            results = []
            for item, key in sorted(windows.items()):
                result = self._stages.load(asset_id, analysis_id, item, key)
                if result is None:
                    raise StorageError(ErrorCode.INDEX_NOT_READY, "window could not be verified")
                results.append(result)
                for doc in documents_for(result):
                    doc_id = canonical_json_sha256(
                        [analysis_id, asset_id, doc.source_kind, doc.source_id]
                    )
                    if doc_id in docs and docs[doc_id] != doc:
                        raise StorageError(
                            ErrorCode.IDEMPOTENCY_CONFLICT, "index source contents differ"
                        )
                    docs[doc_id] = doc
            if not docs:
                raise StorageError(ErrorCode.INDEX_NOT_READY, "no evidence documents to publish")
            key_hash = canonical_json_sha256([version, expected])
            checksum = canonical_json_sha256([(k, astuple(v)) for k, v in sorted(docs.items())])
            summary = timeline_summary(results)
            raw = json.dumps(
                summary, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(raw).hexdigest()
            summary_key = canonical_json_sha256([summary["version"], expected])
            read_summary = (
                "SELECT config_hash,artifact_id,checksum FROM stage_results "
                "WHERE analysis_id=? AND asset_id=? AND stage='summarize' AND item_key='all' AND status='committed'"
            )
            old_summary = conn.execute(read_summary, scope).fetchone()
            existing = old_summary[1] if old_summary and old_summary[0] == summary_key else None
            aid = self._stages.prepare_export(asset_id, existing, raw, digest)
            with immediate_transaction(conn):
                job = conn.execute(
                    "SELECT cancel_requested,status FROM jobs WHERE analysis_id=? AND asset_id=? "
                    "AND kind='analysis'",
                    scope,
                ).fetchone()
                if not job or job[0] or job[1] == "cancelled":
                    raise StorageError(ErrorCode.JOB_CANCELLED, "index job is cancelled or absent")
                if _source_stages(conn, asset_id, analysis_id) != expected:
                    raise StorageError(
                        ErrorCode.INDEX_NOT_READY, "window changed before publication"
                    )
                previous = conn.execute(
                    "SELECT config_hash,checksum FROM stage_results WHERE analysis_id=? "
                    "AND asset_id=? AND stage='index' AND item_key='all' "
                    "AND status='committed'",
                    scope,
                ).fetchone()
                if previous != (key_hash, checksum) or not self._matches(
                    conn, scope, docs, version
                ):
                    self._replace(conn, scope, docs, version, key_hash, checksum)
                if conn.execute(read_summary, scope).fetchone() != (summary_key, aid, digest):
                    conn.execute(
                        """INSERT INTO stage_results(analysis_id,asset_id,stage,item_key,config_hash,
                             artifact_id,checksum,status,created_at) VALUES(?,?,'summarize','all',?,?,?,'committed',?)
                             ON CONFLICT(analysis_id,stage,item_key) DO UPDATE SET config_hash=excluded.config_hash,
                             artifact_id=excluded.artifact_id,checksum=excluded.checksum,status='committed',created_at=excluded.created_at""",
                        (*scope, summary_key, aid, digest, datetime.now(UTC).isoformat()),
                    )
                state = "partial" if partial else "succeeded"
                conn.execute(
                    "UPDATE analysis_runs SET index_state='ready',status=? "
                    "WHERE analysis_id=? AND asset_id=?",
                    (state, *scope),
                )
                conn.execute(
                    "UPDATE jobs SET status=?,updated_at=? WHERE analysis_id=? AND asset_id=? "
                    "AND kind='analysis'",
                    (state, datetime.now(UTC).isoformat(), *scope),
                )
        return len(docs)

    @staticmethod
    def _matches(
        conn: sqlite3.Connection,
        scope: tuple[str, str],
        docs: dict[str, SearchDocument],
        version: str,
    ) -> bool:
        rows = conn.execute(
            "SELECT d.doc_id,d.source_kind,d.source_id,d.original_text,d.tokens,"
            "d.start_ms,d.end_ms,d.index_version,f.tokens FROM search_docs d "
            "LEFT JOIN search_fts f ON f.doc_id=d.doc_id WHERE analysis_id=? AND asset_id=? "
            "ORDER BY d.doc_id",
            scope,
        )
        expected = [(k, *astuple(doc), version, doc.tokens) for k, doc in sorted(docs.items())]
        return rows.fetchall() == expected

    @staticmethod
    def _replace(
        conn: sqlite3.Connection,
        scope: tuple[str, str],
        docs: dict[str, SearchDocument],
        version: str,
        key_hash: str,
        checksum: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "DELETE FROM search_fts WHERE doc_id IN (SELECT doc_id FROM search_docs "
            "WHERE analysis_id=? AND asset_id=?)",
            scope,
        )
        conn.executemany("DELETE FROM search_fts WHERE doc_id=?", [(key,) for key in docs])
        conn.execute("DELETE FROM search_docs WHERE analysis_id=? AND asset_id=?", scope)
        conn.executemany(
            "INSERT INTO search_docs(doc_id,analysis_id,asset_id,source_kind,source_id,"
            "original_text,tokens,start_ms,end_ms,index_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(key, *scope, *astuple(doc), version, now) for key, doc in docs.items()],
        )
        conn.executemany(
            "INSERT INTO search_fts(doc_id,tokens) VALUES(?,?)",
            [(key, doc.tokens) for key, doc in docs.items()],
        )
        conn.execute(
            """INSERT INTO stage_results(analysis_id,asset_id,stage,item_key,config_hash,
                     checksum,status,created_at) VALUES(?,?,'index','all',?,?,'committed',?)
                     ON CONFLICT(analysis_id,stage,item_key) DO UPDATE SET
                     config_hash=excluded.config_hash,
                     checksum=excluded.checksum,status='committed',created_at=excluded.created_at""",
            (*scope, key_hash, checksum, now),
        )
