-- 个人信审手动 Action 联调数据集（合成数据，不包含真实客户信息）。
--
-- 使用方式：以 UATMAN202608270001 作为“已有申请续办”场景，从征信查询开始
-- 手动推进后续 Action。征信、反欺诈、风险评估、电核、人工复核与决策建议
-- 不在此处预置，必须由对应 Action 产生。
--
-- 脚本可重复执行；已存在的测试记录保持原状，不覆盖人工测试产生的变化。

BEGIN;

INSERT INTO public.natural_person (
  id_number, name, phone, gender, address, marital_status,
  monthly_income, employer, guarantee_rating, guarantee_count, guarantee_total
) VALUES
  (
    '110101199203150047', '手测借款人丁', '13900009101', '男',
    '上海市手工验收区合成数据路8号', '已婚', 23500.00,
    '手工测试数据科技有限公司', NULL, NULL, NULL
  ),
  (
    '110101198706220058', '手测担保人戊', '13900009102', '女',
    '上海市手工验收区合成数据路9号', '已婚', 28000.00,
    '手工测试担保服务有限公司', '良', 2, 180000.00
  ),
  (
    '110101199411080069', '手测共借人己', '13900009103', '女',
    '上海市手工验收区合成数据路10号', '未婚', 15000.00,
    '手工测试运营有限公司', NULL, NULL, NULL
  )
ON CONFLICT (id_number) DO NOTHING;

INSERT INTO public.dealer (credit_code, dealer_name, location)
VALUES (
  'UATDEALER202608270001',
  '手工验收汽车经销商',
  '上海市手工验收区'
)
ON CONFLICT (credit_code) DO NOTHING;

INSERT INTO public.vehicle (
  vin, brand, model, invoice_price, guide_price, pledge_status
) VALUES (
  'LHGCM8260MNL00001',
  '手测品牌',
  'Action联调车型',
  210000.00,
  235000.00,
  '未抵押'
)
ON CONFLICT (vin) DO NOTHING;

INSERT INTO public.loan_application (
  application_id, submit_date, channel, loan_amount, loan_term, interest_rate,
  borrower_id, vehicle_vin, dealer_credit_code, guarantor_id, co_borrower_id,
  application_status, decision_result
) VALUES (
  'UATMAN202608270001',
  '2026-08-27 10:30:00+08',
  '手工联调',
  145000.00,
  36,
  0.0468,
  '110101199203150047',
  'LHGCM8260MNL00001',
  'UATDEALER202608270001',
  '110101198706220058',
  '110101199411080069',
  '待征信查询',
  NULL
)
ON CONFLICT (application_id) DO NOTHING;

-- 修正脚本首次落库时可能引用的早期候选 VIN；其他人工修改不受影响。
UPDATE public.loan_application
SET vehicle_vin = 'LHGCM8260MNL00001'
WHERE application_id = 'UATMAN202608270001'
  AND vehicle_vin = 'LHGCM8260UAT00003';

INSERT INTO public.external_debt (
  debt_id, borrower_id, creditor, balance, monthly_payment, start_date,
  overdue_status
) VALUES (
  'UATDEBT202608270001',
  '110101199203150047',
  '合成测试消费金融',
  36000.00,
  1800.00,
  '2025-03-01',
  '正常'
)
ON CONFLICT (debt_id) DO NOTHING;

COMMIT;
