# eval report — query

## run metadata

| key           | value                                                                                 |
|---------------|---------------------------------------------------------------------------------------|
| timestamp_utc | 2026-05-18T15-46-00Z                                                                  |
| model         | gemma4:e2b                                                                            |
| temperature   | 0.2                                                                                   |
| num_ctx       | 8192                                                                                  |
| git_rev       | 154a20dbfa8e891e5f23cb4adefcddf499d7a266                                              |
| tag           | after-fix3                                                                            |
| runs          | 2                                                                                     |
| stage         | query                                                                                 |
| gold_path     | C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\gold\query\acme-overview.yml |

## metrics

| metric            | mean ± std    |
|-------------------|---------------|
| route_accuracy    | 0.875 ± 0.000 |
| sql_success_rate  | 1.000 ± 0.000 |
| row_contains_rate | 0.000 ± 0.000 |
| doc_slug_rate     | 0.000 ± 0.000 |


gold: qa=8, fixture=C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\fixtures\acme.sqlite


## failures (top 20)

### 1. q: アクメの主力製品は？

```
{
  "label": "q: アクメの主力製品は？",
  "expected_route": "text2sql",
  "predicted_route": "fallback",
  "row_contains_rate": null,
  "doc_slug_rate": null,
  "missed_contains": [],
  "missed_slugs": [],
  "sql": null,
  "sql_ok": null,
  "n_rows": 0,
  "error": "text2sql 失敗: no such column: T2.canonical_name"
}
```
