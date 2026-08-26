from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..ontology.baseline import build_personal_credit_baseline
from ..ontology.models import OntologyProposal


ACTION_STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "RECEIVED": ("AUTHORIZING", "FAILED"),
    "AUTHORIZING": ("VALIDATING", "FAILED"),
    "VALIDATING": ("RUNNING", "WAITING_APPROVAL", "FAILED"),
    "RUNNING": (
        "WAITING_EXTERNAL",
        "WAITING_APPROVAL",
        "VERIFYING",
        "COMMITTING",
        "FAILED",
    ),
    "WAITING_EXTERNAL": ("RUNNING", "VERIFYING", "FAILED"),
    "WAITING_APPROVAL": ("RUNNING", "FAILED"),
    "VERIFYING": ("COMMITTING", "FAILED"),
    "COMMITTING": ("SUCCEEDED", "COMPENSATING", "FAILED"),
    "COMPENSATING": ("COMPENSATED", "COMPENSATION_FAILED"),
    "SUCCEEDED": (),
    "FAILED": (),
    "COMPENSATED": (),
    "COMPENSATION_FAILED": (),
}

TERMINAL_ACTION_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "COMPENSATED", "COMPENSATION_FAILED"}
)

STEP_KINDS = frozenset(
    {
        "authorize",
        "load",
        "validate",
        "idempotency",
        "function",
        "external_receive",
        "external_call",
        "external_dispatch",
        "manual_gate",
        "commit",
        "verify",
        "audit",
    }
)

EXTERNAL_STEP_KINDS = frozenset(
    {"external_receive", "external_call", "external_dispatch"}
)


@dataclass(frozen=True)
class ActionStepDesign:
    name: str
    kind: str
    depends_on: tuple[str, ...] = ()
    function_name: str | None = None
    effect: str | None = None
    external_system: str | None = None
    timeout_seconds: int = 5
    max_attempts: int = 1
    failure_policy: str = "fail_closed"


@dataclass(frozen=True)
class ActionRuntimeDesign:
    action_name: str
    trigger: str
    request_fields: tuple[str, ...]
    response_fields: tuple[str, ...]
    idempotency_fields: tuple[str, ...]
    steps: tuple[ActionStepDesign, ...]
    completion_rule: str
    consistency_boundary: str
    compensation: tuple[str, ...]
    audit_events: tuple[str, ...]
    error_codes: tuple[str, ...]
    approval_condition: str | None = None


def step(
    name: str,
    kind: str,
    *depends_on: str,
    function_name: str | None = None,
    effect: str | None = None,
    external_system: str | None = None,
    timeout_seconds: int = 5,
    max_attempts: int = 1,
    failure_policy: str = "fail_closed",
) -> ActionStepDesign:
    return ActionStepDesign(
        name=name,
        kind=kind,
        depends_on=tuple(depends_on),
        function_name=function_name,
        effect=effect,
        external_system=external_system,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        failure_policy=failure_policy,
    )


COMMON_RESPONSE_FIELDS = (
    "execution_id",
    "action_name",
    "status",
    "ontology_version",
    "result",
    "warnings",
    "audit_reference",
)

COMMON_AUDIT_EVENTS = (
    "action_received",
    "authorization_checked",
    "idempotency_checked",
    "action_succeeded",
    "action_failed",
)


ACTION_RUNTIME_DESIGNS: dict[str, ActionRuntimeDesign] = {
    "create_loan_application": ActionRuntimeDesign(
        action_name="create_loan_application",
        trigger="进件系统或有权信审员提交一笔新申请",
        request_fields=(
            "application_id",
            "application",
            "borrower",
            "vehicle",
            "dealer",
            "guarantor",
            "co_borrower",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + ("application_id", "created_relations"),
        idempotency_fields=("application_id",),
        steps=(
            step("receive_from_origination", "external_receive", external_system="进件系统"),
            step("authorize_actor", "authorize", "receive_from_origination"),
            step("validate_request", "validate", "authorize_actor"),
            step("validate_axioms", "validate", "validate_request"),
            step("reserve_idempotency", "idempotency", "validate_axioms"),
            step(
                "create_application",
                "commit",
                "reserve_idempotency",
                effect="create LoanApplication",
            ),
            step(
                "create_application_relations",
                "commit",
                "create_application",
                effect="create SUBMITS/TARGETS/SOLD_BY/GUARANTEES/CO_BORROWS",
            ),
            step("verify_persistence", "verify", "create_application_relations"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="申请、适用关系和图同步意图原子持久化，NebulaGraph 同步及映射回读成功",
        consistency_boundary=(
            "主数据、申请与 outbox 在 PostgreSQL 单事务提交；"
            "NebulaGraph 由带租约 worker 幂等同步并最终收敛"
        ),
        compensation=(
            "事务失败时回滚申请和全部关系",
            "图同步重试耗尽时仅删除本 Action 创建且未被后续业务修改或引用的数据",
            "不得覆盖既有主数据或留下孤立申请/角色关系",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "graph_sync_enqueued",
            "application_created",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_REQUEST_INVALID",
            "ACTION_AXIOM_VIOLATION",
            "ACTION_IDEMPOTENCY_CONFLICT",
            "ACTION_APPLICATION_ALREADY_EXISTS",
            "ACTION_MASTER_DATA_CONFLICT",
            "ACTION_WRITE_FAILED",
            "ACTION_VERIFICATION_FAILED",
            "ACTION_GRAPH_SYNC_FAILED",
            "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
        ),
    ),
    "request_credit_report": ActionRuntimeDesign(
        action_name="request_credit_report",
        trigger="有权人员为当前申请发起已授权的个人征信查询",
        request_fields=(
            "application_id",
            "subject_id_number",
            "authorization_reference",
            "query_purpose",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + ("report_id", "provider_request_id"),
        idempotency_fields=("application_id", "subject_id_number"),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_application_subject", "load", "authorize_actor"),
            step("validate_consent", "validate", "load_application_subject"),
            step("reserve_idempotency", "idempotency", "validate_consent"),
            step(
                "query_credit_provider",
                "external_call",
                "reserve_idempotency",
                external_system="征信系统",
                timeout_seconds=30,
                max_attempts=2,
                failure_policy="仅使用同一 provider_request_id 重试，禁止重复计费查询",
            ),
            step("validate_provider_response", "validate", "query_credit_provider"),
            step(
                "create_credit_report",
                "commit",
                "validate_provider_response",
                effect="create CreditReport",
            ),
            step(
                "link_report_holder",
                "commit",
                "create_credit_report",
                effect="create HOLDS",
            ),
            step("verify_persistence", "verify", "link_report_holder"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="征信响应通过结构校验，报告与唯一 HOLDS 关系持久化成功",
        consistency_boundary="查询请求以 provider_request_id 幂等；报告和 HOLDS 在同一事务提交",
        compensation=(
            "外部查询成功但本地写入失败时缓存受保护响应并重试写入",
            "不得通过重新发起征信查询补偿本地写入失败",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "credit_consent_validated",
            "credit_query_dispatched",
            "credit_report_recorded",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_SUBJECT_NOT_FOUND",
            "ACTION_CREDIT_CONSENT_MISSING",
            "ACTION_CREDIT_PROVIDER_NOT_CONFIGURED",
            "ACTION_IDEMPOTENCY_CONFLICT",
            "ACTION_CREDIT_REPORT_ALREADY_EXISTS",
            "ACTION_EXTERNAL_TIMEOUT",
            "ACTION_EXTERNAL_RESPONSE_INVALID",
            "ACTION_WRITE_FAILED",
            "ACTION_GRAPH_SYNC_FAILED",
            "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
        ),
    ),
    "request_fraud_report": ActionRuntimeDesign(
        action_name="request_fraud_report",
        trigger="有权人员为核心关系完整的当前申请发起反欺诈查询",
        request_fields=("application_id", "request_id"),
        response_fields=COMMON_RESPONSE_FIELDS + (
            "fraud_report_id",
            "provider_request_id",
            "association_risk_summary",
        ),
        idempotency_fields=("application_id",),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_application_context", "load", "authorize_actor"),
            step("validate_core_relations", "validate", "load_application_context"),
            step("reserve_idempotency", "idempotency", "validate_core_relations"),
            step(
                "detect_association_risk",
                "function",
                "reserve_idempotency",
                function_name="detect_association_risk",
                timeout_seconds=10,
            ),
            step(
                "query_fraud_provider",
                "external_call",
                "reserve_idempotency",
                external_system="反欺诈系统",
                timeout_seconds=30,
                max_attempts=2,
                failure_policy="仅使用同一 provider_request_id 重试",
            ),
            step(
                "validate_combined_result",
                "validate",
                "detect_association_risk",
                "query_fraud_provider",
            ),
            step(
                "create_fraud_report",
                "commit",
                "validate_combined_result",
                effect="create AntiFraudReport",
            ),
            step(
                "link_fraud_report",
                "commit",
                "create_fraud_report",
                effect="create HAS_FRAUD_REPORT",
            ),
            step("verify_persistence", "verify", "link_fraud_report"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="外部反欺诈结果与关联风险结果均完整，报告及关系提交成功",
        consistency_boundary="外部请求按申请幂等；报告与 HAS_FRAUD_REPORT 同事务提交",
        compensation=(
            "外部查询成功但写入失败时复用受保护响应重试本地提交",
            "关联风险失败或结果不完整时不得生成正式反欺诈报告",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "association_risk_evaluated",
            "fraud_query_dispatched",
            "fraud_report_recorded",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_APPLICATION_INCOMPLETE",
            "ACTION_IDEMPOTENCY_CONFLICT",
            "ACTION_FRAUD_REPORT_ALREADY_EXISTS",
            "ACTION_FUNCTION_NOT_BOUND",
            "ACTION_FUNCTION_FAILED",
            "ACTION_FUNCTION_OUTPUT_INVALID",
            "ACTION_FRAUD_PROVIDER_NOT_CONFIGURED",
            "ACTION_EXTERNAL_TIMEOUT",
            "ACTION_EXTERNAL_RESPONSE_INVALID",
            "ACTION_WRITE_FAILED",
            "ACTION_GRAPH_SYNC_FAILED",
            "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
        ),
    ),
    "generate_risk_assessment": ActionRuntimeDesign(
        action_name="generate_risk_assessment",
        trigger="申请、征信、反欺诈和车辆数据满足评估门禁后生成指定版本评估",
        request_fields=(
            "application_id",
            "assessment_version",
            "data_snapshot_policy",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + (
            "assessment_id",
            "risk_level",
            "pd_value",
            "evidence_summary",
        ),
        idempotency_fields=("application_id", "assessment_version"),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_consistent_snapshot", "load", "authorize_actor", timeout_seconds=30),
            step("validate_assessment_axioms", "validate", "load_consistent_snapshot"),
            step("reserve_idempotency", "idempotency", "validate_assessment_axioms"),
            step(
                "calculate_dti",
                "function",
                "reserve_idempotency",
                function_name="calculate_dti",
            ),
            step(
                "calculate_ltv",
                "function",
                "reserve_idempotency",
                function_name="calculate_ltv",
            ),
            step(
                "evaluate_credit_risk",
                "function",
                "reserve_idempotency",
                function_name="evaluate_credit_risk",
                timeout_seconds=10,
            ),
            step(
                "evaluate_fraud_risk",
                "function",
                "reserve_idempotency",
                function_name="evaluate_fraud_risk",
                timeout_seconds=10,
            ),
            step(
                "assess_application_risk",
                "function",
                "calculate_dti",
                "calculate_ltv",
                "evaluate_credit_risk",
                "evaluate_fraud_risk",
                function_name="assess_application_risk",
                timeout_seconds=30,
            ),
            step("validate_assessment_output", "validate", "assess_application_risk"),
            step(
                "create_assessment_report",
                "commit",
                "validate_assessment_output",
                effect="create RiskAssessmentReport",
            ),
            step(
                "link_assessment_report",
                "commit",
                "create_assessment_report",
                effect="create GENERATES",
            ),
            step("verify_persistence", "verify", "link_assessment_report"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="五个 Function 使用同一数据快照和固定实现版本完成，PostgreSQL 报告提交且 NebulaGraph GENERATES 同步与映射校验成功",
        consistency_boundary="读取使用 PostgreSQL 一致性快照；报告与 outbox 事件同一 PostgreSQL 事务提交；NebulaGraph 由租约 worker 幂等同步，失败自动重试且服务重启可恢复",
        compensation=(
            "任一必需 Function 失败或不完整时不创建正式评估报告",
            "图同步重试耗尽后删除本次新建报告或恢复预创建空报告，再以补偿后快照重同步图；补偿失败进入 COMPENSATION_FAILED",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "assessment_snapshot_loaded",
            "assessment_functions_completed",
            "graph_sync_enqueued",
            "risk_assessment_recorded",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_REQUEST_INVALID",
            "ACTION_ASSESSMENT_ALREADY_EXISTS",
            "ACTION_EXECUTION_IN_PROGRESS",
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
            "ACTION_ASSESSMENT_PREREQUISITE_FAILED",
            "ACTION_MONTHLY_PAYMENT_UNAVAILABLE",
            "ACTION_FUNCTION_NOT_BOUND",
            "ACTION_FUNCTION_FAILED",
            "ACTION_FUNCTION_OUTPUT_INVALID",
            "ACTION_WRITE_FAILED",
            "ACTION_DATA_QUALITY_GATE_FAILED",
            "ACTION_AXIOM_GATE_FAILED",
            "ACTION_GRAPH_SYNC_FAILED",
            "ACTION_GRAPH_MAPPING_FAILED",
            "ACTION_OUTBOX_PAYLOAD_INVALID",
            "ACTION_OUTBOX_STATE_CONFLICT",
            "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
            "ACTION_COMPENSATION_FAILED",
        ),
    ),
    "create_phone_verification_task": ActionRuntimeDesign(
        action_name="create_phone_verification_task",
        trigger="风险信号满足电核条件时创建指定轮次的电核任务",
        request_fields=(
            "application_id",
            "verification_round",
            "trigger_reason",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + (
            "verification_id",
            "question_count",
            "dispatch_status",
        ),
        idempotency_fields=("application_id", "verification_round"),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_risk_context", "load", "authorize_actor"),
            step("validate_phone_trigger", "validate", "load_risk_context"),
            step("reserve_idempotency", "idempotency", "validate_phone_trigger"),
            step(
                "generate_phone_questions",
                "function",
                "reserve_idempotency",
                function_name="generate_phone_questions",
                timeout_seconds=30,
            ),
            step("validate_question_set", "validate", "generate_phone_questions"),
            step(
                "create_phone_record",
                "commit",
                "validate_question_set",
                effect="create PhoneVerificationRecord",
            ),
            step(
                "link_phone_record",
                "commit",
                "create_phone_record",
                effect="create HAS_PHONE_VERIFICATION",
            ),
            step(
                "dispatch_phone_task",
                "external_dispatch",
                "link_phone_record",
                external_system="电核系统",
                timeout_seconds=10,
                max_attempts=3,
                failure_policy="写入可靠 outbox；投递失败保持待投递并告警",
            ),
            step("verify_persistence", "verify", "dispatch_phone_task"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="电核记录、关系和可靠投递事件均已提交；实际呼叫可异步完成",
        consistency_boundary="电核记录、关系和 outbox 同事务提交，外部投递至少一次且接收端幂等",
        compensation=(
            "投递失败不删除已创建任务，状态保持待投递并按同一任务 ID 重试",
            "问题集不完整时不得创建电核任务",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "phone_questions_generated",
            "phone_task_created",
            "phone_task_dispatched",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_REQUEST_INVALID",
            "ACTION_IDEMPOTENCY_CONFLICT",
            "ACTION_EXECUTION_IN_PROGRESS",
            "ACTION_PHONE_PREREQUISITE_FAILED",
            "ACTION_PHONE_TRIGGER_NOT_MET",
            "ACTION_PHONE_ROUND_ALREADY_EXISTS",
            "ACTION_FUNCTION_NOT_BOUND",
            "ACTION_FUNCTION_FAILED",
            "ACTION_QUESTION_SET_INVALID",
            "ACTION_WRITE_FAILED",
            "ACTION_DISPATCH_PENDING",
            "ACTION_EXTERNAL_TIMEOUT",
            "ACTION_EXTERNAL_RESPONSE_INVALID",
            "ACTION_PHONE_DISPATCH_FAILED",
            "ACTION_GRAPH_SYNC_FAILED",
            "ACTION_GRAPH_MAPPING_FAILED",
            "ACTION_OUTBOX_STATE_CONFLICT",
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
        ),
    ),
    "record_phone_verification": ActionRuntimeDesign(
        action_name="record_phone_verification",
        trigger="电核员或数字人适配器回传已分配任务的通话结果",
        request_fields=(
            "verification_id",
            "callback_id",
            "verifier_identity",
            "call_time",
            "duration_seconds",
            "conclusion",
            "doubts",
            "recording_file_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + ("verification_id", "record_status"),
        idempotency_fields=("verification_id",),
        steps=(
            step("receive_phone_result", "external_receive", external_system="电核系统"),
            step("authorize_actor", "authorize", "receive_phone_result"),
            step("load_assigned_task", "load", "authorize_actor"),
            step("validate_assignment", "validate", "load_assigned_task"),
            step("validate_phone_result", "validate", "validate_assignment"),
            step("reserve_idempotency", "idempotency", "validate_phone_result"),
            step(
                "update_phone_record",
                "commit",
                "reserve_idempotency",
                effect="update PhoneVerificationRecord",
            ),
            step("verify_persistence", "verify", "update_phone_record"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="同一 verification_id 的终态结果只写一次，并满足电核字段约束",
        consistency_boundary="使用记录版本号或行锁更新；相同载荷重复回调返回原结果，不同载荷冲突",
        compensation=(
            "更新失败时保持原任务状态并允许同一 callback_id 重放",
            "终态记录修改必须走独立更正流程，不允许覆盖式重放",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "phone_assignment_validated",
            "phone_result_recorded",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_PHONE_TASK_NOT_FOUND",
            "ACTION_PHONE_TASK_NOT_ASSIGNED",
            "ACTION_PHONE_RESULT_INVALID",
            "ACTION_TERMINAL_STATE_CONFLICT",
            "ACTION_WRITE_FAILED",
        ),
    ),
    "trigger_manual_review": ActionRuntimeDesign(
        action_name="trigger_manual_review",
        trigger="高风险、反欺诈命中或电核存疑满足至少一条转人工规则",
        request_fields=(
            "application_id",
            "review_reason",
            "source_execution_id",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + (
            "application_id",
            "application_status",
            "review_evidence_reference",
        ),
        idempotency_fields=("application_id", "review_reason"),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_decision_context", "load", "authorize_actor"),
            step("validate_manual_review_rule", "validate", "load_decision_context"),
            step("reserve_idempotency", "idempotency", "validate_manual_review_rule"),
            step(
                "aggregate_decision_support",
                "function",
                "reserve_idempotency",
                function_name="aggregate_decision_support",
            ),
            step(
                "build_evidence_explanation",
                "function",
                "aggregate_decision_support",
                function_name="build_evidence_explanation",
            ),
            step("validate_review_evidence", "validate", "build_evidence_explanation"),
            step(
                "set_manual_review_status",
                "commit",
                "validate_review_evidence",
                effect="update application_status=待人工复核",
            ),
            step(
                "record_review_reasons",
                "commit",
                "set_manual_review_status",
                effect="record review reasons",
            ),
            step("verify_persistence", "verify", "record_review_reasons"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="至少一条转人工规则及证据成立，申请状态与复核原因原子提交",
        consistency_boundary="申请状态、复核原因和证据引用在同一事务提交",
        compensation=(
            "写入失败时回滚状态和原因",
            "不得在缺少证据或 Function 不完整时仅修改申请状态",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "manual_review_rule_matched",
            "manual_review_triggered",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_MANUAL_REVIEW_RULE_NOT_MET",
            "ACTION_FUNCTION_NOT_BOUND",
            "ACTION_EVIDENCE_INCOMPLETE",
            "ACTION_IDEMPOTENCY_CONFLICT",
            "ACTION_WRITE_FAILED",
        ),
    ),
    "record_decision_recommendation": ActionRuntimeDesign(
        action_name="record_decision_recommendation",
        trigger="必要报告完成后生成并记录带证据的决策建议",
        request_fields=(
            "application_id",
            "recommendation_version",
            "approver_id",
            "approval_assertion",
            "request_id",
        ),
        response_fields=COMMON_RESPONSE_FIELDS + (
            "application_id",
            "recommended_decision",
            "decision_authority",
            "evidence_reference",
        ),
        idempotency_fields=("application_id", "recommendation_version"),
        steps=(
            step("authorize_actor", "authorize"),
            step("load_decision_context", "load", "authorize_actor"),
            step("validate_required_reports", "validate", "load_decision_context"),
            step("reserve_idempotency", "idempotency", "validate_required_reports"),
            step(
                "aggregate_decision_support",
                "function",
                "reserve_idempotency",
                function_name="aggregate_decision_support",
            ),
            step(
                "build_evidence_explanation",
                "function",
                "aggregate_decision_support",
                function_name="build_evidence_explanation",
            ),
            step("validate_recommendation", "validate", "build_evidence_explanation"),
            step("confirm_human_approval", "manual_gate", "validate_recommendation"),
            step(
                "record_recommendation",
                "commit",
                "confirm_human_approval",
                effect="update decision_result as recommendation",
            ),
            step(
                "record_evidence_lineage",
                "commit",
                "record_recommendation",
                effect="record evidence and lineage",
            ),
            step("verify_persistence", "verify", "record_evidence_lineage"),
            step("record_audit", "audit", "verify_persistence"),
        ),
        completion_rule="建议、证据和人工确认完整记录；响应明确 decision_authority=recommendation_only",
        consistency_boundary="建议、证据、血缘与人工确认在同一事务提交，不调用最终审批系统",
        compensation=(
            "任一证据或确认缺失时不写入建议",
            "提交失败时回滚建议和证据；不得降级为无证据写入",
        ),
        audit_events=COMMON_AUDIT_EVENTS + (
            "decision_support_aggregated",
            "human_confirmation_recorded",
            "recommendation_recorded",
        ),
        error_codes=(
            "ACTION_ROLE_DENIED",
            "ACTION_REQUIRED_REPORT_MISSING",
            "ACTION_FUNCTION_NOT_BOUND",
            "ACTION_RECOMMENDATION_INVALID",
            "ACTION_HIGH_RISK_PASS_FORBIDDEN",
            "ACTION_APPROVAL_REQUIRED",
            "ACTION_WRITE_FAILED",
        ),
        approval_condition="必须由风险经理或有权信审人员确认；Agent 只能生成候选建议和证据",
    ),
}


def _has_cycle(steps: tuple[ActionStepDesign, ...]) -> bool:
    dependencies = {item.name: set(item.depends_on) for item in steps}
    remaining = set(dependencies)
    while remaining:
        ready = {
            name
            for name in remaining
            if not (dependencies[name] & remaining)
        }
        if not ready:
            return True
        remaining -= ready
    return False


def validate_action_design_catalog(
    proposal: OntologyProposal | None = None,
    designs: Mapping[str, ActionRuntimeDesign] | None = None,
) -> list[str]:
    proposal = proposal or build_personal_credit_baseline()
    designs = designs or ACTION_RUNTIME_DESIGNS
    errors: list[str] = []
    actions = {item.name: item for item in proposal.action_types}
    functions = {item.name: item for item in proposal.functions}
    security = {item.action: item for item in proposal.dynamic_security}

    if set(designs) != set(actions):
        errors.append("Action 运行设计必须完整覆盖 TBox ActionType")

    for key, design in designs.items():
        action = actions.get(key)
        if action is None:
            continue
        if design.action_name != key:
            errors.append(f"Action 设计键与名称不一致：{key}")
        if not design.request_fields or not design.response_fields:
            errors.append(f"Action 请求或响应合同为空：{key}")
        expected_idempotency = tuple((action.idempotency_key or "").split("+"))
        if design.idempotency_fields != expected_idempotency:
            errors.append(f"Action 幂等字段与 TBox 不一致：{key}")
        for field_name in design.idempotency_fields:
            if field_name not in design.request_fields:
                errors.append(f"Action 请求缺少幂等字段：{key}.{field_name}")

        step_names = [item.name for item in design.steps]
        if len(step_names) != len(set(step_names)):
            errors.append(f"Action 步骤名称重复：{key}")
        name_set = set(step_names)
        for item in design.steps:
            if item.kind not in STEP_KINDS:
                errors.append(f"Action 步骤类型非法：{key}.{item.name}")
            if item.timeout_seconds <= 0 or item.max_attempts <= 0:
                errors.append(f"Action 超时或尝试次数非法：{key}.{item.name}")
            missing_dependencies = set(item.depends_on) - name_set
            if missing_dependencies or item.name in item.depends_on:
                errors.append(f"Action 步骤依赖非法：{key}.{item.name}")
            if item.max_attempts > 1 and item.kind not in EXTERNAL_STEP_KINDS:
                errors.append(f"非外部步骤不得自动重试：{key}.{item.name}")
            if item.kind in EXTERNAL_STEP_KINDS and not item.external_system:
                errors.append(f"外部步骤缺少系统：{key}.{item.name}")
            if item.external_system and item.kind not in EXTERNAL_STEP_KINDS:
                errors.append(f"非外部步骤声明了外部系统：{key}.{item.name}")
            if item.effect and (item.kind != "commit" or item.max_attempts != 1):
                errors.append(f"副作用必须是不可自动重试的提交步骤：{key}.{item.name}")
        if _has_cycle(design.steps):
            errors.append(f"Action 步骤 DAG 存在环：{key}")

        declared_functions = set(action.invokes_functions)
        designed_functions = {
            item.function_name for item in design.steps if item.function_name
        }
        if designed_functions != declared_functions:
            errors.append(f"Action Function 覆盖与 TBox 不一致：{key}")
        for function_name in designed_functions:
            function = functions.get(function_name)
            if function is None or function.side_effects:
                errors.append(f"Action 引用了未知或有副作用 Function：{key}.{function_name}")

        declared_effects = set(action.effects)
        designed_effects = {item.effect for item in design.steps if item.effect}
        if designed_effects != declared_effects:
            errors.append(f"Action 副作用覆盖与 TBox 不一致：{key}")

        external_systems = {
            item.external_system for item in design.steps if item.external_system
        }
        expected_external = {action.external_system} if action.external_system else set()
        if external_systems != expected_external:
            errors.append(f"Action 外部系统边界与 TBox 不一致：{key}")

        policy = security.get(key)
        if policy is None or not policy.audit_required or not action.audit_required:
            errors.append(f"Action 缺少审计安全策略：{key}")
        if not any(item.kind == "audit" for item in design.steps):
            errors.append(f"Action 缺少审计步骤：{key}")
        if not {"action_received", "action_succeeded", "action_failed"}.issubset(
            design.audit_events
        ):
            errors.append(f"Action 缺少核心审计事件：{key}")
        if not design.error_codes or any(
            not code.startswith("ACTION_") for code in design.error_codes
        ):
            errors.append(f"Action 错误码合同非法：{key}")
        if len(design.error_codes) != len(set(design.error_codes)):
            errors.append(f"Action 错误码重复：{key}")
        if not design.completion_rule or not design.consistency_boundary or not design.compensation:
            errors.append(f"Action 完成、事务或补偿合同不完整：{key}")
        if policy and policy.approval_required:
            if not design.approval_condition or not any(
                item.kind == "manual_gate" for item in design.steps
            ):
                errors.append(f"Action 缺少必需人工审批门禁：{key}")
        elif any(item.kind == "manual_gate" for item in design.steps):
            errors.append(f"Action 声明了未授权的人工审批门禁：{key}")

    forbidden_actions = {"execute_approval", "approve_application"}
    if forbidden_actions & set(designs):
        errors.append("Action 设计不得包含最终审批动作")
    return errors
