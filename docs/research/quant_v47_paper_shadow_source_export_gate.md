# V47 影子模拟盘源导出校验器

状态：源导出校验已生成，当前不代表生产批准。

## 结论

- `source_export_valid=false`
- `production_ready=false`
- 日期：无
- 阻塞项数量：1

## 指标

- `order_count`：0
- `fill_count`：0
- `position_count`：0
- `traded_symbol_count`：0
- `position_symbol_count`：0
- `audit_event_count`：0
- `audit_hash`：

## 阻塞项

- 缺少 --source-export-json

## 输出

- 未生成单日日报

## 说明

- V47 校验订单、成交、持仓、行情和审计导出的一致性。
- V47 通过只表示可以进入 V46/V45 流水线，不代表 V42/V44 或生产准入通过。
- 任何敏感字段、实盘下单权限、订单成交不一致或持仓对账不一致都会阻塞。
