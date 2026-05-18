# eval report — query

## run metadata

| key           | value                                                                             |
|---------------|-----------------------------------------------------------------------------------|
| timestamp_utc | 2026-05-18T15-00-27Z                                                              |
| model         | gemma4:e2b                                                                        |
| temperature   | 0.2                                                                               |
| num_ctx       | 8192                                                                              |
| git_rev       | 154a20dbfa8e891e5f23cb4adefcddf499d7a266                                          |
| tag           | baseline                                                                          |
| runs          | 2                                                                                 |
| stage         | query                                                                             |
| gold_path     | C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\gold\query\i127-9285.yml |

## metrics

| metric            | mean ± std    |
|-------------------|---------------|
| route_accuracy    | 1.000 ± 0.000 |
| sql_success_rate  | 1.000 ± 0.000 |
| row_contains_rate | 0.000 ± 0.000 |
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

### 2. q: Bob 無料評価版の試用期間は？

```
{
  "label": "q: Bob 無料評価版の試用期間は？",
  "expected_route": "text2sql",
  "predicted_route": "text2sql",
  "row_contains_rate": 0.0,
  "doc_slug_rate": null,
  "missed_contains": [
    {
      "column": "*",
      "value": "30 日間"
    }
  ],
  "missed_slugs": [],
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob%' AND T1.attribute LIKE '%trial period%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```

### 3. q: Bob Pro は月にコインがいくらもらえる？

```
{
  "label": "q: Bob Pro は月にコインがいくらもらえる？",
  "expected_route": "text2sql",
  "predicted_route": "text2sql",
  "row_contains_rate": 0.0,
  "doc_slug_rate": null,
  "missed_contains": [
    {
      "column": "*",
      "value": "40 Bobcoin"
    }
  ],
  "missed_slugs": [],
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob Pro%' AND T1.attribute LIKE '%monthly coin%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```

### 4. q: Bob の SLA 違反時のクレジットは何パーセント？

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
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob%' AND T1.attribute LIKE '%SLA violation%' AND T1.attribute LIKE '%credit%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```

### 5. q: Bob Enterprise の超過課金の単位は？

```
{
  "label": "q: Bob Enterprise の超過課金の単位は？",
  "expected_route": "text2sql",
  "predicted_route": "text2sql",
  "row_contains_rate": 0.0,
  "doc_slug_rate": null,
  "missed_contains": [
    {
      "column": "*",
      "value": "1000 リソース単位パック"
    }
  ],
  "missed_slugs": [],
  "sql": "SELECT T1.value FROM facts AS T1 INNER JOIN entities AS T2 ON T1.entity_id = T2.id WHERE T2.canonical_name LIKE '%Bob Enterprise%' AND T1.attribute LIKE '%surcharge unit%' LIMIT 50",
  "sql_ok": true,
  "n_rows": 0,
  "error": null
}
```
