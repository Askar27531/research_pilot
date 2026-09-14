from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import dump_json, load_json, utc_now
from app.schemas import ResearchRequest


def _elapsed_minutes(since: str | None, now: datetime | None = None) -> float:
    """Wall-clock minutes between *since* (ISO) and now; 0 when unknown."""
    if not since:
        return 0.0
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        return 0.0
    reference = now or datetime.now(UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (reference - started).total_seconds() / 60.0)


def _doc_scope(document_ids: list[str] | None) -> tuple[str, list]:
    """SQL fragment + params to restrict a metrics query to *document_ids*.

    ``None`` means "no scope filter" (project-wide). An *empty* list means "the
    scoped set is empty" and is signalled by returning a filter of ``None`` so
    callers can short-circuit to zero results instead of building ``IN ()``.
    """
    if document_ids is None:
        return "", []
    doc_ids = list(dict.fromkeys(document_ids))
    if not doc_ids:
        return None, []
    markers = ",".join("?" for _ in doc_ids)
    return f" AND document_id IN ({markers})", doc_ids



class ResearchDataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_workspace(
        self,
        name: str,
        request: ResearchRequest,
    ) -> tuple[str, str]:
        """Create the project and its first job in one transaction."""
        project_id, job_id = str(uuid4()), str(uuid4())
        now = utc_now()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO projects(id,name,goal,request_json,status,current_stage,"
                "created_at,updated_at,version) VALUES(?,?,?,?,'created','initialized',?,?,1)",
                (project_id, name.strip(), request.research_question,
                 request.model_dump_json(), now, now),
            )
            await connection.execute(
                "INSERT INTO workflow_jobs(id,project_id,run_id,status,attempts,created_at,"
                "updated_at,job_type) VALUES(?,?,?,'queued',0,?,?,'research')",
                (job_id, project_id, f"workspace:{job_id}", now, now),
            )
            await connection.commit()
        return project_id, job_id

    async def review_evidence(
        self,
        project_id: str,
        evidence_id: str,
        status: str,
        note: str | None,
        *,
        source: str = "human",
        review_session_id: str | None = None,
    ) -> None:
        now = utc_now()
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM evidence WHERE id=? AND project_id=?", (evidence_id, project_id)
            )).fetchone()
            if row is None:
                raise RecordNotFoundError("Evidence not found")
            await connection.execute(
                "INSERT INTO evidence_reviews(evidence_id,project_id,status,note,source,"
                "review_session_id,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(evidence_id) DO UPDATE SET "
                "status=excluded.status,note=excluded.note,source=excluded.source,"
                "review_session_id=excluded.review_session_id,updated_at=excluded.updated_at",
                (evidence_id, project_id, status, note, source, review_session_id, now),
            )
            await connection.commit()

    async def list_reviews(self, project_id: str) -> list[dict]:
        """Return every evidence review row (decision desk provenance)."""
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT evidence_id,status,note,source,review_session_id,updated_at "
                "FROM evidence_reviews WHERE project_id=?",
                (project_id,),
            )).fetchall()
        return [dict(row) for row in rows]

    async def is_pause_requested(self, project_id: str) -> bool:
        """Cooperative pause flag checked at every safe analysis boundary."""
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT pause_requested FROM projects WHERE id=?", (project_id,)
            )).fetchone()
        return bool(row and row["pause_requested"])

    # --- M3 cost-threshold pause: usage ledger + budget windows ---

    async def record_usage(
        self,
        project_id: str,
        *,
        phase: str | None = None,
        kind: str,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int | None = None,
    ) -> None:
        """Append one metered LLM call (append-only audit row, FK-cascaded)."""
        if kind not in {"text", "vision"}:
            raise ValueError(f"Unknown usage kind: {kind}")
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO llm_usage(id,project_id,phase,kind,model,prompt_tokens,"
                "completion_tokens,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), project_id, phase, kind, model,
                 int(prompt_tokens), int(completion_tokens), latency_ms, utc_now()),
            )
            await connection.commit()

    async def usage_totals(self, project_id: str) -> dict[str, Any]:
        """Cumulative metered usage for one project (tokens/calls/vision/first_at)."""
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens,"
                " COUNT(*) AS calls,"
                " COALESCE(SUM(CASE WHEN kind='vision' THEN 1 ELSE 0 END),0) AS vision_calls,"
                " MIN(created_at) AS first_at FROM llm_usage WHERE project_id=?",
                (project_id,),
            )).fetchone()
        return {
            "tokens": int(row["tokens"]),
            "calls": int(row["calls"]),
            "vision_calls": int(row["vision_calls"]),
            "first_at": row["first_at"],
        }

    async def budget_window(self, project_id: str) -> dict[str, Any]:
        """Current allowance window for a project (defaults to an empty window)."""
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM budget_windows WHERE project_id=?", (project_id,)
            )).fetchone()
        if row is None:
            return {
                "project_id": project_id, "baseline_tokens": 0, "baseline_vision": 0,
                "since": None, "open": False, "opened_at": None, "snapshot": None,
            }
        return {
            "project_id": row["project_id"],
            "baseline_tokens": int(row["baseline_tokens"]),
            "baseline_vision": int(row["baseline_vision"]),
            "since": row["since"],
            "open": bool(row["open"]),
            "opened_at": row["opened_at"],
            "snapshot": load_json(row["snapshot_json"]),
        }

    async def budget_pause_reason(self, project_id: str, settings: Settings) -> str | None:
        """Decide at a safe boundary whether the analysis must stop.

        Returns ``"user"`` when the human requested a cooperative pause (always
        honoured, even with the gate disabled), ``"budget_gate"`` when a budget
        *window* crossed a configured threshold (opening the gate once), else
        ``None`` to keep running. Windows are re-baselined by
        :meth:`ack_budget_gate`, so a human "continue" buys another full
        threshold-sized block instead of re-pausing on the very next step.
        """
        if await self.is_pause_requested(project_id):
            return "user"
        if not settings.budget_gate_enabled:
            return None
        totals = await self.usage_totals(project_id)
        window = await self.budget_window(project_id)
        if window["open"]:
            return None  # already parked at a budget pause; waiting for a human
        elapsed = _elapsed_minutes(window["since"] or totals["first_at"])
        exceeded = (
            totals["tokens"] - window["baseline_tokens"] >= settings.budget_gate_tokens
            or totals["vision_calls"] - window["baseline_vision"]
            >= settings.budget_gate_vision_calls
            or elapsed >= settings.budget_gate_minutes
        )
        if not exceeded:
            return None
        await self._open_budget_gate(project_id, totals, window)
        return "budget_gate"

    async def _open_budget_gate(self, project_id: str, totals: dict[str, Any],
                                window: dict[str, Any]) -> None:
        """Atomically mark the gate open and snapshot the triggering usage."""
        now = utc_now()
        since = window["since"] or totals["first_at"] or now
        snapshot = dump_json({
            "tokens": totals["tokens"], "vision_calls": totals["vision_calls"],
            "calls": totals["calls"],
        })
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO budget_windows(project_id,baseline_tokens,baseline_vision,"
                "since,open,opened_at,snapshot_json) VALUES(?,?,?,?,1,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET open=1,opened_at=excluded.opened_at,"
                "snapshot_json=excluded.snapshot_json",
                (project_id, window["baseline_tokens"], window["baseline_vision"],
                 since, now, snapshot),
            )
            await connection.commit()

    async def ack_budget_gate(self, project_id: str) -> None:
        """Human "continue": close any open gate and re-baseline the window.

        After an ack the window counts usage since *now*, so the next pause only
        happens after another threshold-sized block of spend (soft supervisor,
        not a per-step nag). Idempotent for every analysis-enqueueing action.
        """
        totals = await self.usage_totals(project_id)
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO budget_windows(project_id,baseline_tokens,baseline_vision,"
                "since,open,opened_at,snapshot_json) VALUES(?,?,?,?,0,NULL,NULL) "
                "ON CONFLICT(project_id) DO UPDATE SET baseline_tokens=excluded.baseline_tokens,"
                "baseline_vision=excluded.baseline_vision,since=excluded.since,open=0,"
                "opened_at=NULL,snapshot_json=NULL",
                (project_id, totals["tokens"], totals["vision_calls"], utc_now()),
            )
            await connection.commit()

    async def set_progress(
        self,
        project_id: str,
        paper_id: str,
        stage_key: str,
        status: str,
        *,
        label: str | None = None,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Upsert one cell of the structured per-(paper, stage) analysis board."""
        now = utc_now()
        # ``done`` is NOT NULL in analysis_progress. Stage rows that are not a
        # counted board (e.g. the per-paper specialist/overview/index stages)
        # may omit it; coerce None -> 0 so a first-time INSERT never violates
        # the constraint. Counting stages (visuals) pass a real value.
        done_value = 0 if done is None else done
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO analysis_progress(project_id,paper_id,stage_key,status,"
                "label,done,total,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,paper_id,stage_key) DO UPDATE SET "
                "status=excluded.status,updated_at=excluded.updated_at,"
                "label=COALESCE(excluded.label,analysis_progress.label),"
                "done=COALESCE(excluded.done,analysis_progress.done),"
                "total=COALESCE(excluded.total,analysis_progress.total)",
                (project_id, paper_id, stage_key, status, label, done_value, total, now),
            )
            await connection.commit()

    async def park_running_stages(self, project_id: str) -> None:
        """After a pause, no stage may still read as ``running`` on the board."""
        async with self.database.connect() as connection:
            await connection.execute(
                "UPDATE analysis_progress SET status='queued',updated_at=? "
                "WHERE project_id=? AND status='running'",
                (utc_now(), project_id),
            )
            await connection.commit()

    async def list_progress(self, project_id: str) -> list[dict]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT * FROM analysis_progress WHERE project_id=? "
                "ORDER BY paper_id,stage_key",
                (project_id,),
            )).fetchall()
        return [dict(row) for row in rows]

    async def reviewed_evidence_ids(self, project_id: str,
                                    evidence_ids: list[str]) -> set[str]:
        """Return evidence ids that already carry a human/automatic review.

        Automatic passes only touch evidence that has never been decided, so they
        never overwrite a human verdict on resume or re-analysis.
        """
        if not evidence_ids:
            return set()
        markers = ",".join("?" for _ in evidence_ids)
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                f"SELECT evidence_id FROM evidence_reviews WHERE project_id=? "
                f"AND status!='unreviewed' AND evidence_id IN ({markers})",
                (project_id, *evidence_ids),
            )).fetchall()
        return {row["evidence_id"] for row in rows}

    async def review_counts(self, project_id: str) -> dict[str, int]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT status,COUNT(*) count FROM evidence_reviews WHERE project_id=? GROUP BY status",
                (project_id,),
            )).fetchall()
        result = {"unreviewed": 0, "confirmed": 0, "doubted": 0, "excluded": 0}
        result.update({row["status"]: row["count"] for row in rows})
        return result

    async def analysis_metrics(self, project_id: str,
                               document_ids: list[str] | None = None) -> dict[str, int | str | None]:
        doc_filter, doc_params = _doc_scope(document_ids)
        if doc_filter is None:
            return {
                "analyzed_pages": 0, "analyzed_visuals": 0, "total_visuals": 0,
                "completed_visuals": 0, "current_step": None,
            }
        async with self.database.connect() as connection:
            page_row = await (await connection.execute(
                "SELECT COALESCE(SUM(page_count),0) pages FROM ("
                "SELECT document_id,MAX(page_count) page_count FROM document_analyses "
                f"WHERE project_id=? AND status='completed'{doc_filter} GROUP BY document_id)",
                (project_id, *doc_params),
            )).fetchone()
            region_row = await (await connection.execute(
                f"SELECT COUNT(*) regions FROM visual_regions WHERE project_id=?{doc_filter}",
                (project_id, *doc_params),
            )).fetchone()
            progress_row = await (await connection.execute(
                "SELECT COALESCE(SUM(total_visuals),0) total_visuals,"
                "COALESCE(SUM(completed_visuals),0) completed_visuals,MAX(current_step) current_step "
                f"FROM document_analyses WHERE project_id=? AND id IN ("
                "SELECT id FROM document_analyses current WHERE current.project_id=? "
                "AND current.updated_at=(SELECT MAX(latest.updated_at) FROM document_analyses latest "
                f"WHERE latest.document_id=current.document_id)){doc_filter}",
                (project_id, project_id, *doc_params),
            )).fetchone()
        return {
            "analyzed_pages": page_row["pages"],
            "analyzed_visuals": region_row["regions"],
            "total_visuals": progress_row["total_visuals"],
            "completed_visuals": progress_row["completed_visuals"],
            "current_step": progress_row["current_step"],
        }

    async def pending_documents(self, project_id: str) -> list[dict]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT d.*,p.id paper_id FROM documents d JOIN papers p ON p.id=d.paper_id "
                "WHERE d.project_id=? AND NOT EXISTS (SELECT 1 FROM document_analyses a "
                "WHERE a.document_id=d.id AND a.pipeline_version=3 AND a.input_hash=d.sha256 "
                "AND a.status='completed')", (project_id,),
            )).fetchall()
        return [dict(row) for row in rows]

    async def start_document_analysis(self, project_id: str, document_id: str,
                                      input_hash: str, vision_model: str) -> str:
        identifier, now = str(uuid4()), utc_now()
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO document_analyses(id,project_id,document_id,pipeline_version,status,"
                "vision_model,warnings_json,input_hash,created_at,updated_at) "
                "VALUES(?,?,?,3,'running',?,'[]',?,?,?) ON CONFLICT(document_id,pipeline_version,input_hash) "
                "DO UPDATE SET status='running',error_json=NULL,updated_at=excluded.updated_at",
                (identifier, project_id, document_id, vision_model, input_hash, now, now),
            )
            row = await (await connection.execute(
                "SELECT id FROM document_analyses WHERE document_id=? AND pipeline_version=3 AND input_hash=?",
                (document_id, input_hash),
            )).fetchone()
            await connection.commit()
        return row["id"]

    async def start_visual_progress(self, analysis_id: str, total_visuals: int) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "UPDATE document_analyses SET total_visuals=?,completed_visuals=0,"
                "current_step='正在准备图表分析',updated_at=? WHERE id=?",
                (total_visuals, utc_now(), analysis_id),
            )
            await connection.commit()

    async def advance_visual_progress(self, analysis_id: str, label: str) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "UPDATE document_analyses SET completed_visuals=MIN(total_visuals,completed_visuals+1),"
                "current_step=?,updated_at=? WHERE id=?",
                (label, utc_now(), analysis_id),
            )
            await connection.commit()

    async def set_analysis_step(self, project_id: str, step: str) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "UPDATE document_analyses SET current_step=?,updated_at=? WHERE id=("
                "SELECT id FROM document_analyses WHERE project_id=? "
                "ORDER BY updated_at DESC LIMIT 1)",
                (step, utc_now(), project_id),
            )
            await connection.commit()

    async def save_visual_region(self, analysis_id: str, project_id: str, document_id: str,
                                 region_type: str, page_number: int, label: str | None,
                                 bbox: dict, source_path: str, source_hash: str, payload: dict) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO visual_regions(id,analysis_id,project_id,document_id,region_type,"
                "page_number,label,bbox_json,source_path,source_hash,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), analysis_id, project_id, document_id, region_type, page_number,
                 label, dump_json(bbox), source_path, source_hash, dump_json(payload), utc_now()),
            )
            await connection.commit()

    async def visual_keys(self, analysis_id: str) -> set[str]:
        """Keys of already-persisted visual regions of one analysis run.

        A cooperative pause can therefore resume mid-document without re-paying
        the vision calls for visuals that were finished before the pause.
        """
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT region_type,page_number,label FROM visual_regions "
                "WHERE analysis_id=?",
                (analysis_id,),
            )).fetchall()
        return {f"{row['page_number']}|{row['region_type']}|{row['label'] or ''}"
                for row in rows}

    async def running_visual_analysis(self, project_id: str,
                                      document_ids: list[str] | None = None) -> dict | None:
        """The document-analysis run currently doing visual observation.

        Drives the "which page / which figure or table is being analyzed"
        line in the UI; None when no visual pass is in flight. When
        ``document_ids`` is given it is restricted to the current selection so a
        superseded paper's still-running pass never shows up as "current".
        """
        doc_filter, doc_params = _doc_scope(document_ids)
        if doc_filter is None:
            return None
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT id,document_id,total_visuals,completed_visuals FROM document_analyses "
                f"WHERE project_id=? AND status='running' AND total_visuals>0{doc_filter} "
                "ORDER BY updated_at DESC LIMIT 1",
                (project_id, *doc_params),
            )).fetchone()
        return dict(row) if row else None

    async def visual_regions_for_document(
        self, project_id: str, document_id: str
    ) -> list[dict]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT * FROM visual_regions WHERE project_id=? AND document_id=? "
                "ORDER BY page_number,region_type,created_at",
                (project_id, document_id),
            )).fetchall()
        return [
            {
                **dict(row),
                "bbox": load_json(row["bbox_json"]),
                "payload": load_json(row["payload_json"]),
            }
            for row in rows
        ]

    async def get_visual_region(self, project_id: str, region_id: str) -> dict:
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT * FROM visual_regions WHERE id=? AND project_id=?",
                (region_id, project_id),
            )).fetchone()
        if row is None:
            raise RecordNotFoundError("Visual region not found")
        return {
            **dict(row),
            "bbox": load_json(row["bbox_json"]),
            "payload": load_json(row["payload_json"]),
        }

    async def finish_document_analysis(self, analysis_id: str, page_count: int,
                                       warnings: list[str], error: dict | None = None) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "UPDATE document_analyses SET status=?,page_count=?,warnings_json=?,error_json=?,"
                "updated_at=? WHERE id=?", ("failed" if error else "completed", page_count,
                dump_json(warnings), dump_json(error) if error else None, utc_now(), analysis_id),
            )
            await connection.commit()
