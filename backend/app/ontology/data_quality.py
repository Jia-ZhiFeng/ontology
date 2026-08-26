from collections import defaultdict
from datetime import date,datetime
from decimal import Decimal

from .models import OntologyProposal


VALIDATOR_VERSION="ontology-catalog-data-quality-1.0"
MAX_VIOLATION_EXAMPLES=50


def _present(value)->bool:
    return value is not None and (not isinstance(value,str) or bool(value.strip()))


def _any_present(row:dict,*fields:str)->bool:
    return any(_present(row.get(field)) for field in fields)


class ConditionContext:
    def __init__(self,profile:dict):
        self.tables=profile.get("samples",{})
        self.applications=self.tables.get("loan_application",[])
        self.borrower_applications=defaultdict(list)
        for row in self.applications:
            if _present(row.get("borrower_id")):
                self.borrower_applications[str(row["borrower_id"])].append(row)

    def applies(self,expression:str,table:str,row:dict,column:str)->bool|None:
        if expression=="申请提交前":
            return bool(self.borrower_applications.get(str(row.get("id_number"))))
        if expression=="还款能力评估前":
            borrower_id=row.get("id_number") or row.get("borrower_id")
            return any(
                app.get("application_status") in {"评估中","待人工复核","已完成"} or _present(app.get("decision_result"))
                for app in self.borrower_applications.get(str(borrower_id),[])
            )
        if expression=="自然人作为担保人时":
            identifier=str(row.get("id_number"))
            return any(str(app.get("guarantor_id"))==identifier for app in self.applications if _present(app.get("guarantor_id")))
        if expression=="申请渠道为经销商APP或线下门店时":
            return row.get("channel") in {"经销商APP","线下门店"}
        if expression=="申请存在担保人时":
            return _present(row.get("guarantor_id"))
        if expression=="申请存在共借人时":
            return _present(row.get("co_borrower_id"))
        if expression=="综合决策完成后":
            return row.get("application_status")=="已完成"
        if expression=="征信查询完成后":
            return _any_present(row,"query_time","application_channel","reporting_agency")
        if expression=="征信报告解析完成后":
            return _any_present(row,"outstanding_loan_count","overdue_3y_count","total_overdue_count","inquiry_count")
        if expression=="评估完成后":
            return _any_present(row,"risk_level","assessment_time","assessment_channel","risk_factors")
        if expression=="模型评估完成后":
            # threshold 是规则配置，可在模型执行前预置；PD 出现才表示模型已经产出结果。
            return _present(row.get("pd_value"))
        if expression=="核验完成后":
            return _any_present(row,"check_item","check_result")
        if expression=="人工复核时":
            # “待人工复核”仍是队列状态，尚不能要求核验人已经回填。
            return row.get("assessment_channel")=="人工评估"
        if expression=="反欺诈查询完成后":
            return _any_present(row,"fraud_score","result")
        if expression=="命中反欺诈规则时":
            score=row.get("fraud_score")
            return row.get("result") in {"拒绝","人工复核"} or isinstance(score,(int,float,Decimal)) and score>70
        if expression=="电核任务创建后":
            return True
        if expression=="电核完成后":
            return _any_present(row,"verifier_name","call_time","call_duration_seconds","conclusion")
        if expression=="电核结论为存疑时":
            return row.get("conclusion")=="存疑"
        return None

    @staticmethod
    def base_required_applies(table:str,row:dict,column:str,business_key:bool)->bool:
        """Apply unconditional required fields only after the object's base stage is complete.

        Business keys and lifecycle status are structural and are always checked. A loan
        application in 资料待补充 is deliberately allowed to contain pre-created NULL
        fields; once it enters evaluation, all unconditional application fields are due.
        """
        if business_key or column=="application_status":
            return True
        if table=="loan_application":
            return row.get("application_status") in {"评估中","待人工复核","已完成"}
        return True


def _type_valid(value,expected:str)->bool:
    if expected=="string": return isinstance(value,str)
    if expected=="integer": return isinstance(value,int) and not isinstance(value,bool)
    if expected=="number": return isinstance(value,(int,float,Decimal)) and not isinstance(value,bool)
    if expected=="datetime": return isinstance(value,datetime)
    if expected=="date": return isinstance(value,date) and not isinstance(value,datetime)
    if expected=="boolean": return isinstance(value,bool)
    return False


def _record_identifier(table:str,row:dict,business_keys:dict[str,list[str]],row_index:int):
    for column in business_keys.get(table,[]):
        if _present(row.get(column)): return str(row[column])
    return f"row:{row_index}"


def validate_catalog_data_quality(proposal:OntologyProposal,profile:dict)->dict:
    """Validate proposal property metadata against one consistent PostgreSQL snapshot."""
    context=ConditionContext(profile); samples=profile.get("samples",{})
    business_keys=defaultdict(list)
    for prop in proposal.properties:
        if prop.business_key and prop.source_table and prop.source_column:
            business_keys[prop.source_table].append(prop.source_column)

    unknown_conditions=sorted({
        prop.required_when
        for prop in proposal.properties
        if prop.required_when and context.applies(prop.required_when,prop.source_table or "",{},prop.source_column or "") is None
    })
    results=[]; total_checked=0
    for prop in proposal.properties:
        table=prop.source_table; column=prop.source_column
        if not table or not column:
            continue
        rows=samples.get(table,[]); violations=[]; violation_count=0; applicable_required=0

        def violate(row_index,row,rule,message,details=None):
            nonlocal violation_count
            violation_count+=1
            if len(violations)<MAX_VIOLATION_EXAMPLES:
                item={
                    "rule":rule,"table":table,"column":column,
                    "record_id":_record_identifier(table,row,business_keys,row_index),
                    "row_index":row_index,"message":message,
                }
                if details: item["details"]=details
                violations.append(item)

        seen={}
        for row_index,row in enumerate(rows):
            total_checked+=1; value=row.get(column)
            condition=context.applies(prop.required_when,table,row,column) if prop.required_when else None
            required_now=(
                bool(prop.required and not prop.required_when)
                and context.base_required_applies(table,row,column,prop.business_key)
            ) or condition is True
            if required_now:
                applicable_required+=1
                if not _present(value):
                    violate(row_index,row,"required","字段在当前业务阶段必填",{"required_when":prop.required_when})
            if value is None:
                continue
            if isinstance(value,str) and not value.strip():
                violate(row_index,row,"empty_string","空字符串不是有效值，应使用 NULL 表示尚未采集")
                continue
            if not _type_valid(value,prop.range):
                violate(row_index,row,"type",f"字段类型不符合目录 range={prop.range}",{"actual_type":type(value).__name__})
                continue
            if prop.enum_values and value not in prop.enum_values:
                violate(row_index,row,"enum","字段值不在目录枚举范围内",{"allowed_values":prop.enum_values})
            if prop.unit=="0-1" and isinstance(value,(int,float,Decimal)) and not 0<=value<=1:
                violate(row_index,row,"unit_range","字段值不在目录单位定义的 0-1 范围内")
            if prop.business_key and _present(value):
                if value in seen:
                    violate(row_index,row,"business_key_unique","业务主键重复",{"first_row_index":seen[value]})
                else:
                    seen[value]=row_index

        results.append({
            "property":f"{prop.domain}.{prop.name}","label":prop.label,
            "table":table,"column":column,"range":prop.range,
            "required":prop.required,"required_when":prop.required_when,
            "business_key":prop.business_key,"enum_values":prop.enum_values,
            "checked_rows":len(rows),"required_applicable_rows":applicable_required,
            "violation_count":violation_count,"violations":violations,
            "examples_truncated":violation_count>len(violations),
            "status":"passed" if violation_count==0 else "failed",
        })

    truncated_tables=profile.get("truncated_tables",[])
    violation_count=sum(item["violation_count"] for item in results)
    failed_properties=sum(item["status"]=="failed" for item in results)
    valid=bool(profile.get("snapshot_consistent")) and not truncated_tables and not unknown_conditions and violation_count==0
    return {
        "valid":valid,
        "status":"passed" if valid else "failed",
        "validator":VALIDATOR_VERSION,
        "proposal_version":proposal.version,
        "catalog_version":proposal.metadata.get("attribute_catalog_version"),
        "snapshot":{
            "mode":profile.get("snapshot_mode"),
            "consistent":profile.get("snapshot_consistent",False),
            "id":profile.get("snapshot_id"),
            "started_at":profile.get("snapshot_started_at"),
            "dataset_complete":not truncated_tables,
            "truncated_tables":truncated_tables,
        },
        "condition_coverage":{
            "valid":not unknown_conditions,
            "known_condition_count":len({prop.required_when for prop in proposal.properties if prop.required_when})-len(unknown_conditions),
            "unknown_conditions":unknown_conditions,
        },
        "summary":{
            "property_rule_count":len(results),
            "checked_value_count":total_checked,
            "failed_property_count":failed_properties,
            "violation_count":violation_count,
            "row_count":sum(len(rows) for rows in samples.values()),
        },
        "results":results,
    }
