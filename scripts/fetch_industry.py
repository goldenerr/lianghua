#!/usr/bin/env python3.9
"""Fetch stock industry classifications using baostock."""
import baostock as bs
import pandas as pd

lg = bs.login()
print(f"Baostock login: {lg.error_code} {lg.error_msg}")

rs = bs.query_stock_industry()
print(f"query_stock_industry: {rs.error_code} {rs.error_msg}")

industries = []
while (rs.error_code == '0') & rs.next():
    row = rs.get_row_data()
    industries.append(row)

df = pd.DataFrame(industries, columns=rs.fields)
print(f"Total: {len(df)} stocks")
print(df.head(10))

out_path = "/home/hermes/.hermes/projects/lianghua/data/industry_fixed.parquet"
df.to_parquet(out_path)
print(f"Saved to {out_path}")
print(f"Top industries:\n{df['industry'].value_counts().head(15)}")

bs.logout()
