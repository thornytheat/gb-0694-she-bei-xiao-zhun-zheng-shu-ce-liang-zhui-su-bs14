# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## 供应商结算批

结算批是质量与采购共用的对账视图，按“供应商 + 结算期”唯一确定；同一结算期重跑返回同一批，不新增批次、不重复记账。

- `GET /api/settlements`：结算批列表。
- `POST /api/settlements`：建批或重算草稿。JSON 字段 `supplier`（必填）、`period`（YYYY-MM，省略时取基础批次最新测量月份）、`run_id`（基础测量批次，省略取最新）、`unit_price`（每条可结算测量的单价，默认100）、`currency`（默认CNY）、`evidence`（晚到证据数组）。返回 `snapshot.lines`（按证书明细）、`snapshot.totals`、`evidence`、`carries`、`ledger`、`reused`。
- `GET /api/settlements/<id>`：读取结算批全貌。
- `POST /api/settlements/<id>/evidence`：登记晚到撤销或改证，JSON 为 `{cert, kind, received_on, valid_from, valid_to, parent, device, alias, value, note}`，或包在 `evidence` 字段中。同一证据重复提交不重复生效。
- `POST /api/settlements/<id>/review`：`revision` 与 `decision`（通过或退回），乐观锁，陈旧提交409；封账批返回409。
- `POST /api/settlements/<id>/post`：按 `revision` 封账并入账。封账后重跑返回既有批（`reused: true`），台账不追加。
- `GET /api/settlements/<id>/evidence.csv`：结算明细、晚到证据去向、上期调整与付款凭证的单一导出。

结算错误码：`SUPPLIER`、`PERIOD`、`CURRENCY`、`PRICE`、`EVIDENCE`、`CERT`、`RUN`、`REVIEW`、`CLOSED`、`VERSION`。页面入口 `/settlement`，可用 `?id=` 或 `?supplier=&period=` 打开。
