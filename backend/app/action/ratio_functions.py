from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .function_registry import (
    FunctionAlreadyBoundError,
    FunctionContractError,
    FunctionExecutionContext,
    FunctionOutput,
    FunctionRegistry,
    build_personal_credit_function_registry,
)


DTI_IMPLEMENTATION_VERSION = "dti-v1.0.0"
LTV_IMPLEMENTATION_VERSION = "ltv-v1.0.0"
_RATIO_SCALE = Decimal("0.000001")


def _decimal_value(
    value: Any,
    field_name: str,
    *,
    allow_zero: bool,
) -> Decimal:
    if value is None:
        raise FunctionContractError(f"缺少必需输入：{field_name}")
    if isinstance(value, bool):
        raise FunctionContractError(f"输入必须是有限数字：{field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FunctionContractError(f"输入必须是有限数字：{field_name}") from exc
    if not parsed.is_finite():
        raise FunctionContractError(f"输入必须是有限数字：{field_name}")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        operator = "大于等于 0" if allow_zero else "大于 0"
        raise FunctionContractError(f"输入必须{operator}：{field_name}")
    return parsed


def _ratio(numerator: Decimal, denominator: Decimal) -> float:
    return float((numerator / denominator).quantize(_RATIO_SCALE, rounding=ROUND_HALF_UP))


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_dti(
    inputs: dict[str, Any],
    context: FunctionExecutionContext,
) -> FunctionOutput:
    """按月负债支出 / 月收入计算 DTI，不使用负债余额代替月供。"""

    monthly_income = _decimal_value(
        inputs.get("monthly_income"),
        "monthly_income",
        allow_zero=False,
    )
    payments = inputs.get("monthly_debt_payments")
    if not isinstance(payments, (list, tuple)):
        raise FunctionContractError("monthly_debt_payments 必须是月负债支出列表")

    parsed_payments = [
        _decimal_value(value, f"monthly_debt_payments[{index}]", allow_zero=True)
        for index, value in enumerate(payments)
    ]
    total_monthly_payment = sum(parsed_payments, Decimal("0"))
    dti = _ratio(total_monthly_payment, monthly_income)
    return FunctionOutput(
        result={
            "dti": dti,
            "total_monthly_payment": _money(total_monthly_payment),
            "monthly_income": _money(monthly_income),
            "debt_item_count": len(parsed_payments),
            "calculation_policy": "monthly_debt_payments/gross_monthly_income",
        },
        evidence=(
            {"source": "NaturalPerson.monthly_income"},
            {
                "source": "ExternalDebt.monthly_payment",
                "aggregation": "sum",
                "item_count": len(parsed_payments),
            },
        ),
        confidence=1.0,
        complete=True,
        data_snapshot_time=context.data_snapshot_time,
    )


def calculate_ltv(
    inputs: dict[str, Any],
    context: FunctionExecutionContext,
) -> FunctionOutput:
    """按贷款本金 / 较低有效车辆价格计算 LTV。"""

    loan_amount = _decimal_value(
        inputs.get("loan_amount"),
        "loan_amount",
        allow_zero=False,
    )
    candidates: list[tuple[str, Decimal]] = []
    for field_name in ("invoice_price", "guide_price"):
        value = inputs.get(field_name)
        if value is not None:
            candidates.append(
                (
                    field_name,
                    _decimal_value(value, field_name, allow_zero=False),
                )
            )
    if not candidates:
        raise FunctionContractError("invoice_price 和 guide_price 至少需要一个有效值")

    # min() 在价格相同时保留候选顺序，因此同价时优先使用发票价格。
    value_source, vehicle_value = min(candidates, key=lambda item: item[1])
    ltv = _ratio(loan_amount, vehicle_value)
    return FunctionOutput(
        result={
            "ltv": ltv,
            "loan_amount": _money(loan_amount),
            "vehicle_value": _money(vehicle_value),
            "value_source": value_source,
            "calculation_policy": "lower_of_available_invoice_and_guide_price",
        },
        evidence=tuple(
            [{"source": "LoanApplication.loan_amount"}]
            + [
                {"source": f"Vehicle.{field_name}"}
                for field_name, _value in candidates
            ]
        ),
        confidence=1.0,
        complete=True,
        data_snapshot_time=context.data_snapshot_time,
    )


def bind_personal_credit_ratio_functions(
    registry: FunctionRegistry,
    *,
    replace: bool = False,
) -> FunctionRegistry:
    """将两个确定性比率实现显式绑定到给定注册中心。"""

    names = ("calculate_dti", "calculate_ltv")
    for name in names:
        registry.describe(name)
    collisions = sorted(set(names) & set(registry.bound_names()))
    if collisions and not replace:
        raise FunctionAlreadyBoundError(
            f"Function 已绑定：{','.join(collisions)}"
        )
    registry.bind(
        "calculate_dti",
        calculate_dti,
        DTI_IMPLEMENTATION_VERSION,
        replace=replace,
    )
    registry.bind(
        "calculate_ltv",
        calculate_ltv,
        LTV_IMPLEMENTATION_VERSION,
        replace=replace,
    )
    return registry


def build_personal_credit_ratio_registry() -> FunctionRegistry:
    registry = build_personal_credit_function_registry()
    return bind_personal_credit_ratio_functions(registry)
