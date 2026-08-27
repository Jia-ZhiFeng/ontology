BEGIN;

-- 修复早期两组纯合成手测申请中不属于本体目录的候选枚举值。
-- 仅命中固定 UAT 编号及旧值，避免覆盖后续人工验收产生的合法变化。
UPDATE public.loan_application
SET channel = '经销商APP'
WHERE application_id IN ('UATMAN202608270001', 'UATMAN202608270002')
  AND channel = '手工联调';

UPDATE public.loan_application
SET application_status = '评估中'
WHERE application_id IN ('UATMAN202608270001', 'UATMAN202608270002')
  AND application_status = '待征信查询';

COMMIT;
