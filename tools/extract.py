"""raw/sources/ の原本を markdown 化して raw/extracted/ に保存する。

ルーティング:
  pdf / docx / pptx  -> docling
  xlsx               -> pandas で各シートを markdown 表に
  txt / md           -> そのままコピー

原本は読むだけ。書き換えない。
"""
import sys
import config


def slug(name: str) -> str:
    return name.rsplit(".", 1)[0].strip().replace(" ", "-")


def extract_docling(path):
    from docling.document_converter import DocumentConverter

    conv = DocumentConverter()
    result = conv.convert(str(path))
    return result.document.export_to_markdown()


def extract_xlsx(path):
    import pandas as pd

    sheets = pd.read_excel(path, sheet_name=None)
    out = []
    for sheet_name, df in sheets.items():
        out.append(f"## シート: {sheet_name}\n")
        out.append(df.to_markdown(index=False))
        out.append("")
    return "\n".join(out)


def main():
    config.RAW_EXTRACTED.mkdir(parents=True, exist_ok=True)
    targets = list(config.RAW_SOURCES.iterdir())
    if not targets:
        print("raw/sources/ が空です。原本を置いてください。")
        return

    for path in targets:
        if path.is_dir():
            continue
        ext = path.suffix.lower().lstrip(".")
        out = config.RAW_EXTRACTED / f"{slug(path.name)}.md"
        if out.exists():
            print(f"skip (既存): {out.name}")
            continue
        try:
            if ext in ("pdf", "docx", "pptx"):
                text = extract_docling(path)
            elif ext == "xlsx":
                text = extract_xlsx(path)
            elif ext in ("txt", "md"):
                text = path.read_text(encoding="utf-8", errors="replace")
            else:
                print(f"未対応の拡張子なのでスキップ: {path.name}")
                continue
        except Exception as e:  # noqa: BLE001
            print(f"抽出失敗 {path.name}: {e}", file=sys.stderr)
            continue

        header = f"<!-- source: {path.name} -->\n\n"
        out.write_text(header + text, encoding="utf-8")
        print(f"抽出完了: {out.name}")


if __name__ == "__main__":
    main()
