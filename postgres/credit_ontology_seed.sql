-- 信审本体 MVP：合并用户业务字段与《信审本体PRD》v1.1 的静态业务数据结构。
-- NULL 表示尚未采集；一期通过稳定业务主键增量回填，不启用自动更新或自动决策。

CREATE TABLE natural_person (
  id_number varchar(18) PRIMARY KEY CHECK (char_length(id_number)=18),
  name varchar(50) NOT NULL,
  phone varchar(11), gender varchar(2), address varchar(200), marital_status varchar(10),
  monthly_income numeric(12,2), employer varchar(255),
  guarantee_rating varchar(10), guarantee_count integer, guarantee_total numeric(15,2)
);
COMMENT ON TABLE natural_person IS '自然人：借款人、担保人和共借人均通过申请关系上的role表达';
COMMENT ON COLUMN natural_person.id_number IS '身份证号'; COMMENT ON COLUMN natural_person.name IS '姓名';
COMMENT ON COLUMN natural_person.phone IS '手机号'; COMMENT ON COLUMN natural_person.gender IS '性别';
COMMENT ON COLUMN natural_person.address IS '居住地址'; COMMENT ON COLUMN natural_person.marital_status IS '婚姻状况';
COMMENT ON COLUMN natural_person.monthly_income IS '月收入'; COMMENT ON COLUMN natural_person.employer IS '工作单位';
COMMENT ON COLUMN natural_person.guarantee_rating IS '担保能力评级'; COMMENT ON COLUMN natural_person.guarantee_count IS '历史担保次数';
COMMENT ON COLUMN natural_person.guarantee_total IS '历史担保总额';

CREATE TABLE dealer (
  credit_code varchar(32) PRIMARY KEY, dealer_name varchar(100) NOT NULL, location varchar(200)
);
COMMENT ON TABLE dealer IS '经销商'; COMMENT ON COLUMN dealer.credit_code IS '经销商信用代码';
COMMENT ON COLUMN dealer.dealer_name IS '经销商名称'; COMMENT ON COLUMN dealer.location IS '经销商所在地';

CREATE TABLE vehicle (
  vin varchar(17) PRIMARY KEY CHECK (char_length(vin)=17), brand varchar(30), model varchar(50),
  invoice_price numeric(12,2), guide_price numeric(12,2), pledge_status varchar(10)
);
COMMENT ON TABLE vehicle IS '车辆（金融标的）'; COMMENT ON COLUMN vehicle.vin IS 'VIN码';
COMMENT ON COLUMN vehicle.brand IS '品牌'; COMMENT ON COLUMN vehicle.model IS '型号';
COMMENT ON COLUMN vehicle.invoice_price IS '发票价格'; COMMENT ON COLUMN vehicle.guide_price IS '指导价格';
COMMENT ON COLUMN vehicle.pledge_status IS '抵押状态：未抵押/抵押中/已解押';

CREATE TABLE loan_application (
  application_id varchar(32) PRIMARY KEY, submit_date timestamptz, channel varchar(20),
  loan_amount numeric(12,2), loan_term integer, interest_rate numeric(7,4),
  borrower_id varchar(18) NOT NULL REFERENCES natural_person(id_number),
  vehicle_vin varchar(17) REFERENCES vehicle(vin), dealer_credit_code varchar(32) REFERENCES dealer(credit_code),
  guarantor_id varchar(18) REFERENCES natural_person(id_number),
  co_borrower_id varchar(18) REFERENCES natural_person(id_number),
  application_status varchar(20), decision_result varchar(20)
);
COMMENT ON TABLE loan_application IS '贷款申请单（信审业务核心枢纽）';
COMMENT ON COLUMN loan_application.application_id IS '申请编号'; COMMENT ON COLUMN loan_application.submit_date IS '申请日期时间';
COMMENT ON COLUMN loan_application.channel IS '申请渠道'; COMMENT ON COLUMN loan_application.loan_amount IS '贷款本金';
COMMENT ON COLUMN loan_application.loan_term IS '贷款期限（月）'; COMMENT ON COLUMN loan_application.interest_rate IS '贷款利率';
COMMENT ON COLUMN loan_application.borrower_id IS '借款人身份证号'; COMMENT ON COLUMN loan_application.vehicle_vin IS '车辆VIN码';
COMMENT ON COLUMN loan_application.dealer_credit_code IS '经销商信用代码'; COMMENT ON COLUMN loan_application.guarantor_id IS '担保人身份证号';
COMMENT ON COLUMN loan_application.co_borrower_id IS '共借人身份证号'; COMMENT ON COLUMN loan_application.application_status IS '申请状态';
COMMENT ON COLUMN loan_application.decision_result IS '综合决策结果';

CREATE TABLE credit_report (
  report_id varchar(32) PRIMARY KEY, subject_type varchar(10), subject_id_number varchar(18),
  query_time timestamptz, application_channel varchar(20), outstanding_loan_count integer,
  overdue_3y_count integer, total_overdue_count integer, inquiry_count integer, reporting_agency varchar(50)
);
COMMENT ON TABLE credit_report IS '征信报告（可由API或LLM抽取）'; COMMENT ON COLUMN credit_report.report_id IS '征信编号';
COMMENT ON COLUMN credit_report.subject_type IS '记录主体类型：个人/企业'; COMMENT ON COLUMN credit_report.subject_id_number IS '关联主体证件号';
COMMENT ON COLUMN credit_report.query_time IS '申请/查询时间'; COMMENT ON COLUMN credit_report.application_channel IS '申请渠道';
COMMENT ON COLUMN credit_report.outstanding_loan_count IS '未结清贷款数'; COMMENT ON COLUMN credit_report.overdue_3y_count IS '近3年逾期次数';
COMMENT ON COLUMN credit_report.total_overdue_count IS '累计逾期次数'; COMMENT ON COLUMN credit_report.inquiry_count IS '查询次数';
COMMENT ON COLUMN credit_report.reporting_agency IS '征信报送机构';

CREATE TABLE external_debt (
  debt_id varchar(32) PRIMARY KEY, borrower_id varchar(18) NOT NULL REFERENCES natural_person(id_number),
  creditor varchar(50), balance numeric(12,2), monthly_payment numeric(12,2), start_date date,
  overdue_status varchar(20),
  CONSTRAINT external_debt_monthly_payment_nonnegative
    CHECK (monthly_payment IS NULL OR monthly_payment >= 0)
);
COMMENT ON TABLE external_debt IS '外部存量负债'; COMMENT ON COLUMN external_debt.debt_id IS '负债编号';
COMMENT ON COLUMN external_debt.borrower_id IS '借款人身份证号'; COMMENT ON COLUMN external_debt.creditor IS '负债机构';
COMMENT ON COLUMN external_debt.balance IS '负债余额'; COMMENT ON COLUMN external_debt.monthly_payment IS '月还款额';
COMMENT ON COLUMN external_debt.start_date IS '负债起始日';
COMMENT ON COLUMN external_debt.overdue_status IS '逾期状态';

CREATE TABLE risk_assessment_report (
  assessment_id varchar(32) PRIMARY KEY, application_id varchar(32) NOT NULL REFERENCES loan_application(application_id),
  risk_level varchar(10), assessment_time timestamptz, assessment_channel varchar(30),
  pd_value numeric(10,6), threshold numeric(10,6), risk_factors text,
  check_item varchar(100), check_result varchar(20), checker varchar(50)
);
COMMENT ON TABLE risk_assessment_report IS '风险评估报告'; COMMENT ON COLUMN risk_assessment_report.assessment_id IS '评估报告编号';
COMMENT ON COLUMN risk_assessment_report.application_id IS '申请单编号'; COMMENT ON COLUMN risk_assessment_report.risk_level IS '风险等级';
COMMENT ON COLUMN risk_assessment_report.assessment_time IS '评估时间'; COMMENT ON COLUMN risk_assessment_report.assessment_channel IS '评估渠道';
COMMENT ON COLUMN risk_assessment_report.pd_value IS 'PD值'; COMMENT ON COLUMN risk_assessment_report.threshold IS '阈值';
COMMENT ON COLUMN risk_assessment_report.risk_factors IS '风险因子'; COMMENT ON COLUMN risk_assessment_report.check_item IS '核验项名称';
COMMENT ON COLUMN risk_assessment_report.check_result IS '核验结果'; COMMENT ON COLUMN risk_assessment_report.checker IS '核验人';

CREATE TABLE anti_fraud_report (
  fraud_report_id varchar(32) PRIMARY KEY, application_id varchar(32) NOT NULL REFERENCES loan_application(application_id),
  fraud_score numeric(10,4), result varchar(20), hit_details text
);
COMMENT ON TABLE anti_fraud_report IS '反欺诈报告'; COMMENT ON COLUMN anti_fraud_report.fraud_report_id IS '反欺诈报告编号';
COMMENT ON COLUMN anti_fraud_report.application_id IS '申请单编号'; COMMENT ON COLUMN anti_fraud_report.fraud_score IS '欺诈评分';
COMMENT ON COLUMN anti_fraud_report.result IS '结果'; COMMENT ON COLUMN anti_fraud_report.hit_details IS '命中明细';

CREATE TABLE phone_verification_record (
  verification_id varchar(32) PRIMARY KEY, application_id varchar(32) NOT NULL REFERENCES loan_application(application_id),
  verifier_name varchar(50), verification_target varchar(100), call_time timestamptz,
  call_duration_seconds integer, conclusion varchar(100), doubts text, recording_file_id varchar(64)
);
COMMENT ON TABLE phone_verification_record IS '电核记录'; COMMENT ON COLUMN phone_verification_record.verification_id IS '电核记录编号';
COMMENT ON COLUMN phone_verification_record.application_id IS '申请单编号'; COMMENT ON COLUMN phone_verification_record.verifier_name IS '电核员姓名';
COMMENT ON COLUMN phone_verification_record.verification_target IS '电核对象'; COMMENT ON COLUMN phone_verification_record.call_time IS '通话时间';
COMMENT ON COLUMN phone_verification_record.call_duration_seconds IS '通话时长（秒）'; COMMENT ON COLUMN phone_verification_record.conclusion IS '结论';
COMMENT ON COLUMN phone_verification_record.doubts IS '存疑点'; COMMENT ON COLUMN phone_verification_record.recording_file_id IS '录音文件ID';

INSERT INTO natural_person VALUES
('110101198803120011','张伟','13800000001','男','北京市朝阳区','已婚',25000,'华信科技有限公司','良',1,200000),
('110101199007210022','李敏','13800000002','女','上海市浦东新区','未婚',18000,'远航商贸有限公司','中',0,0),
-- 王强与张伟使用同一演示手机号，用于验证 SHARES_CONTACT 派生关系。
('110101198511090033','王强','13800000001','男','北京市海淀区','已婚',NULL,NULL,NULL,NULL,NULL);
INSERT INTO dealer VALUES ('91310000DEALER0001','申城汽车销售有限公司','上海市浦东新区'),('91110000DEALER0002','京华汽车服务有限公司','北京市朝阳区');
INSERT INTO vehicle VALUES
('LHGCM82633A000001','奥迪','A4L',268000,321800,'未抵押'),
('LSVFA49J2E2000002','大众','迈腾',198000,229900,'抵押中'),
('LVGBM51K0HG000003','丰田','凯美瑞',168000,189800,NULL);
INSERT INTO loan_application VALUES
('APP20260801000001','2026-08-01 09:30:00+08','线上H5',200000,36,0.0450,'110101198803120011','LHGCM82633A000001','91310000DEALER0001','110101199007210022',NULL,'评估中','转人工'),
('APP20260803000002','2026-08-03 14:15:00+08','经销商APP',150000,24,0.0480,'110101199007210022','LSVFA49J2E2000002','91110000DEALER0002',NULL,'110101198803120011','待人工复核',NULL),
('APP20260805000003','2026-08-05 10:00:00+08',NULL,120000,36,0.0520,'110101198511090033','LVGBM51K0HG000003',NULL,NULL,NULL,'资料待补充',NULL);
INSERT INTO credit_report VALUES
('CR20260001','个人','110101198803120011','2026-08-01 09:35:00+08','线上H5',2,1,2,5,'央行征信'),
('CR20260002','个人','110101199007210022','2026-08-03 14:20:00+08','经销商APP',0,0,0,2,'百行征信'),
('CR20260003','个人','110101198511090033',NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO external_debt
  (debt_id,borrower_id,creditor,balance,monthly_payment,start_date,overdue_status)
VALUES
('DEBT20260001','110101198803120011','某商业银行',80000,3200,'2024-05-01','正常'),
('DEBT20260002','110101199007210022','某消费金融公司',12000,1200,'2025-11-15','逾期30天');
INSERT INTO risk_assessment_report VALUES
('RA20260001','APP20260801000001','中','2026-08-01 09:40:00+08','自动决策',0.031500,0.050000,'近期征信查询次数偏高','收入真实性核验','待人工复核',NULL),
('RA20260002','APP20260803000002','低','2026-08-03 14:25:00+08','自动决策',0.012000,0.050000,'无高风险因子','身份一致性核验','通过','系统'),
('RA20260003','APP20260805000003',NULL,NULL,NULL,NULL,0.050000,NULL,NULL,NULL,NULL);
INSERT INTO anti_fraud_report VALUES
('FR20260001','APP20260801000001',18.5,'通过','未命中高风险规则'),
('FR20260002','APP20260803000002',63,'人工复核','设备关联多个申请人'),
('FR20260003','APP20260805000003',NULL,NULL,NULL);
INSERT INTO phone_verification_record VALUES
('PV20260001','APP20260801000001','陈审核','张伟','2026-08-01 10:10:00+08',328,'信息基本一致','收入需补充银行流水','REC20260001'),
('PV20260002','APP20260803000002','刘审核','李敏','2026-08-03 15:05:00+08',215,'通过',NULL,'REC20260002'),
('PV20260003','APP20260805000003',NULL,'王强',NULL,NULL,NULL,NULL,NULL);
