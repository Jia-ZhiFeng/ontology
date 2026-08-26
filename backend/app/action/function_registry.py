from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping

from ..ontology.models import Function


class FunctionRegistryError(RuntimeError):
    """Function 注册、绑定或执行失败的受控基类。"""


class UnknownFunctionError(FunctionRegistryError):
    pass


class FunctionAlreadyBoundError(FunctionRegistryError):
    pass


class FunctionNotBoundError(FunctionRegistryError):
    pass


class FunctionContractError(FunctionRegistryError):
    pass


class FunctionExecutionError(FunctionRegistryError):
    pass


@dataclass(frozen=True)
class FunctionExecutionContext:
    ontology_version: str
    action_execution_id: str | None = None
    actor: str | None = None
    data_snapshot_time: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.ontology_version, str) or not self.ontology_version.strip():
            raise FunctionContractError("ontology_version 不能为空")
        if not isinstance(self.metadata, Mapping):
            raise FunctionContractError("context metadata 必须是映射")


@dataclass(frozen=True)
class FunctionOutput:
    result: Mapping[str, Any]
    evidence: tuple[Any, ...] = ()
    confidence: float | None = None
    complete: bool = True
    warnings: tuple[str, ...] = ()
    data_snapshot_time: str | None = None


FunctionImplementation = Callable[[dict[str, Any], FunctionExecutionContext], FunctionOutput]


@dataclass(frozen=True)
class _FunctionBinding:
    implementation: FunctionImplementation
    implementation_version: str


class FunctionRegistry:
    """保存只读 Function 契约，并以显式绑定方式执行本地实现。"""

    def __init__(self, definitions: Iterable[Function]):
        self._lock = RLock()
        self._definitions: dict[str, Function] = {}
        self._bindings: dict[str, _FunctionBinding] = {}
        for definition in definitions:
            if not definition.name.strip():
                raise FunctionContractError("Function name 不能为空")
            if definition.name in self._definitions:
                raise FunctionContractError(f"Function 契约重复：{definition.name}")
            if definition.side_effects:
                raise FunctionContractError(f"Function 不允许声明副作用：{definition.name}")
            if definition.implementation_status != "not_bound":
                raise FunctionContractError(
                    f"本体 Function 必须保持 not_bound：{definition.name}"
                )
            self._definitions[definition.name] = definition.model_copy(deep=True)

    def __len__(self) -> int:
        return len(self._definitions)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def bound_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._bindings))

    def describe(self, name: str) -> dict[str, Any]:
        definition = self._definition(name)
        with self._lock:
            binding = self._bindings.get(name)
        return {
            "contract": definition.model_dump(mode="json"),
            "bound": binding is not None,
            "implementation_version": (
                binding.implementation_version if binding is not None else None
            ),
        }

    def bind(
        self,
        name: str,
        implementation: FunctionImplementation,
        implementation_version: str,
        *,
        replace: bool = False,
    ) -> None:
        self._definition(name)
        if not callable(implementation):
            raise FunctionContractError(f"Function 实现不可调用：{name}")
        if not isinstance(implementation_version, str):
            raise FunctionContractError(f"Function 实现版本必须是字符串：{name}")
        version = implementation_version.strip()
        if not version:
            raise FunctionContractError(f"Function 实现版本不能为空：{name}")
        with self._lock:
            if name in self._bindings and not replace:
                raise FunctionAlreadyBoundError(f"Function 已绑定：{name}")
            self._bindings[name] = _FunctionBinding(implementation, version)

    def unbind(self, name: str) -> bool:
        self._definition(name)
        with self._lock:
            return self._bindings.pop(name, None) is not None

    def execute(
        self,
        name: str,
        inputs: Mapping[str, Any],
        context: FunctionExecutionContext,
    ) -> dict[str, Any]:
        definition = self._definition(name)
        if not isinstance(inputs, Mapping):
            raise FunctionContractError(f"Function inputs 必须是映射：{name}")
        if not isinstance(context, FunctionExecutionContext):
            raise FunctionContractError(f"Function context 类型无效：{name}")
        with self._lock:
            binding = self._bindings.get(name)
        if binding is None:
            raise FunctionNotBoundError(f"Function 尚未绑定：{name}")

        started = perf_counter()
        try:
            output = binding.implementation(deepcopy(dict(inputs)), context)
        except FunctionRegistryError:
            raise
        except Exception as exc:
            raise FunctionExecutionError(f"Function 执行失败：{name}") from exc
        duration_ms = max(0, round((perf_counter() - started) * 1000))
        return self._validated_envelope(
            definition,
            output,
            binding.implementation_version,
            context,
            duration_ms,
        )

    def _definition(self, name: str) -> Function:
        definition = self._definitions.get(name)
        if definition is None:
            raise UnknownFunctionError(f"Function 契约不存在：{name}")
        return definition

    @staticmethod
    def _validated_envelope(
        definition: Function,
        output: FunctionOutput,
        implementation_version: str,
        context: FunctionExecutionContext,
        duration_ms: int,
    ) -> dict[str, Any]:
        if not isinstance(output, FunctionOutput):
            raise FunctionContractError(
                f"Function 必须返回 FunctionOutput：{definition.name}"
            )
        if not isinstance(output.result, Mapping):
            raise FunctionContractError(f"Function result 必须是映射：{definition.name}")
        missing = sorted(set(definition.returns) - set(output.result))
        if missing:
            raise FunctionContractError(
                f"Function 缺少声明的返回字段：{definition.name}.{','.join(missing)}"
            )
        if output.confidence is not None:
            if isinstance(output.confidence, bool) or not isinstance(
                output.confidence, (int, float)
            ):
                raise FunctionContractError(
                    f"Function confidence 必须是 0 到 1 的数字：{definition.name}"
                )
            if not 0 <= float(output.confidence) <= 1:
                raise FunctionContractError(
                    f"Function confidence 超出 0 到 1：{definition.name}"
                )
        if not isinstance(output.complete, bool):
            raise FunctionContractError(f"Function complete 必须是布尔值：{definition.name}")
        if not isinstance(output.evidence, (list, tuple)):
            raise FunctionContractError(f"Function evidence 必须是列表：{definition.name}")
        if not isinstance(output.warnings, (list, tuple)) or any(
            not isinstance(item, str) for item in output.warnings
        ):
            raise FunctionContractError(f"Function warnings 必须是字符串：{definition.name}")
        if output.data_snapshot_time is not None and not isinstance(
            output.data_snapshot_time, str
        ):
            raise FunctionContractError(
                f"Function data_snapshot_time 必须是字符串：{definition.name}"
            )

        return {
            "function_name": definition.name,
            "status": "success",
            "result": deepcopy(dict(output.result)),
            "evidence": deepcopy(list(output.evidence)),
            "confidence": (
                float(output.confidence) if output.confidence is not None else None
            ),
            "implementation_version": implementation_version,
            "data_snapshot_time": output.data_snapshot_time
            or context.data_snapshot_time,
            "complete": output.complete,
            "warnings": list(output.warnings),
            "duration_ms": duration_ms,
        }


def build_personal_credit_function_registry() -> FunctionRegistry:
    from ..ontology.baseline import build_personal_credit_baseline

    return FunctionRegistry(build_personal_credit_baseline().functions)
