# eval report — query

## run metadata

| key           | value                                                                             |
|---------------|-----------------------------------------------------------------------------------|
| timestamp_utc | 2026-05-18T15-39-42Z                                                              |
| model         | gemma4:e2b                                                                        |
| temperature   | 0.2                                                                               |
| num_ctx       | 8192                                                                              |
| git_rev       | 154a20dbfa8e891e5f23cb4adefcddf499d7a266                                          |
| tag           | after-fix3                                                                        |
| runs          | 2                                                                                 |
| stage         | query                                                                             |
| gold_path     | C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\gold\query\i127-9285.yml |

## metrics

| metric            | mean ± std    |
|-------------------|---------------|
| route_accuracy    | 1.000 ± 0.000 |
| sql_success_rate  | 1.000 ± 0.000 |
| row_contains_rate | 0.600 ± 0.000 |
| doc_slug_rate     | 0.000 ± 0.000 |


gold: qa=5, fixture=C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\fixtures\i127-9285.sqlite


## failures (top 20)

### 1. q: Bobの無料トライアル版って？

```
{
  "label": "q: Bobの無料トライアル版って？",
  "expected_route": "text2sql",
  "predicted_route": "text2sql",
  "row_contains_rate": 0.0,
  "doc_slug_rate": null,
  "missed_contains": [
    {
      "column": "*",
      "value": "無料評価版"
    }
  ],
  "missed_slugs": [],
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob%' AND T1.attribute LIKE '%free trial version%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```

### 2. q: Bob の SLA 違反時のクレジットは何パーセント？

```
{
  "label": "q: Bob の SLA 違反時のクレジットは何パーセント？",
  "expected_route": "text2sql",
  "predicted_route": "text2sql",
  "row_contains_rate": 0.0,
  "doc_slug_rate": null,
  "missed_contains": [
    {
      "column": "*",
      "value": "2%"
    }
  ],
  "missed_slugs": [],
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob%' AND T1.attribute LIKE '%SLA violation credit%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```
