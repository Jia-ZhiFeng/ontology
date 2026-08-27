-- 个人信审手动 Action 联调数据集 002（合成数据，不包含真实客户信息）。
--
-- 使用 UATMAN202608270002 作为“已有申请续办”场景，从征信查询开始推进。
-- 本组设置了较高月供压力和逾期外部负债，用于观察后续风险 Action 的真实输出。
-- 征信、反欺诈、风险评估、电核、人工复核与决策建议不在此处预置。
--
-- 脚本可重复执行；已存在的测试记录保持原状。

BEGIN;

INSERT INTO public.natural_person (
  id_number, name, phone, gender, address, marital_status,
  monthly_income, employer, guarantee_rating, guarantee_count, guarantee_total
) VALUES
  (
    '110101199305160071', '手测借款人庚', '13900009111', '男',
    '广州市手工验收区合成数据路21号', '已婚', 12500.00,
    '手工联调贸易有限公司', NULL, NULL, NULL
  ),
  (
    '110101198809230082', '手测担保人辛', '13900009112', '女',
    '广州市手工验收区合成数据路22号', '已婚', 19000.00,
    '手工联调担保服务有限公司', '中', 3, 260000.00
  ),
  (
    '110101199612120093', '手测共借人壬', '13900009113', '女',
    '广州市手工验收区合成数据路23号', '未婚', 9800.00,
    '手工联调零售有限公司', NULL, NULL, NULL
  )
ON CONFLICT (id_number) DO NOTHING;

INSERT INTO public.dealer (credit_code, dealer_name, location)
VALUES (
  'UATDEALER202608270002',
  '手工联调华南汽车经销商',
  '广州市手工验收区'
)
ON CONFLICT (credit_code) DO NOTHING;

INSERT INTO public.vehicle (
  vin, brand, model, invoice_price, guide_price, pledge_status
) VALUES (
  'LHGCM8260MNL00002',
  '联调品牌',
  '高负债场景车型',
  175000.00,
  188000.00,
  '未抵押'
)
ON CONFLICT (vin) DO NOTHING;

INSERT INTO public.loan_application (
  application_id, submit_date, channel, loan_amount, loan_term, interest_rate,
  borrower_id, vehicle_vin, dealer_credit_code, guarantor_id, co_borrower_id,
  application_status, decision_result
) VALUES (
  'UATMAN202608270002',
  '2026-08-27 11:15:00+08',
  '手工联调',
  140000.00,
  48,
  0.0558,
  '110101199305160071',
  'LHGCM8260MNL00002',
  'UATDEALER202608270002',
  '110101198809230082',
  '110101199612120093',
  '待征信查询',
  NULL
)
ON CONFLICT (application_id) DO NOTHING;

INSERT INTO public.external_debt (
  debt_id, borrower_id, creditor, balance, monthly_payment, start_date,
  overdue_status
) VALUES (
  'UATDEBT202608270002',
  '110101199305160071',
  '合成测试消费金融华南分部',
  68000.00,
  3600.00,
  '2024-10-15',
  '逾期30天'
)
ON CONFLICT (debt_id) DO NOTHING;

COMMIT;
