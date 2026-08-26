#!/usr/bin/env python3
"""Prepare and execute the synthetic eight-Action business-acceptance chain.

The scenario definition is persisted under ``mvp_uat``. Business rows are never
seeded directly: each one must be produced by its owning Action through the
personal-credit workflow API.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_FILE = ROOT / "scripts" / "action_chain_uat_scenario.json"
SCHEMA_FILE = ROOT / "postgres" / "action_chain_uat_schema.sql"
ACTION_ORDER = (
    "create_loan_application",
    "request_credit_report",
    "request_fraud_report",
    "generate_risk_assessment",
    "create_phone_verification_task",
    "record_phone_verification",
    "trigger_manual_review",
    "record_decision_recommendation",
)
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class UatError(RuntimeError):
    pass


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _run(command: list[str], *, stdin: str | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            input=stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise UatError(f"命令不可用：{command[0]}") from error
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise UatError(f"命令执行失败（{command[0]}）：{message}")
    return completed.stdout.strip()


def _psql(sql: str) -> str:
    return _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "credit",
            "-d",
            "credit_ontology",
            "-A",
            "-t",
        ],
        stdin=sql,
    )


def _json_document_sql(document: dict[str, Any], delimiter: str) -> str:
    serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    token = f"${delimiter}$"
    if token in serialized:
        raise UatError("场景 JSON 与 SQL dollar-quote 分隔符冲突")
    return f"{token}{serialized}{token}::jsonb"


def _load_scenario() -> dict[str, Any]:
    try:
        scenario = json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UatError(f"无法读取 UAT 场景：{error}") from error
    _validate_scenario(scenario)
    return scenario


def _fraud_expectation(application_id: str) -> tuple[str, float]:
    application_digest = hashlib.sha256(application_id.encode("utf-8")).hexdigest()
    provider_request_id = f"FP{application_digest[:28].upper()}"
    response_digest = hashlib.sha256(provider_request_id.encode("utf-8")).hexdigest()
    score = (int(response_digest[:4], 16) % 10001) / 100
    return provider_request_id, score


def _validate_scenario(scenario: dict[str, Any]) -> None:
    if scenario.get("synthetic") is not True:
        raise UatError("UAT 场景必须明确标记 synthetic=true")
    application_id = str(scenario.get("application_id") or "")
    actions = scenario.get("actions")
    if not application_id or not isinstance(actions, list):
        raise UatError("UAT 场景缺少 application_id 或 actions")
    action_names = tuple(item.get("action_name") for item in actions)
    positions = tuple(item.get("position") for item in actions)
    if action_names != ACTION_ORDER or positions != tuple(range(1, 9)):
        raise UatError("UAT 场景必须按固定顺序完整声明八个 Action")
    if any(
        item.get("payload", {}).get("application_id", application_id)
        != application_id
        for item in actions
    ):
        raise UatError("Action payload 中存在跨申请数据")
    expected_request_id, expected_score = _fraud_expectation(application_id)
    deterministic = scenario.get("deterministic_expectations") or {}
    if deterministic.get("fraud_provider_request_id") != expected_request_id:
        raise UatError("场景中的反欺诈供应商请求号不可复算")
    if float(deterministic.get("fraud_score", -1)) != expected_score:
        raise UatError("场景中的反欺诈分数不可复算")
    if not 55 <= expected_score < 85:
        raise UatError("场景反欺诈分数不能稳定触发人工复核")


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _api_request(
    api_base_url: str,
    api_key: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json", "X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(
        f"{api_base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=180) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body
        raise UatError(f"API {method} {path} 返回 {error.code}：{detail}") from error
    except URLError as error:
        raise UatError(f"无法连接 MVP API：{error.reason}") from error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise UatError(f"API {method} {path} 未返回合法 JSON") from error
    if not isinstance(value, dict):
        raise UatError(f"API {method} {path} 返回值不是对象")
    return value


def _api_credentials(api_base_url: str | None) -> tuple[str, str]:
    env = _read_env()
    base_url = api_base_url or os.getenv("MVP_API_BASE_URL") or "http://localhost:8000/api"
    api_key = os.getenv("MVP_API_KEY") or env.get("MVP_API_KEY", "")
    if not api_key:
        raise UatError("未配置 MVP_API_KEY，无法创建或执行工作流")
    return base_url, api_key


def _persist_definition(scenario: dict[str, Any]) -> None:
    _psql(SCHEMA_FILE.read_text(encoding="utf-8"))
    document = _json_document_sql(scenario, "uat_definition")
    sql = f"""
WITH seed AS (SELECT {document} AS doc)
INSERT INTO mvp_uat.action_chain_scenarios (
  scenario_id,dataset_version,application_id,workflow_request_id,synthetic,
  state,definition
)
SELECT
  doc->>'scenario_id',doc->>'dataset_version',doc->>'application_id',
  doc->>'workflow_request_id',(doc->>'synthetic')::boolean,'PREPARED',doc
FROM seed
ON CONFLICT (scenario_id) DO UPDATE SET
  dataset_version=EXCLUDED.dataset_version,
  definition=EXCLUDED.definition,
  updated_at=now()
WHERE mvp_uat.action_chain_scenarios.application_id=EXCLUDED.application_id
  AND mvp_uat.action_chain_scenarios.workflow_request_id=EXCLUDED.workflow_request_id;
"""
    _psql(sql)


def _workflow_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    workflow = detail.get("workflow")
    if not isinstance(workflow, dict):
        raise UatError("工作流响应缺少 workflow")
    workflow_id = str(workflow.get("workflow_id") or "")
    if not UUID_PATTERN.fullmatch(workflow_id):
        raise UatError("工作流响应中的 workflow_id 无效")
    return workflow


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _attach_workflow(scenario_id: str, detail: dict[str, Any]) -> None:
    workflow = _workflow_from_detail(detail)
    workflow_id = str(workflow["workflow_id"])
    state = "PASSED" if workflow.get("status") == "COMPLETED" else "WORKFLOW_READY"
    sql = f"""
UPDATE mvp_uat.action_chain_scenarios
SET workflow_id={_sql_text(workflow_id)},state={_sql_text(state)},updated_at=now()
WHERE scenario_id={_sql_text(scenario_id)};
"""
    _psql(sql)


def prepare(
    scenario: dict[str, Any],
    *,
    api_base_url: str | None,
    database_only: bool,
) -> dict[str, Any]:
    _persist_definition(scenario)
    if database_only:
        return {
            "scenario_id": scenario["scenario_id"],
            "application_id": scenario["application_id"],
            "state": "PREPARED",
            "workflow_id": None,
        }
    base_url, api_key = _api_credentials(api_base_url)
    detail = _api_request(
        base_url,
        api_key,
        "/workflows/personal-credit",
        method="POST",
        payload={
            "application_id": scenario["application_id"],
            "entry_mode": "NEW",
            "request_id": scenario["workflow_request_id"],
        },
    )
    workflow = _workflow_from_detail(detail)
    steps = detail.get("steps") or []
    first = next(
        (item for item in steps if item.get("action_name") == ACTION_ORDER[0]),
        None,
    )
    if workflow.get("status") != "COMPLETED" and (
        not isinstance(first, dict) or first.get("status") not in {"READY", "COMPLETED"}
    ):
        raise UatError("新工作流未进入首 Action 就绪状态")
    _attach_workflow(scenario["scenario_id"], detail)
    return {
        "scenario_id": scenario["scenario_id"],
        "application_id": scenario["application_id"],
        "state": "PASSED" if workflow.get("status") == "COMPLETED" else "WORKFLOW_READY",
        "workflow_id": workflow["workflow_id"],
        "workflow_status": workflow.get("status"),
        "ready_action": first.get("action_name") if first else None,
    }


def _runtime_modes() -> dict[str, str]:
    code = (
        "import json,os;keys=('CREDIT_PROVIDER_MODE','CREDIT_CONSENT_MODE',"
        "'FRAUD_PROVIDER_MODE','PHONE_TASK_DISPATCH_MODE');"
        "print(json.dumps({k:os.getenv(k,'') for k in keys}))"
    )
    output = _run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-c", code]
    )
    try:
        return json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise UatError("无法读取后端外部依赖运行模式") from error


def _assert_runtime(scenario: dict[str, Any]) -> None:
    expected = scenario["required_runtime"]
    actual = _runtime_modes()
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise UatError(
            "UAT 沙箱尚未启用："
            f"{json.dumps(mismatches, ensure_ascii=False)}。请先运行 "
            "docker compose -f docker-compose.yml -f docker-compose.uat.yml "
            "up -d --force-recreate backend frontend"
        )


def _step(detail: dict[str, Any], action_name: str) -> dict[str, Any]:
    for item in detail.get("steps") or []:
        if item.get("action_name") == action_name:
            return item
    raise UatError(f"工作流响应缺少步骤：{action_name}")


def _wait_for_step_terminal(
    api_base_url: str,
    api_key: str,
    workflow_id: str,
    action_name: str,
    detail: dict[str, Any],
    *,
    timeout_seconds: int = 120,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    current = _step(detail, action_name)
    while current.get("status") in {"RUNNING", "WAITING"}:
        if time.monotonic() >= deadline:
            raise UatError(
                f"{action_name} 在 {timeout_seconds} 秒内未进入终态："
                f"{current.get('status')}"
            )
        time.sleep(2)
        detail = _api_request(
            api_base_url,
            api_key,
            f"/workflows/personal-credit/{workflow_id}/refresh",
            method="POST",
        )
        current = _step(detail, action_name)
    return detail, current


def _query_scalar(sql: str) -> str:
    lines = [line.strip() for line in _psql(sql).splitlines() if line.strip()]
    if len(lines) != 1:
        raise UatError(f"数据库查询未返回唯一值：{lines}")
    return lines[0]


def _resolve_payload(
    action: dict[str, Any],
    *,
    scenario: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(action["payload"])
    action_name = action["action_name"]
    application_id = scenario["application_id"]
    if action_name == "record_phone_verification":
        payload["verification_id"] = _query_scalar(
            "SELECT verification_id FROM mvp_control.phone_verification_tasks "
            f"WHERE application_id={_sql_text(application_id)} AND verification_round=1;"
        )
        payload["call_time"] = datetime.now(timezone.utc).isoformat()
    elif action_name == "trigger_manual_review":
        source = _step(detail, "record_phone_verification")
        execution_id = str(source.get("execution_id") or "")
        if not UUID_PATTERN.fullmatch(execution_id):
            raise UatError("电核结果步骤缺少可用的来源执行编号")
        payload["source_execution_id"] = execution_id
    return payload


def _record_observation(
    scenario_id: str,
    workflow_id: str,
    action: dict[str, Any],
    step: dict[str, Any],
) -> None:
    status = str(step.get("status") or "UNKNOWN")
    summary = step.get("result_summary")
    if not isinstance(summary, dict):
        summary = {}
    action_status = summary.get("status")
    notes = [
        f"workflow_step_status={status}",
        f"action_status={action_status or 'unknown'}",
    ]
    observation = {
        "scenario_id": scenario_id,
        "action_name": action["action_name"],
        "position": action["position"],
        "workflow_id": workflow_id,
        "execution_id": step.get("execution_id"),
        "workflow_step_status": status,
        "action_status": action_status,
        "verified": status == "COMPLETED" and action_status == "SUCCEEDED",
        "verification_notes": notes,
        "result_summary": summary,
    }
    document = _json_document_sql(observation, "uat_observation")
    sql = f"""
WITH item AS (SELECT {document} AS doc)
INSERT INTO mvp_uat.action_chain_step_results (
  scenario_id,action_name,position,workflow_id,execution_id,
  workflow_step_status,action_status,verified,verification_notes,result_summary
)
SELECT
  doc->>'scenario_id',doc->>'action_name',(doc->>'position')::integer,
  doc->>'workflow_id',NULLIF(doc->>'execution_id',''),
  doc->>'workflow_step_status',NULLIF(doc->>'action_status',''),
  (doc->>'verified')::boolean,doc->'verification_notes',doc->'result_summary'
FROM item
ON CONFLICT (scenario_id,action_name) DO UPDATE SET
  workflow_id=EXCLUDED.workflow_id,
  execution_id=EXCLUDED.execution_id,
  workflow_step_status=EXCLUDED.workflow_step_status,
  action_status=EXCLUDED.action_status,
  verified=EXCLUDED.verified,
  verification_notes=EXCLUDED.verification_notes,
  result_summary=EXCLUDED.result_summary,
  observed_at=now();
"""
    _psql(sql)


def _set_state(
    scenario_id: str,
    state: str,
    *,
    final_verification: dict[str, Any] | None = None,
) -> None:
    final_sql = "NULL"
    if final_verification is not None:
        final_sql = _json_document_sql(final_verification, "uat_final")
    _psql(
        "UPDATE mvp_uat.action_chain_scenarios SET "
        f"state={_sql_text(state)},final_verification={final_sql},updated_at=now() "
        f"WHERE scenario_id={_sql_text(scenario_id)};"
    )


def _final_verification(scenario: dict[str, Any], workflow_id: str) -> dict[str, Any]:
    application_id = scenario["application_id"]
    create_payload = scenario["actions"][0]["payload"]
    borrower_id = create_payload["borrower"]["id_number"]
    sql = f"""
SELECT jsonb_build_object(
  'workflow_status',(
    SELECT status FROM mvp_control.personal_credit_workflows
    WHERE workflow_id={_sql_text(workflow_id)}
  ),
  'completed_steps',(
    SELECT count(*) FROM mvp_control.personal_credit_workflow_steps
    WHERE workflow_id={_sql_text(workflow_id)} AND status='COMPLETED'
  ),
  'succeeded_executions',(
    SELECT count(*) FROM mvp_control.personal_credit_workflow_steps s
    JOIN mvp_control.action_executions e ON e.execution_id=s.execution_id
    WHERE s.workflow_id={_sql_text(workflow_id)} AND e.status='SUCCEEDED'
  ),
  'unsucceeded_outbox_events',(
    SELECT count(*) FROM mvp_control.action_outbox o
    JOIN mvp_control.personal_credit_workflow_steps s ON s.execution_id=o.execution_id
    WHERE s.workflow_id={_sql_text(workflow_id)} AND o.status<>'SUCCEEDED'
  ),
  'business_rows',jsonb_build_object(
    'loan_application',(SELECT count(*) FROM public.loan_application WHERE application_id={_sql_text(application_id)}),
    'credit_report',(SELECT count(*) FROM public.credit_report WHERE subject_id_number={_sql_text(borrower_id)}),
    'anti_fraud_report',(SELECT count(*) FROM public.anti_fraud_report WHERE application_id={_sql_text(application_id)}),
    'risk_assessment_report',(SELECT count(*) FROM public.risk_assessment_report WHERE application_id={_sql_text(application_id)}),
    'phone_verification_task',(SELECT count(*) FROM mvp_control.phone_verification_tasks WHERE application_id={_sql_text(application_id)}),
    'phone_verification_result',(SELECT count(*) FROM public.phone_verification_record WHERE application_id={_sql_text(application_id)} AND conclusion IS NOT NULL),
    'manual_review_record',(SELECT count(*) FROM mvp_control.manual_review_records WHERE application_id={_sql_text(application_id)}),
    'decision_recommendation',(SELECT count(*) FROM mvp_control.decision_recommendations WHERE application_id={_sql_text(application_id)})
  ),
  'fraud',(
    SELECT jsonb_build_object('score',fraud_score,'result',result)
    FROM public.anti_fraud_report WHERE application_id={_sql_text(application_id)}
    ORDER BY fraud_report_id DESC LIMIT 1
  ),
  'risk',(
    SELECT jsonb_build_object('risk_level',risk_level,'check_result',check_result)
    FROM public.risk_assessment_report WHERE application_id={_sql_text(application_id)}
    ORDER BY assessment_time DESC NULLS LAST LIMIT 1
  ),
  'phone',(
    SELECT jsonb_build_object('conclusion',conclusion,'has_doubts',COALESCE(length(trim(doubts)),0)>0)
    FROM public.phone_verification_record WHERE application_id={_sql_text(application_id)}
    ORDER BY call_time DESC NULLS LAST LIMIT 1
  ),
  'application',(
    SELECT jsonb_build_object('application_status',application_status,'decision_result',decision_result)
    FROM public.loan_application WHERE application_id={_sql_text(application_id)}
  )
);
"""
    raw = _query_scalar(sql)
    try:
        verification = json.loads(raw)
    except json.JSONDecodeError as error:
        raise UatError("最终数据库验证结果不是合法 JSON") from error
    rows = verification.get("business_rows") or {}
    passed = (
        verification.get("workflow_status") == "COMPLETED"
        and int(verification.get("completed_steps", 0)) == 8
        and int(verification.get("succeeded_executions", 0)) == 8
        and int(verification.get("unsucceeded_outbox_events", 0)) == 0
        and all(int(rows.get(name, 0)) == 1 for name in rows)
        and (verification.get("fraud") or {}).get("result") == "人工复核"
        and float((verification.get("fraud") or {}).get("score", -1))
          == float(scenario["deterministic_expectations"]["fraud_score"])
        and (verification.get("risk") or {}).get("risk_level") in {"中", "高"}
        and (verification.get("risk") or {}).get("check_result") == "待人工复核"
        and (verification.get("phone") or {}).get("conclusion") == "存疑"
        and (verification.get("phone") or {}).get("has_doubts") is True
        and (verification.get("application") or {}).get("decision_result") == "转人工"
    )
    verification["passed"] = passed
    return verification


def run_chain(scenario: dict[str, Any], *, api_base_url: str | None) -> dict[str, Any]:
    prepared = prepare(scenario, api_base_url=api_base_url, database_only=False)
    _assert_runtime(scenario)
    base_url, api_key = _api_credentials(api_base_url)
    workflow_id = str(prepared["workflow_id"])
    scenario_id = scenario["scenario_id"]
    _set_state(scenario_id, "RUNNING")
    try:
        detail = _api_request(
            base_url, api_key, f"/workflows/personal-credit/{workflow_id}"
        )
        for action in scenario["actions"]:
            action_name = action["action_name"]
            current = _step(detail, action_name)
            if current.get("status") in {"RUNNING", "WAITING"}:
                detail, current = _wait_for_step_terminal(
                    base_url, api_key, workflow_id, action_name, detail
                )
            if current.get("status") != "COMPLETED":
                if current.get("status") != "READY":
                    raise UatError(
                        f"{action_name} 当前不可执行：{current.get('status')}"
                    )
                payload = _resolve_payload(
                    action, scenario=scenario, detail=detail
                )
                detail = _api_request(
                    base_url,
                    api_key,
                    f"/workflows/personal-credit/{workflow_id}/steps/{action_name}/execute",
                    method="POST",
                    payload={"payload": payload},
                )
                current = _step(detail, action_name)
                if current.get("status") in {"RUNNING", "WAITING"}:
                    detail, current = _wait_for_step_terminal(
                        base_url, api_key, workflow_id, action_name, detail
                    )
            _record_observation(scenario_id, workflow_id, action, current)
            summary = current.get("result_summary") or {}
            if current.get("status") != "COMPLETED" or summary.get("status") != "SUCCEEDED":
                raise UatError(
                    f"{action_name} 未成功：step={current.get('status')}, "
                    f"action={summary.get('status')}"
                )
        verification = _final_verification(scenario, workflow_id)
        if not verification["passed"]:
            raise UatError(
                f"八 Action 已执行但闭环验证未通过："
                f"{json.dumps(verification, ensure_ascii=False)}"
            )
        _set_state(scenario_id, "PASSED", final_verification=verification)
        return {
            "scenario_id": scenario_id,
            "application_id": scenario["application_id"],
            "workflow_id": workflow_id,
            "state": "PASSED",
            "verification": verification,
        }
    except Exception as error:
        failure = {"passed": False, "error": str(error)[:1000]}
        _set_state(scenario_id, "FAILED", final_verification=failure)
        raise


def status(scenario: dict[str, Any]) -> dict[str, Any]:
    scenario_id = scenario["scenario_id"]
    sql = f"""
SELECT jsonb_build_object(
  'scenario_id',s.scenario_id,
  'application_id',s.application_id,
  'state',s.state,
  'workflow_id',s.workflow_id,
  'workflow_status',w.status,
  'verified_steps',(
    SELECT count(*) FROM mvp_uat.action_chain_step_results r
    WHERE r.scenario_id=s.scenario_id AND r.verified
  ),
  'final_verification',s.final_verification,
  'updated_at',s.updated_at
)
FROM mvp_uat.action_chain_scenarios s
LEFT JOIN mvp_control.personal_credit_workflows w ON w.workflow_id=s.workflow_id
WHERE s.scenario_id={_sql_text(scenario_id)};
"""
    raw = _query_scalar(sql)
    return json.loads(raw)


def main() -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="个人信审八 Action 业务验收工具")
    parser.add_argument(
        "--api-base-url",
        help="MVP API 根地址，默认 http://localhost:8000/api",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="落库场景并创建首步就绪工作流")
    prepare_parser.add_argument(
        "--database-only", action="store_true", help="只落库场景，不创建工作流"
    )
    subparsers.add_parser("run", help="依次执行并验证八个 Action")
    subparsers.add_parser("status", help="查询场景和工作流验收状态")
    args = parser.parse_args()
    try:
        scenario = _load_scenario()
        if args.command == "prepare":
            result = prepare(
                scenario,
                api_base_url=args.api_base_url,
                database_only=args.database_only,
            )
        elif args.command == "run":
            result = run_chain(scenario, api_base_url=args.api_base_url)
        else:
            result = status(scenario)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except UatError as error:
        print(f"UAT_ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
