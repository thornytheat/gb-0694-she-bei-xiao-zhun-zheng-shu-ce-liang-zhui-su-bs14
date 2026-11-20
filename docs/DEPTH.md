# 深度登记 设备校准证书与测量追溯

- 规模：中型
- 使用者与工作流：检验机构质量员，合并仪器使用与校准证书，裁决测量是否在有效可追溯链内。
- 核心规则：证书有效期与量程双约束；参考标准也须在校准时有效，证书撤销向下游传播并检测环。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 证书接收 | 运行证书接收并复核证据 | `POST /api/m1`；`/work/1` | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py` |
| M2 设备别名 | 运行设备别名并复核证据 | `POST /api/m2`；`/work/2` | `app/modules/m2_identity/rules.py` | `tests/test_m2.py` |
| M3 有效区间 | 运行有效区间并复核证据 | `POST /api/m3`；`/work/3` | `app/modules/m3_validity/rules.py` | `tests/test_m3.py` |
| M4 量程匹配 | 运行量程匹配并复核证据 | `POST /api/m4`；`/work/4` | `app/modules/m4_ranges/rules.py` | `tests/test_m4.py` |
| M5 溯源链路 | 运行溯源链路并复核证据 | `POST /api/m5`；`/work/5` | `app/modules/m5_trace/rules.py` | `tests/test_m5.py` |
| M6 测量裁决 | 运行测量裁决并复核证据 | `POST /api/m6`；`/work/6` | `app/modules/m6_verdict/rules.py` | `tests/test_m6.py` |
| M7 撤销复核 | 运行撤销复核并复核证据 | `POST /api/m7`；`/work/7` | `app/modules/m7_revocation/rules.py` | `tests/test_m7.py` |
| M8 供应商结算 | 生成可复核结算批，处理晚到证据与封账 | `POST /api/settlements`；`/settlement` | `app/settlement.py`、`app/settle_store.py` | `tests/test_settlement.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码证书 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py::test_legacy_chinese` |
| TRAP-07 | 空值测量 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_identity/rules.py` | `tests/test_m2.py::test_missing_isolated` |
| TRAP-08 | 设备编码保形 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_identity/rules.py` | `tests/test_m2.py::test_device_code_preserved` |
| TRAP-14 | 定义冲突 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_identity/rules.py` | `tests/test_m2.py::test_conflict_certificate_flag` |
| TRAP-20 | 溯源依赖环 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m5_trace/rules.py` | `tests/test_m5.py::test_reference_cycle` |
| TRAP-22 | 设备别名 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_identity/rules.py` | `tests/test_m2.py::test_alias_order_independent` |
| TRAP-23 | 失效日边界 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m3_validity/rules.py` | `tests/test_m3.py::test_expiry_endpoint` |
| TRAP-25 | 多证书重叠 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m3_validity/rules.py` | `tests/test_m3.py::test_overlapping_certificates` |
| TRAP-26 | 晚到撤销/改证比账单晚到 | 封账付掉已失效项或等齐拖过结算日 | 封账前并入本批重算；封账后留痕并转入下期扣回/补付 | `app/settlement.py` | `tests/test_settlement.py::test_late_evidence_after_close_goes_to_next_period` |
| TRAP-27 | 同一证书换设备别名或代记 | 拆成多行重复结算，或沿用旧归属 | 按证书归并明细，改证后按新归属重算有效区间与溯源 | `app/settlement.py` | `tests/test_settlement.py::test_alias_amendment_recomputes_ownership_and_window` |
| TRAP-28 | 重跑结算批 | 重复生成应付与付款 | 身份只含供应商与结算期，台账按凭证去重 | `app/settle_store.py` | `tests/test_settlement.py::test_rerun_same_batch_does_not_double_book` |

## 对账规则

输入记录 = 采信记录 + 隔离记录 + 相同行重复。重复表头与空行单独记数。业务合计按阶段中的守恒或差值列复核。运行清单有原文SHA256、规则版本与耗时；相同规则版本下同文重复提交复用批次。

结算批按证书复核：证书数 = 应付 + 暂扣 + 拒付；应付净额 = 本期应付 + 上期调整；测量总数 = 本批测量 + 期外测量。晚到证据的去向只有“本批”“无金额影响”“转某期扣回/补付”三种，付款凭证按批与类型去重。

## 已知不覆盖

部署为本地单用户工具，不连接外部上游系统。CSV单文件5MB及一万条业务记录上限；不推断未提供的未知数据。
