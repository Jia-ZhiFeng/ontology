from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..ontology.baseline import build_personal_credit_baseline
from ..ontology.data_binding import OntologyDataBinding, OntologyDataBindingError
from .business_functions import build_personal_credit_readonly_registry
from .function_registry import FunctionExecutionContext, FunctionRegistry


class FunctionDataAdapterError(RuntimeError):
    """只读 Function 上下文无法从 PostgreSQL/NebulaGraph 完整构造。"""


@dataclass(frozen=True)
class ApplicationFunctionSnapshot:
    borrower_id: str
    application: Mapping[str, Any]
    borrower: Mapping[str, Any]
    vehicle: Mapping[str, Any]
    credit_report: Mapping[str, Any] | None
    external_debts: tuple[Mapping[str, Any], ...]
    anti_fraud_report: Mapping[str, Any] | None
    risk_assessment: Mapping[str, Any] | None
    phone_verification: Mapping[str, Any] | None
    snapshot_id: str
    snapshot_started_at: str
    data_binding_version: str
    data_binding_digest: str


def _time_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise FunctionDataAdapterError("PostgreSQL 未返回有效快照时间")
    return text


class PersonalCreditFunctionDataAdapter:
    """为 9 个只读 Function 构造最小化 PostgreSQL 和 NebulaGraph 输入。"""

    def __init__(
        self,
        postgres_service,
        nebula_factory: Callable[[], Any],
        *,
        proposal=None,
    ):
        if not hasattr(postgres_service, "conn"):
            raise TypeError("postgres_service 必须提供 conn()")
        if not callable(nebula_factory):
            raise TypeError("nebula_factory 必须可调用")
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        try:
            self.bindings = OntologyDataBinding(
                proposal or build_personal_credit_baseline()
            )
            self.bindings.require({
                "LoanApplication": (
                    "application_id", "loan_amount", "application_status",
                    "decision_result", "borrower_id", "vehicle_vin",
                ),
                "NaturalPerson": ("id_number", "monthly_income"),
                "Vehicle": ("vin", "invoice_price", "guide_price"),
                "CreditReport": (
                    "report_id", "subject_id_number", "query_time",
                    "outstanding_loan_count", "overdue_3y_count",
                    "total_overdue_count", "inquiry_count",
                ),
                "ExternalDebt": (
                    "debt_id", "borrower_id", "monthly_payment", "overdue_status",
                ),
                "AntiFraudReport": (
                    "fraud_report_id", "application_id", "fraud_score", "result",
                    "hit_details",
                ),
                "RiskAssessmentReport": (
                    "assessment_id", "application_id", "risk_level", "pd_value",
                    "risk_factors", "check_result", "assessment_time",
                ),
                "PhoneVerificationRecord": (
                    "verification_id", "application_id", "conclusion", "doubts",
                    "call_time",
                ),
            })
        except OntologyDataBindingError as error:
            raise FunctionDataAdapterError(
                f"本体数据绑定不可执行：{error}"
            ) from error

    @staticmethod
    def _application_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FunctionDataAdapterError("application_id 不能为空")
        identifier = value.strip()
        if len(identifier) > 32:
            raise FunctionDataAdapterError("application_id 长度不能超过 32")
        return identifier

    def load_postgres_snapshot(self, application_id: str) -> ApplicationFunctionSnapshot:
        identifier = self._application_id(application_id)
        binding = self.bindings
        table = binding.table_sql
        column = binding.column_sql
        select = binding.select_sql
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("""
                SELECT txid_current_snapshot()::text AS snapshot_id,
                       transaction_timestamp() AS snapshot_started_at
            """)
            snapshot = cursor.fetchone()
            if not snapshot:
                raise FunctionDataAdapterError("PostgreSQL 未返回一致性快照信息")

            cursor.execute(f"""
                SELECT {select('LoanApplication','application_id',alias='application')},
                       {select('LoanApplication','loan_amount',alias='application')},
                       {select('LoanApplication','application_status',alias='application')},
                       {select('LoanApplication','decision_result',alias='application')},
                       {select('LoanApplication','borrower_id',alias='application')},
                       {select('LoanApplication','vehicle_vin',alias='application')},
                       {select('NaturalPerson','monthly_income',alias='person')},
                       {select('Vehicle','invoice_price',alias='vehicle')},
                       {select('Vehicle','guide_price',alias='vehicle')}
                FROM {table('LoanApplication')} AS "application"
                LEFT JOIN {table('NaturalPerson')} AS "person"
                  ON {column('NaturalPerson','id_number',alias='person')}
                   = {column('LoanApplication','borrower_id',alias='application')}
                LEFT JOIN {table('Vehicle')} AS "vehicle"
                  ON {column('Vehicle','vin',alias='vehicle')}
                   = {column('LoanApplication','vehicle_vin',alias='application')}
                WHERE {column('LoanApplication','application_id',alias='application')} = %s
            """, (identifier,))
            base = cursor.fetchone()
            if not base:
                raise FunctionDataAdapterError(f"贷款申请不存在：{identifier}")
            if not base.get("borrower_id"):
                raise FunctionDataAdapterError(f"贷款申请缺少借款人关联：{identifier}")
            if not base.get("vehicle_vin"):
                raise FunctionDataAdapterError(f"贷款申请缺少车辆关联：{identifier}")

            cursor.execute(f"""
                SELECT {select('CreditReport','report_id',alias='report')},
                       {select('CreditReport','outstanding_loan_count',alias='report')},
                       {select('CreditReport','overdue_3y_count',alias='report')},
                       {select('CreditReport','total_overdue_count',alias='report')},
                       {select('CreditReport','inquiry_count',alias='report')}
                FROM {table('CreditReport')} AS "report"
                WHERE {column('CreditReport','subject_id_number',alias='report')} = %s
                ORDER BY {column('CreditReport','query_time',alias='report')} DESC NULLS LAST,
                         {column('CreditReport','report_id',alias='report')} DESC
                LIMIT 1
            """, (base["borrower_id"],))
            credit_report = cursor.fetchone()

            cursor.execute(f"""
                SELECT {select('ExternalDebt','debt_id',alias='debt')},
                       {select('ExternalDebt','monthly_payment',alias='debt')},
                       {select('ExternalDebt','overdue_status',alias='debt')}
                FROM {table('ExternalDebt')} AS "debt"
                WHERE {column('ExternalDebt','borrower_id',alias='debt')} = %s
                ORDER BY {column('ExternalDebt','debt_id',alias='debt')}
            """, (base["borrower_id"],))
            external_debts = cursor.fetchall()

            cursor.execute(f"""
                SELECT {select('AntiFraudReport','fraud_report_id',alias='fraud')},
                       {select('AntiFraudReport','fraud_score',alias='fraud')},
                       {select('AntiFraudReport','result',alias='fraud')},
                       {select('AntiFraudReport','hit_details',alias='fraud')}
                FROM {table('AntiFraudReport')} AS "fraud"
                WHERE {column('AntiFraudReport','application_id',alias='fraud')} = %s
                ORDER BY {column('AntiFraudReport','fraud_report_id',alias='fraud')} DESC
                LIMIT 1
            """, (identifier,))
            anti_fraud_report = cursor.fetchone()

            cursor.execute(f"""
                SELECT {select('RiskAssessmentReport','assessment_id',alias='assessment')},
                       {select('RiskAssessmentReport','risk_level',alias='assessment')},
                       {select('RiskAssessmentReport','pd_value',alias='assessment')},
                       {select('RiskAssessmentReport','risk_factors',alias='assessment')},
                       {select('RiskAssessmentReport','check_result',alias='assessment')}
                FROM {table('RiskAssessmentReport')} AS "assessment"
                WHERE {column('RiskAssessmentReport','application_id',alias='assessment')} = %s
                ORDER BY {column('RiskAssessmentReport','assessment_time',alias='assessment')} DESC NULLS LAST,
                         {column('RiskAssessmentReport','assessment_id',alias='assessment')} DESC
                LIMIT 1
            """, (identifier,))
            risk_assessment = cursor.fetchone()

            cursor.execute(f"""
                SELECT {select('PhoneVerificationRecord','verification_id',alias='phone')},
                       {select('PhoneVerificationRecord','conclusion',alias='phone')},
                       {select('PhoneVerificationRecord','doubts',alias='phone')}
                FROM {table('PhoneVerificationRecord')} AS "phone"
                WHERE {column('PhoneVerificationRecord','application_id',alias='phone')} = %s
                ORDER BY {column('PhoneVerificationRecord','call_time',alias='phone')} DESC NULLS LAST,
                         {column('PhoneVerificationRecord','verification_id',alias='phone')} DESC
                LIMIT 1
            """, (identifier,))
            phone_verification = cursor.fetchone()

        application = {
            "application_id": base["application_id"],
            "loan_amount": base.get("loan_amount"),
            "application_status": base.get("application_status"),
            "decision_result": base.get("decision_result"),
        }
        borrower = {"monthly_income": base.get("monthly_income")}
        vehicle = {
            "invoice_price": base.get("invoice_price"),
            "guide_price": base.get("guide_price"),
        }
        return ApplicationFunctionSnapshot(
            borrower_id=str(base["borrower_id"]),
            application=application,
            borrower=borrower,
            vehicle=vehicle,
            credit_report=credit_report,
            external_debts=tuple(external_debts),
            anti_fraud_report=anti_fraud_report,
            risk_assessment=risk_assessment,
            phone_verification=phone_verification,
            snapshot_id=str(snapshot["snapshot_id"]),
            snapshot_started_at=_time_text(snapshot["snapshot_started_at"]),
            data_binding_version=binding.ontology_version,
            data_binding_digest=binding.binding_digest,
        )

    def resolve_postgres_snapshot(
        self,
        application_id: str,
        snapshot: ApplicationFunctionSnapshot | None = None,
    ) -> ApplicationFunctionSnapshot:
        """Reuse a caller-owned turn snapshot, or load one when absent."""
        identifier = self._application_id(application_id)
        if snapshot is None:
            return self.load_postgres_snapshot(identifier)
        snapshot_application_id = str(
            snapshot.application.get("application_id") or ""
        ).strip()
        if snapshot_application_id != identifier:
            raise FunctionDataAdapterError("共享快照与当前贷款申请不匹配")
        if snapshot.data_binding_version != self.bindings.ontology_version:
            raise FunctionDataAdapterError("共享快照与当前本体数据绑定版本不匹配")
        return snapshot

    def load_association_graph(self, borrower_id: str, *, hops: int = 2, limit: int = 300) -> dict[str, Any]:
        service = self.nebula_factory()
        try:
            resolved = service.resolve_node_id(str(borrower_id))
            graph = service.query_graph(resolved, hops, limit, 0, "entity")
        finally:
            service.close()
        page = graph.get("page") or {}
        return {
            "borrower_node_id": resolved,
            "graph_nodes": graph.get("nodes") or [],
            "graph_edges": graph.get("edges") or [],
            "hops": hops,
            "query_coverage": {
                "candidate_count": int(page.get("edge_returned", len(graph.get("edges") or []))),
                "evaluated_count": int(page.get("edge_returned", len(graph.get("edges") or []))),
                "truncated": bool(page.get("result_truncated", False)),
            },
            "graph_snapshot_time": datetime.now(timezone.utc).isoformat(),
        }

    def execute_all(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
    ) -> dict[str, Any]:
        snapshot = self.load_postgres_snapshot(application_id)
        # borrower_id 只用于图查询，不进入最终 Function 聚合结果。
        association_input = self.load_association_graph(snapshot.borrower_id)
        active_registry = registry or build_personal_credit_readonly_registry()
        context = FunctionExecutionContext(
            ontology_version=ontology_version,
            action_execution_id=action_execution_id,
            actor=actor,
            data_snapshot_time=snapshot.snapshot_started_at,
            metadata={
                "postgres_snapshot_id": snapshot.snapshot_id,
                "postgres_snapshot_mode": "repeatable_read_read_only",
                "data_binding_version": snapshot.data_binding_version,
                "data_binding_digest": snapshot.data_binding_digest,
                "graph_snapshot_time": association_input["graph_snapshot_time"],
                "cross_store_atomic": False,
            },
        )
        outputs: dict[str, dict[str, Any]] = {}
        outputs["calculate_dti"] = active_registry.execute("calculate_dti", {
            "monthly_income": snapshot.borrower.get("monthly_income"),
            "monthly_debt_payments": [item.get("monthly_payment") for item in snapshot.external_debts],
        }, context)
        outputs["calculate_ltv"] = active_registry.execute("calculate_ltv", {
            "loan_amount": snapshot.application.get("loan_amount"),
            "invoice_price": snapshot.vehicle.get("invoice_price"),
            "guide_price": snapshot.vehicle.get("guide_price"),
        }, context)
        outputs["evaluate_credit_risk"] = active_registry.execute("evaluate_credit_risk", {
            "credit_report": snapshot.credit_report,
            "external_debts": list(snapshot.external_debts),
        }, context)
        outputs["detect_association_risk"] = active_registry.execute(
            "detect_association_risk", association_input, context
        )
        association_result = outputs["detect_association_risk"]["result"]
        outputs["evaluate_fraud_risk"] = active_registry.execute("evaluate_fraud_risk", {
            "anti_fraud_report": snapshot.anti_fraud_report,
            "association_paths": association_result["association_paths"],
            "query_coverage": association_result["query_coverage"],
        }, context)
        outputs["assess_application_risk"] = active_registry.execute("assess_application_risk", {
            "application_id": snapshot.application["application_id"],
            "dti_result": outputs["calculate_dti"],
            "ltv_result": outputs["calculate_ltv"],
            "credit_risk_result": outputs["evaluate_credit_risk"],
            "fraud_risk_result": outputs["evaluate_fraud_risk"],
        }, context)
        assessment = {
            "assessment_id": (snapshot.risk_assessment or {}).get("assessment_id")
                             or f"computed:{snapshot.application['application_id']}",
            **outputs["assess_application_risk"]["result"],
        }
        outputs["generate_phone_questions"] = active_registry.execute("generate_phone_questions", {
            "risk_factors": assessment["risk_factors"],
            "fraud_hit_details": (snapshot.anti_fraud_report or {}).get("hit_details"),
            "association_risk_signals": association_result["association_risk_signals"],
        }, context)
        outputs["aggregate_decision_support"] = active_registry.execute("aggregate_decision_support", {
            "application": snapshot.application,
            "risk_assessment": assessment,
            "anti_fraud_report": snapshot.anti_fraud_report,
            "phone_verification": snapshot.phone_verification,
        }, context)
        outputs["build_evidence_explanation"] = active_registry.execute("build_evidence_explanation", {
            "application": snapshot.application,
            "risk_assessment": assessment,
            "anti_fraud_report": snapshot.anti_fraud_report,
            "phone_verification": snapshot.phone_verification,
            "decision_support": outputs["aggregate_decision_support"],
        }, context)
        return {
            "application_id": snapshot.application["application_id"],
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": snapshot.snapshot_id,
            "postgres_snapshot_time": snapshot.snapshot_started_at,
            "data_binding_version": snapshot.data_binding_version,
            "data_binding_digest": snapshot.data_binding_digest,
            "graph_snapshot_time": association_input["graph_snapshot_time"],
            "cross_store_atomic": False,
            "execution_order": list(outputs),
            "functions": outputs,
        }

    def execute_risk_assessment(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
        snapshot: ApplicationFunctionSnapshot | None = None,
    ) -> dict[str, Any]:
        """Execute only the five Functions declared by generate_risk_assessment."""
        snapshot = self.resolve_postgres_snapshot(application_id, snapshot)
        active_registry = registry or build_personal_credit_readonly_registry()
        context = FunctionExecutionContext(
            ontology_version=ontology_version,
            action_execution_id=action_execution_id,
            actor=actor,
            data_snapshot_time=snapshot.snapshot_started_at,
            metadata={
                "postgres_snapshot_id": snapshot.snapshot_id,
                "postgres_snapshot_mode": "repeatable_read_read_only",
                "data_binding_version": snapshot.data_binding_version,
                "data_binding_digest": snapshot.data_binding_digest,
                "cross_store_atomic": True,
            },
        )
        outputs: dict[str, dict[str, Any]] = {}
        outputs["calculate_dti"] = active_registry.execute("calculate_dti", {
            "monthly_income": snapshot.borrower.get("monthly_income"),
            "monthly_debt_payments": [
                item.get("monthly_payment") for item in snapshot.external_debts
            ],
        }, context)
        outputs["calculate_ltv"] = active_registry.execute("calculate_ltv", {
            "loan_amount": snapshot.application.get("loan_amount"),
            "invoice_price": snapshot.vehicle.get("invoice_price"),
            "guide_price": snapshot.vehicle.get("guide_price"),
        }, context)
        outputs["evaluate_credit_risk"] = active_registry.execute(
            "evaluate_credit_risk",
            {
                "credit_report": snapshot.credit_report,
                "external_debts": list(snapshot.external_debts),
            },
            context,
        )
        outputs["evaluate_fraud_risk"] = active_registry.execute(
            "evaluate_fraud_risk",
            {
                "anti_fraud_report": snapshot.anti_fraud_report,
                "association_paths": [],
                "query_coverage": {
                    "candidate_count": 0,
                    "evaluated_count": 0,
                    "truncated": False,
                },
            },
            context,
        )
        outputs["assess_application_risk"] = active_registry.execute(
            "assess_application_risk",
            {
                "application_id": snapshot.application["application_id"],
                "dti_result": outputs["calculate_dti"],
                "ltv_result": outputs["calculate_ltv"],
                "credit_risk_result": outputs["evaluate_credit_risk"],
                "fraud_risk_result": outputs["evaluate_fraud_risk"],
            },
            context,
        )
        return {
            "application_id": snapshot.application["application_id"],
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": snapshot.snapshot_id,
            "postgres_snapshot_time": snapshot.snapshot_started_at,
            "data_binding_version": snapshot.data_binding_version,
            "data_binding_digest": snapshot.data_binding_digest,
            "graph_snapshot_time": None,
            "cross_store_atomic": True,
            "execution_order": list(outputs),
            "functions": outputs,
            "association_query": {
                "used": False,
                "reason": "detect_association_risk is not declared by this Action",
            },
        }

    def execute_association_risk(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
        hops: int = 2,
        limit: int = 300,
    ) -> dict[str, Any]:
        """Execute only detect_association_risk for request_fraud_report."""
        snapshot = self.load_postgres_snapshot(application_id)
        association_input = self.load_association_graph(
            snapshot.borrower_id, hops=hops, limit=limit
        )
        active_registry = registry or build_personal_credit_readonly_registry()
        context = FunctionExecutionContext(
            ontology_version=ontology_version,
            action_execution_id=action_execution_id,
            actor=actor,
            data_snapshot_time=snapshot.snapshot_started_at,
            metadata={
                "postgres_snapshot_id": snapshot.snapshot_id,
                "postgres_snapshot_mode": "repeatable_read_read_only",
                "data_binding_version": snapshot.data_binding_version,
                "data_binding_digest": snapshot.data_binding_digest,
                "graph_snapshot_time": association_input["graph_snapshot_time"],
                "cross_store_atomic": False,
            },
        )
        output = active_registry.execute(
            "detect_association_risk", association_input, context
        )
        return {
            "application_id": snapshot.application["application_id"],
            "borrower_id": snapshot.borrower_id,
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": snapshot.snapshot_id,
            "postgres_snapshot_time": snapshot.snapshot_started_at,
            "data_binding_version": snapshot.data_binding_version,
            "data_binding_digest": snapshot.data_binding_digest,
            "graph_snapshot_time": association_input["graph_snapshot_time"],
            "cross_store_atomic": False,
            "execution_order": ["detect_association_risk"],
            "functions": {"detect_association_risk": output},
        }

    @staticmethod
    def _phone_risk_factors(raw: Any) -> list[dict[str, Any]]:
        value = raw
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                codes = []
                for token, code in (
                    ("负债", "DTI_STORED_CONTEXT"),
                    ("收入", "DTI_STORED_CONTEXT"),
                    ("车辆", "LTV_STORED_CONTEXT"),
                    ("贷款价值", "LTV_STORED_CONTEXT"),
                    ("逾期", "OVERDUE_STORED_CONTEXT"),
                    ("查询", "INQUIRY_STORED_CONTEXT"),
                    ("未结清", "OUTSTANDING_STORED_CONTEXT"),
                    ("欺诈", "FRAUD_STORED_CONTEXT"),
                    ("关联", "ASSOCIATION_STORED_CONTEXT"),
                ):
                    if token in text and code not in codes:
                        codes.append(code)
                return [{"factor": code} for code in (codes or ["DTI_BASE_CONTEXT"])]
        if isinstance(value, Mapping):
            value = value.get("factors")
        if not isinstance(value, (list, tuple)):
            raise FunctionDataAdapterError("风险评估 risk_factors 不是可解析列表")
        factors = []
        for item in value:
            if isinstance(item, Mapping) and item.get("factor"):
                factors.append(dict(item))
            elif isinstance(item, str) and item.strip():
                factors.append({"factor": item.strip()})
            else:
                raise FunctionDataAdapterError("风险评估包含无效风险因子")
        return factors

    def execute_phone_questions(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
        snapshot: ApplicationFunctionSnapshot | None = None,
    ) -> dict[str, Any]:
        """Execute only generate_phone_questions for the phone-task Action."""
        snapshot = self.resolve_postgres_snapshot(application_id, snapshot)
        risk = snapshot.risk_assessment
        fraud = snapshot.anti_fraud_report
        if not risk or not all(
            risk.get(field)
            for field in ("assessment_id", "risk_level", "risk_factors", "check_result")
        ):
            raise FunctionDataAdapterError("缺少完整风险评估报告")
        if (
            not fraud
            or fraud.get("fraud_score") is None
            or not fraud.get("result")
            or not fraud.get("hit_details")
        ):
            raise FunctionDataAdapterError("缺少完整反欺诈报告")
        active_registry = registry or build_personal_credit_readonly_registry()
        context = FunctionExecutionContext(
            ontology_version=ontology_version,
            action_execution_id=action_execution_id,
            actor=actor,
            data_snapshot_time=snapshot.snapshot_started_at,
            metadata={
                "postgres_snapshot_id": snapshot.snapshot_id,
                "postgres_snapshot_mode": "repeatable_read_read_only",
                "data_binding_version": snapshot.data_binding_version,
                "data_binding_digest": snapshot.data_binding_digest,
                "cross_store_atomic": True,
            },
        )
        output = active_registry.execute(
            "generate_phone_questions",
            {
                "risk_factors": self._phone_risk_factors(risk.get("risk_factors")),
                "fraud_hit_details": fraud.get("hit_details"),
                "association_risk_signals": [],
            },
            context,
        )
        return {
            "application_id": snapshot.application["application_id"],
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": snapshot.snapshot_id,
            "postgres_snapshot_time": snapshot.snapshot_started_at,
            "data_binding_version": snapshot.data_binding_version,
            "data_binding_digest": snapshot.data_binding_digest,
            "cross_store_atomic": True,
            "execution_order": ["generate_phone_questions"],
            "functions": {"generate_phone_questions": output},
        }

    def execute_decision_support(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
        snapshot: ApplicationFunctionSnapshot | None = None,
    ) -> dict[str, Any]:
        """Execute only the two Functions declared by manual-review Actions."""
        snapshot = self.resolve_postgres_snapshot(application_id, snapshot)
        risk = snapshot.risk_assessment
        fraud = snapshot.anti_fraud_report
        phone = snapshot.phone_verification
        if (
            not risk
            or not risk.get("assessment_id")
            or not risk.get("risk_level")
            or risk.get("pd_value") is None
        ):
            raise FunctionDataAdapterError("缺少完整风险评估证据")
        if not fraud or not fraud.get("fraud_report_id") or not fraud.get("result"):
            raise FunctionDataAdapterError("缺少完整反欺诈证据")
        if not phone or not phone.get("verification_id"):
            raise FunctionDataAdapterError("缺少电核记录证据")
        active_registry = registry or build_personal_credit_readonly_registry()
        context = FunctionExecutionContext(
            ontology_version=ontology_version,
            action_execution_id=action_execution_id,
            actor=actor,
            data_snapshot_time=snapshot.snapshot_started_at,
            metadata={
                "postgres_snapshot_id": snapshot.snapshot_id,
                "postgres_snapshot_mode": "repeatable_read_read_only",
                "data_binding_version": snapshot.data_binding_version,
                "data_binding_digest": snapshot.data_binding_digest,
                "cross_store_atomic": True,
            },
        )
        outputs: dict[str, dict[str, Any]] = {}
        outputs["aggregate_decision_support"] = active_registry.execute(
            "aggregate_decision_support",
            {
                "application": snapshot.application,
                "risk_assessment": risk,
                "anti_fraud_report": fraud,
                "phone_verification": phone,
            },
            context,
        )
        outputs["build_evidence_explanation"] = active_registry.execute(
            "build_evidence_explanation",
            {
                "application": snapshot.application,
                "risk_assessment": risk,
                "anti_fraud_report": fraud,
                "phone_verification": phone,
                "decision_support": outputs["aggregate_decision_support"],
            },
            context,
        )
        return {
            "application_id": snapshot.application["application_id"],
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": snapshot.snapshot_id,
            "postgres_snapshot_time": snapshot.snapshot_started_at,
            "data_binding_version": snapshot.data_binding_version,
            "data_binding_digest": snapshot.data_binding_digest,
            "graph_snapshot_time": None,
            "cross_store_atomic": True,
            "execution_order": list(outputs),
            "functions": outputs,
        }
