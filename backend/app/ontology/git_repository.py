from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone

from .audit import proposal_digest
from .models import OntologyProposal
from .versioning import proposal_diff


REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,62}$")


class OntologyGitError(ValueError):
    pass


def _jsonb(value):
    # Keep pure Git helpers importable in lightweight test environments.
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def validate_ref_name(value: str, label: str = "引用") -> str:
    name = (value or "").strip()
    if not REF_PATTERN.fullmatch(name) or ".." in name or name.endswith("/"):
        raise OntologyGitError(f"{label}名称无效")
    return name


def build_commit_id(
    proposal: OntologyProposal,
    parent_ids: list[str],
    message: str,
    author: str,
    created_at: str,
) -> str:
    payload = {
        "proposal_digest": proposal_digest(proposal),
        "parents": parent_ids,
        "message": message,
        "author": author,
        "created_at": created_at,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OntologyGitRepository:
    """Git-like immutable ontology snapshots; publication remains a separate workflow."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def ensure_schema(self):
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.ontology_git_commits (
                        commit_id char(64) PRIMARY KEY,
                        parent_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                        proposal jsonb NOT NULL,
                        proposal_digest char(64) NOT NULL,
                        message text NOT NULL,
                        author varchar(128) NOT NULL,
                        created_at timestamptz NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.ontology_git_branches (
                        name varchar(64) PRIMARY KEY,
                        head_commit_id char(64) REFERENCES mvp_control.ontology_git_commits(commit_id),
                        is_default boolean NOT NULL DEFAULT false,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ontology_git_single_default
                    ON mvp_control.ontology_git_branches ((is_default)) WHERE is_default
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.ontology_git_tags (
                        name varchar(64) PRIMARY KEY,
                        commit_id char(64) NOT NULL REFERENCES mvp_control.ontology_git_commits(commit_id),
                        message text NOT NULL DEFAULT '',
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cursor.execute("""
                    INSERT INTO mvp_control.ontology_git_branches(name,is_default)
                    VALUES ('main',true) ON CONFLICT (name) DO NOTHING
                """)
            type(self)._schema_ready = True

    def bootstrap(self, proposal: OntologyProposal):
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT head_commit_id FROM mvp_control.ontology_git_branches WHERE name='main' FOR UPDATE")
            row = cursor.fetchone()
            if row and row["head_commit_id"]:
                return row["head_commit_id"]
            created_at = datetime.now(timezone.utc).isoformat()
            message = f"导入已发布本体 {proposal.version}"
            commit_id = build_commit_id(proposal, [], message, "system", created_at)
            cursor.execute("""
                INSERT INTO mvp_control.ontology_git_commits
                (commit_id,parent_ids,proposal,proposal_digest,message,author,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (commit_id) DO NOTHING
            """, (commit_id, _jsonb([]), _jsonb(proposal.model_dump(mode="json")),
                  proposal_digest(proposal), message, "system", created_at))
            cursor.execute("""
                UPDATE mvp_control.ontology_git_branches
                SET head_commit_id=%s,updated_at=now() WHERE name='main' AND head_commit_id IS NULL
            """, (commit_id,))
            return commit_id

    @staticmethod
    def _commit_summary(row):
        return {key: row[key] for key in (
            "commit_id", "parent_ids", "proposal_digest", "message", "author", "created_at"
        )}

    def resolve(self, ref: str) -> str:
        value = (ref or "").strip()
        if not value:
            raise OntologyGitError("引用不能为空")
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT head_commit_id FROM mvp_control.ontology_git_branches WHERE name=%s", (value,))
            row = cursor.fetchone()
            if row and row["head_commit_id"]:
                return row["head_commit_id"].strip()
            cursor.execute("SELECT commit_id FROM mvp_control.ontology_git_tags WHERE name=%s", (value,))
            row = cursor.fetchone()
            if row:
                return row["commit_id"].strip()
            cursor.execute("SELECT commit_id FROM mvp_control.ontology_git_commits WHERE commit_id LIKE %s", (f"{value}%",))
            rows = cursor.fetchall()
        if len(rows) == 1:
            return rows[0]["commit_id"].strip()
        if len(rows) > 1:
            raise OntologyGitError("提交短 ID 不唯一")
        raise OntologyGitError(f"找不到本体 Git 引用：{value}")

    def show(self, ref: str) -> dict:
        commit_id = self.resolve(ref)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_git_commits WHERE commit_id=%s", (commit_id,))
            row = cursor.fetchone()
        return {**self._commit_summary(row), "proposal": row["proposal"]}

    def list_branches(self) -> list[dict]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT b.*,c.message AS head_message,c.author AS head_author,c.created_at AS head_created_at
                FROM mvp_control.ontology_git_branches b
                LEFT JOIN mvp_control.ontology_git_commits c ON c.commit_id=b.head_commit_id
                ORDER BY b.is_default DESC,b.name
            """)
            return [dict(row) for row in cursor.fetchall()]

    def list_tags(self) -> list[dict]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM mvp_control.ontology_git_tags ORDER BY created_at DESC,name")
            return [dict(row) for row in cursor.fetchall()]

    def create_branch(self, name: str, from_ref: str = "main") -> dict:
        branch = validate_ref_name(name, "分支")
        head = self.resolve(from_ref)
        try:
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO mvp_control.ontology_git_branches(name,head_commit_id)
                    VALUES (%s,%s) RETURNING *
                """, (branch, head))
                return dict(cursor.fetchone())
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise OntologyGitError(f"分支已存在：{branch}") from exc
            raise

    def create_tag(self, name: str, ref: str, message: str = "") -> dict:
        tag = validate_ref_name(name, "标签")
        commit_id = self.resolve(ref)
        try:
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO mvp_control.ontology_git_tags(name,commit_id,message)
                    VALUES (%s,%s,%s) RETURNING *
                """, (tag, commit_id, message.strip()))
                return dict(cursor.fetchone())
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise OntologyGitError(f"标签已存在：{tag}") from exc
            raise

    def commit(self, branch: str, proposal: OntologyProposal, message: str, author: str,
               expected_head: str | None = None) -> dict:
        branch = validate_ref_name(branch, "分支")
        message = (message or "").strip()
        author = (author or "").strip()
        if len(message) < 3:
            raise OntologyGitError("提交说明至少需要 3 个字符")
        if not author:
            raise OntologyGitError("提交人不能为空")
        self.ensure_schema()
        created_at = datetime.now(timezone.utc).isoformat()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT head_commit_id FROM mvp_control.ontology_git_branches WHERE name=%s FOR UPDATE", (branch,))
            row = cursor.fetchone()
            if not row:
                raise OntologyGitError(f"分支不存在：{branch}")
            head = row["head_commit_id"].strip() if row["head_commit_id"] else None
            if expected_head is not None and head != expected_head:
                raise OntologyGitError("分支已被其他提交更新，请刷新后重试")
            parents = [head] if head else []
            commit_id = build_commit_id(proposal, parents, message, author, created_at)
            cursor.execute("""
                INSERT INTO mvp_control.ontology_git_commits
                (commit_id,parent_ids,proposal,proposal_digest,message,author,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (commit_id, _jsonb(parents), _jsonb(proposal.model_dump(mode="json")),
                  proposal_digest(proposal), message, author, created_at))
            cursor.execute("""
                UPDATE mvp_control.ontology_git_branches
                SET head_commit_id=%s,updated_at=now() WHERE name=%s
            """, (commit_id, branch))
        return self.show(commit_id)

    def log(self, ref: str = "main", limit: int = 50) -> list[dict]:
        commit_id = self.resolve(ref)
        output = []
        for _ in range(max(1, min(limit, 200))):
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT * FROM mvp_control.ontology_git_commits WHERE commit_id=%s", (commit_id,))
                row = cursor.fetchone()
            if not row:
                break
            output.append(self._commit_summary(row))
            parents = row["parent_ids"] or []
            if not parents:
                break
            commit_id = parents[0]
        return output

    def diff(self, from_ref: str, to_ref: str) -> dict:
        before = self.show(from_ref)
        after = self.show(to_ref)
        difference = proposal_diff(
            OntologyProposal.model_validate(before["proposal"]),
            OntologyProposal.model_validate(after["proposal"]),
        )
        return {
            "from": {"ref": from_ref, "commit_id": before["commit_id"]},
            "to": {"ref": to_ref, "commit_id": after["commit_id"]},
            "change_set": difference,
        }
