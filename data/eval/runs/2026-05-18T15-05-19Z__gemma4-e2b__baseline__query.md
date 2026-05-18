# eval report — query

## run metadata

| key           | value                                                                                 |
|---------------|---------------------------------------------------------------------------------------|
| timestamp_utc | 2026-05-18T15-05-19Z                                                                  |
| model         | gemma4:e2b                                                                            |
| temperature   | 0.2                                                                                   |
| num_ctx       | 8192                                                                                  |
| git_rev       | 154a20dbfa8e891e5f23cb4adefcddf499d7a266                                              |
| tag           | baseline                                                                              |
| runs          | 2                                                                                     |
| stage         | query                                                                                 |
| gold_path     | C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\gold\query\acme-overview.yml |

## metrics

| metric            | mean ± std    |
|-------------------|---------------|
| route_accuracy    | 0.938 ± 0.062 |
| sql_success_rate  | 1.000 ± 0.000 |
| row_contains_rate | 0.000 ± 0.000 |
| doc_slug_rate     | 0.000 ± 0.000 |


gold: qa=8, fixture=C:\Users\mizuk\Documents\workspace\my_llm_wiki\data\eval\fixtures\acme.sqlite
