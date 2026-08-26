-- 个人信审八 Action 业务验收数据区。
--
-- 这里只保存合成场景、执行观测和闭环验证结果。贷款申请、征信、反欺诈、
-- 风险评估、电核、人工复核与决策建议等业务记录必须由对应 Action 产生，
-- 不允许通过本脚本提前伪造。

CREATE SCHEMA IF NOT EXISTS mvp_uat;

CREATE TABLE IF NOT EXISTS mvp_uat.action_chain_scenarios (
  scenario_id varchar(64) PRIMARY KEY,
  dataset_version varchar(32) NOT NULL,
  application_id varchar(32) NOT NULL UNIQUE,
  workflow_request_id varchar(128) NOT NULL UNIQUE,
  workflow_id varchar(36),
  synthetic boolean NOT NULL DEFAULT true CHECK (synthetic),
  state varchar(24) NOT NULL DEFAULT 'PREPARED'
    CHECK (state IN (
      'PREPARED', 'WORKFLOW_READY', 'RUNNING', 'PASSED', 'FAILED', 'BLOCKED'
    )),
  definition jsonb NOT NULL,
  final_verification jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(definition) = 'object'),
  CHECK (definition->>'scenario_id' = scenario_id),
  CHECK (definition->>'application_id' = application_id),
  CHECK ((definition->>'synthetic')::boolean = true)
);

COMMENT ON SCHEMA mvp_uat IS
  '仅用于本地/验收环境的合成测试场景；不得承载真实客户数据';
COMMENT ON TABLE mvp_uat.action_chain_scenarios IS
  '八 Action 闭环场景定义；业务结果由 Action 生成而非预置';

CREATE TABLE IF NOT EXISTS mvp_uat.action_chain_step_results (
  scenario_id varchar(64) NOT NULL
    REFERENCES mvp_uat.action_chain_scenarios(scenario_id) ON DELETE RESTRICT,
  action_name varchar(64) NOT NULL,
  position integer NOT NULL CHECK (position BETWEEN 1 AND 8),
  workflow_id varchar(36) NOT NULL,
  execution_id varchar(36),
  workflow_step_status varchar(32) NOT NULL,
  action_status varchar(32),
  verified boolean NOT NULL DEFAULT false,
  verification_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
  result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  observed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scenario_id, action_name),
  UNIQUE (scenario_id, position),
  CHECK (jsonb_typeof(verification_notes) = 'array'),
  CHECK (jsonb_typeof(result_summary) = 'object')
);

COMMENT ON TABLE mvp_uat.action_chain_step_results IS
  '验收脚本逐步记录的脱敏 Action 观测，不替代 Action 自身审计记录';

CREATE INDEX IF NOT EXISTS action_chain_step_results_execution_idx
  ON mvp_uat.action_chain_step_results(execution_id)
  WHERE execution_id IS NOT NULL;
