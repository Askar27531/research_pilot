from uuid import uuid4

from app.db.database import Database
from app.db.errors import RecordNotFoundError
from app.db.repositories import dump_json, load_json, utc_now
from app.schemas import ResearchProfile, ResearchProfileInput, ResearchRequest


class ResearchDataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_workspace(
        self,
        name: str,
        request: ResearchRequest,
        profile_input: ResearchProfileInput,
    ) -> tuple[str, str]:
        """Create the project, profile and first job in one transaction."""
        project_id, profile_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
        now = utc_now()
        profile = ResearchProfile(
            **profile_input.model_dump(),
            profile_id=profile_id,
            project_id=project_id,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO projects(id,name,goal,request_json,status,current_stage,"
                "created_at,updated_at,version) VALUES(?,?,?,?,'created','initialized',?,?,1)",
                (project_id, name.strip(), request.research_question,
                 request.model_dump_json(), now, now),
            )
            await connection.execute(
                "INSERT INTO research_profiles(id,project_id,revision,payload_json,created_at,updated_at) "
                "VALUES(?,?,1,?,?,?)",
                (profile_id, project_id, profile.model_dump_json(), now, now),
            )
            await connection.execute(
                "INSERT INTO workflow_jobs(id,project_id,run_id,status,attempts,created_at,"
                "updated_at,job_type) VALUES(?,?,?,'queued',0,?,?,'research')",
                (job_id, project_id, f"workspace:{job_id}", now, now),
            )
            await connection.commit()
        return project_id, job_id

    async def review_evidence(self, project_id: str, evidence_id: str,
                              status: str, note: str | None) -> None:
        now = utc_now()
        async with self.database.connect() as connection:
            row = await (await connection.execute(
                "SELECT 1 FROM evidence WHERE id=? AND project_id=?", (evidence_id, project_id)
            )).fetchone()
            if row is None:
                raise RecordNotFoundError("Evidence not found")
            await connection.execute(
                "INSERT INTO evidence_reviews(evidence_id,project_id,status,note,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET "
                "status=excluded.status,note=excluded.note,updated_at=excluded.updated_at",
                (evidence_id, project_id, status, note, now),
            )
            await connection.commit()

    async def review_counts(self, project_id: str) -> dict[str, int]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT status,COUNT(*) count FROM evidence_reviews WHERE project_id=? GROUP BY status",
                (project_id,),
            )).fetchall()
        result = {"unreviewed": 0, "confirmed": 0, "doubted": 0, "excluded": 0}
        result.update({row["status"]: row["count"] for row in rows})
        return result

    async def save_search(self, project_id: str, query: str, sources: list[str],
                          result_count: int, warnings: list[str], latency_ms: int) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                "INSERT INTO literature_searches(id,project_id,query,sources_json,result_count,"
                "warnings_json,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), project_id, query, dump_json(sources), result_count,
                 dump_json(warnings), latency_ms, utc_now()),
            )
            await connection.commit()

    async def latest_searches(self, project_id: str, limit: int = 10) -> list[dict]:
        async with self.database.connect() as connection:
            rows = await (await connection.execute(
                "SELECT * FROM literature_searches WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            )).fetchall()
        return [{**dict(row), "sources": load_json(row["sources_json"]),
                 "warnings": load_json(row["warnings_json"])} for row in rows]

    async def analysis_metrics(self, project_id: str) -> dict[str, int | str | None]:
        async with self.database.connect() as connection:
            page_row = await (await connection.execute(
                "SELECT COALESCE(SUM(page_count),0) pages FROM ("
                "SELECT document_id,MAX(page_count) page_count FROM document_analyses "
                "WHERE project_id=? AND status='completed' GROUP BY document_id)",
                (project_id,),
            )).fetchone()
            region_row = await (await connection.execute(
                "SELECT COUNT(*) regions FROM visual_regions WHERE project_id=?",
                (project_id,),
            )).fetchone()
            progress_row = await (await connection.execute(
                "SELECT COALESCE(SUM(total_visuals),0) total_visuals,"
                "COALESCE(SUM(completed_visuals),0) completed_visuals,MAX(current_step) current_step "
                "FROM document_analyses WHERE project_id=? AND id IN ("
                "SELECT id FROM document_analyses current WHERE current.project_id=? "
                "AND current.updated_at=(SELECT MAX(latest.updated_at) FROM document_analyses latest "
                "WHERE latest.document_id=current.document_id))",
                (project_id, project_id),
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
                "WHERE a.document_id=d.id AND a.pipeline_version=2 AND a.input_hash=d.sha256 "
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
                "VALUES(?,?,?,2,'running',?,'[]',?,?,?) ON CONFLICT(document_id,pipeline_version,input_hash) "
                "DO UPDATE SET status='running',error_json=NULL,updated_at=excluded.updated_at",
                (identifier, project_id, document_id, vision_model, input_hash, now, now),
            )
            row = await (await connection.execute(
                "SELECT id FROM document_analyses WHERE document_id=? AND pipeline_version=2 AND input_hash=?",
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
