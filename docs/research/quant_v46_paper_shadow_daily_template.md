# V46 影子模拟盘日报模板与导出适配器

状态：模板和适配器已生成，当前不代表生产批准。

## 产物

- 模板：`/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v46_paper_shadow_daily_template.json`
- 示例：`/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v46_paper_shadow_daily_example.json`
- 字段映射：`/Users/jacklondon/Documents/study/lianghua/data/backtest_results/quant_v46_paper_shadow_adapter_mapping.json`

## 安全约束

- 模板带有 `template_only=true`，V45 会拒绝写入台账。
- 示例带有 `example_only=true`，V45 会拒绝写入台账。
- 默认不会生成可入账日报；只有显式提供 `--source-export-json` 才会转换真实导出文件。
- 转换出的日报仍必须经过 V45、V42 和 V44 逐级校验。
- 任何令牌、接口密钥、密码或私钥都不能出现在日报或映射文件中。

## 使用流程

- 从真实模拟盘服务导出标准化源 JSON。
- 运行本脚本并传入 `--source-export-json`，生成 V45 单日日报。
- 运行 V45 导入脚本，把单日日报写入台账并刷新 V42/V44。
- 连续至少 90 个自然日且至少 60 个日报交易日后，再评估是否解除 V42 日报数量阻塞。
