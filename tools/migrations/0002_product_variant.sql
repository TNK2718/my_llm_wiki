-- Migration 0002: starter set に product_variant junction を追加。
-- schema.sql 側で CREATE TABLE IF NOT EXISTS してあるので、ここでは conflict_kinds の
-- starter 値追加のみ。
INSERT OR IGNORE INTO conflict_kinds(kind, description, created_at) VALUES
  ('product_variant_existence', 'product_variant 存在主張の矛盾', '2026-05-21T00:00:00Z');
