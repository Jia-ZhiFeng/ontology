-- 个人信审本体 1.1.0：为 DTI 增加真实的月还款额输入。
-- 本迁移可重复执行，不删除表、不清空业务数据，也不使用负债余额推算月供。

BEGIN;

ALTER TABLE public.external_debt
  ADD COLUMN IF NOT EXISTS monthly_payment numeric(12,2);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'external_debt_monthly_payment_nonnegative'
      AND conrelid = 'public.external_debt'::regclass
  ) THEN
    ALTER TABLE public.external_debt
      ADD CONSTRAINT external_debt_monthly_payment_nonnegative
      CHECK (monthly_payment IS NULL OR monthly_payment >= 0);
  END IF;
END
$$;

COMMENT ON COLUMN public.external_debt.monthly_payment IS '月还款额';

-- 仅为仓库自带的两条演示负债补齐明确样例；真实业务记录保持原值或 NULL。
UPDATE public.external_debt
SET monthly_payment = CASE debt_id
  WHEN 'DEBT20260001' THEN 3200.00
  WHEN 'DEBT20260002' THEN 1200.00
END
WHERE debt_id IN ('DEBT20260001', 'DEBT20260002')
  AND monthly_payment IS NULL;

COMMIT;
