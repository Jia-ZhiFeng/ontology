import threading
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from .audit import proposal_digest
from .models import OntologyProposal
from .versioning import VersionWorkflowError,json_safe_payload,normalize_draft_proposal,require_transition


class OntologyVersionRepository:
    _schema_lock=threading.Lock()
    _schema_ready=False
    _publication_lock_id=824_731_006

    def __init__(self,postgres_service):
        self.postgres=postgres_service

    def ensure_schema(self):
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection,connection.cursor() as cursor:
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.ontology_versions (
                        version varchar(64) PRIMARY KEY,
                        status varchar(16) NOT NULL CHECK (status IN ('draft','reviewed','approved','published')),
                        proposal jsonb NOT NULL,
                        proposal_digest char(64) NOT NULL,
                        audit_result jsonb,
                        audit_digest char(64),
                        notes text NOT NULL DEFAULT '',
                        review_note text NOT NULL DEFAULT '',
                        approval_note text NOT NULL DEFAULT '',
                        is_active boolean NOT NULL DEFAULT false,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        reviewed_at timestamptz,
                        approved_at timestamptz,
                        published_at timestamptz
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ontology_versions_single_active
                    ON mvp_control.ontology_versions ((is_active)) WHERE is_active
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.ontology_publication_events (
                        id bigserial PRIMARY KEY,
                        action varchar(16) NOT NULL CHECK (action IN ('publish','rollback','bootstrap')),
                        from_version varchar(64),
                        to_version varchar(64) NOT NULL,
                        proposal_digest char(64) NOT NULL,
                        details jsonb NOT NULL DEFAULT '{}'::jsonb,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
            type(self)._schema_ready=True

    @contextmanager
    def publication_guard(self):
        """Serialize publication and snapshot-to-graph projection across processes."""
        self.ensure_schema()
        connection=self.postgres.conn()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)",(self._publication_lock_id,))
            yield
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)",(self._publication_lock_id,))
            finally:
                connection.close()

    @staticmethod
    def _summary(row):
        if not row:
            return None
        return {
            key:row[key]
            for key in (
                "version","status","proposal_digest","audit_digest","notes","review_note","approval_note",
                "is_active","created_at","updated_at","reviewed_at","approved_at","published_at",
            )
        }

    def count(self)->int:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS count FROM mvp_control.ontology_versions")
            return cursor.fetchone()["count"]

    def list_versions(self)->list[dict]:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM mvp_control.ontology_versions
                ORDER BY is_active DESC,created_at DESC,version DESC
            """)
            return [self._summary(row) for row in cursor.fetchall()]

    def get(self,version:str)->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE version=%s",(version,))
            row=cursor.fetchone()
        if not row:
            raise VersionWorkflowError(f"本体版本不存在：{version}")
        return dict(row)

    def active(self)->dict|None:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE is_active")
            row=cursor.fetchone()
            return dict(row) if row else None

    def bootstrap(self,proposal:OntologyProposal,audit:dict,is_active:bool):
        self.ensure_schema()
        digest=proposal_digest(proposal)
        status="published" if is_active else "approved"
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO mvp_control.ontology_versions (
                    version,status,proposal,proposal_digest,audit_result,audit_digest,
                    notes,is_active,reviewed_at,approved_at,published_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (version) DO NOTHING
            """,(
                proposal.version,status,Jsonb(proposal.model_dump(mode="json")),digest,
                Jsonb(json_safe_payload(audit)),audit.get("audited_digest"),"采用现有确定性基线",is_active,is_active,
            ))
            if cursor.rowcount and is_active:
                cursor.execute("""
                    INSERT INTO mvp_control.ontology_publication_events
                    (action,from_version,to_version,proposal_digest,details)
                    VALUES ('bootstrap',NULL,%s,%s,%s)
                """,(proposal.version,digest,Jsonb({"reason":"adopt_existing_verified_graph"})))

    def save_draft(self,proposal:OntologyProposal,notes:str="")->dict:
        self.ensure_schema()
        normalized=normalize_draft_proposal(proposal,proposal.version)
        payload=normalized.model_dump(mode="json"); digest=proposal_digest(normalized)
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT status FROM mvp_control.ontology_versions WHERE version=%s FOR UPDATE",(normalized.version,))
            existing=cursor.fetchone()
            if existing and existing["status"]!="draft":
                raise VersionWorkflowError(f"版本 {normalized.version} 已进入 {existing['status']}，内容不可再修改；请创建新版本")
            if existing:
                cursor.execute("""
                    UPDATE mvp_control.ontology_versions
                    SET proposal=%s,proposal_digest=%s,notes=%s,audit_result=NULL,audit_digest=NULL,
                        review_note='',approval_note='',updated_at=now()
                    WHERE version=%s
                """,(Jsonb(payload),digest,notes,normalized.version))
            else:
                cursor.execute("""
                    INSERT INTO mvp_control.ontology_versions
                    (version,status,proposal,proposal_digest,notes)
                    VALUES (%s,'draft',%s,%s,%s)
                """,(normalized.version,Jsonb(payload),digest,notes))
        return self.get(normalized.version)

    def mark_reviewed(self,version:str,audit:dict,note:str="")->dict:
        self.ensure_schema()
        if not audit.get("valid"):
            raise VersionWorkflowError("候选审核未通过，不能进入 reviewed")
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE version=%s FOR UPDATE",(version,))
            row=cursor.fetchone()
            if not row: raise VersionWorkflowError(f"本体版本不存在：{version}")
            require_transition(row["status"],"reviewed")
            if row["proposal_digest"]!=audit.get("audited_digest"):
                raise VersionWorkflowError("审核摘要与已保存草稿不一致")
            cursor.execute("""
                UPDATE mvp_control.ontology_versions
                SET status='reviewed',audit_result=%s,audit_digest=%s,review_note=%s,
                    reviewed_at=now(),updated_at=now()
                WHERE version=%s
            """,(Jsonb(json_safe_payload(audit)),audit["audited_digest"],note,version))
        return self.get(version)

    def record_review_attempt(self,version:str,audit:dict,note:str="")->dict:
        """Keep failed review evidence while leaving the mutable draft in place."""
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT status FROM mvp_control.ontology_versions WHERE version=%s FOR UPDATE",(version,))
            row=cursor.fetchone()
            if not row: raise VersionWorkflowError(f"本体版本不存在：{version}")
            if row["status"]!="draft": raise VersionWorkflowError("只有 draft 状态可以记录审核结果")
            cursor.execute("""
                UPDATE mvp_control.ontology_versions
                SET audit_result=%s,audit_digest=%s,review_note=%s,updated_at=now()
                WHERE version=%s
            """,(Jsonb(json_safe_payload(audit)),audit.get("audited_digest"),note,version))
        return self.get(version)

    def mark_approved(self,version:str,note:str="")->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE version=%s FOR UPDATE",(version,))
            row=cursor.fetchone()
            if not row: raise VersionWorkflowError(f"本体版本不存在：{version}")
            require_transition(row["status"],"approved")
            if not row["audit_digest"] or row["audit_digest"]!=row["proposal_digest"]:
                raise VersionWorkflowError("版本缺少与当前内容一致的审核凭证")
            cursor.execute("""
                UPDATE mvp_control.ontology_versions
                SET status='approved',approval_note=%s,approved_at=now(),updated_at=now()
                WHERE version=%s
            """,(note,version))
        return self.get(version)

    def record_publication(self,version:str,action:str,details:dict|None=None)->dict:
        self.ensure_schema()
        if action not in {"publish","rollback"}:
            raise VersionWorkflowError(f"不支持的发布事件：{action}")
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE is_active FOR UPDATE")
            previous=cursor.fetchone(); previous_version=previous["version"] if previous else None
            cursor.execute("SELECT * FROM mvp_control.ontology_versions WHERE version=%s FOR UPDATE",(version,))
            target=cursor.fetchone()
            if not target: raise VersionWorkflowError(f"本体版本不存在：{version}")
            if action=="publish":
                require_transition(target["status"],"published")
            elif target["status"]!="published":
                raise VersionWorkflowError("只能回滚到曾经成功发布的版本")
            cursor.execute("UPDATE mvp_control.ontology_versions SET is_active=false WHERE is_active")
            cursor.execute("""
                UPDATE mvp_control.ontology_versions
                SET status='published',is_active=true,published_at=COALESCE(published_at,now()),updated_at=now()
                WHERE version=%s
            """,(version,))
            cursor.execute("""
                INSERT INTO mvp_control.ontology_publication_events
                (action,from_version,to_version,proposal_digest,details)
                VALUES (%s,%s,%s,%s,%s)
            """,(action,previous_version,version,target["proposal_digest"],Jsonb(json_safe_payload(details or {}))))
        return self.get(version)

    def list_events(self,limit:int=100)->list[dict]:
        self.ensure_schema(); limit=max(1,min(int(limit),500))
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("""
                SELECT id,action,from_version,to_version,proposal_digest,details,created_at
                FROM mvp_control.ontology_publication_events ORDER BY id DESC LIMIT %s
            """,(limit,))
            return [dict(row) for row in cursor.fetchall()]
