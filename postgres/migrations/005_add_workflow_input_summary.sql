BEGIN;

ALTER TABLE mvp_control.personal_credit_workflow_steps
ADD COLUMN IF NOT EXISTS input_summary jsonb;

COMMENT ON COLUMN mvp_control.personal_credit_workflow_steps.input_summary IS
    '服务端校验和补齐后、经敏感字段脱敏的 Action 有效输入摘要；原始请求不落库';

COMMIT;
