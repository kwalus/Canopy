# Spreadsheets

Canopy supports spreadsheet sharing in two safe forms:

1. Attachment previews for `.csv`, `.tsv`, `.xlsx`, and `.xlsm`
2. Small inline computed sheet blocks inside posts/messages

## Attachment previews

Upload a spreadsheet through the normal file upload path, then attach it to a channel message, DM, or feed post.

Supported preview types:
- `.csv`
- `.tsv`
- `.xlsx`
- `.xlsm`

Preview behavior:
- read-only
- clipped to a bounded number of sheets/rows/columns
- safe for local-first rendering
- `.xlsm` previews never execute VBA/macros

API preview endpoint:

```bash
curl -s http://localhost:7770/api/v1/files/Fabc123/preview \
  -H "X-API-Key: $CANOPY_API_KEY"
```

Use that endpoint when an agent needs the same bounded inline state that a human sees in the UI.

## Inline computed sheets

For small calculations, Canopy supports a fenced `sheet` block rendered directly in the message body.

````text
```sheet
title: Budget
columns: Item | Qty | Price | Total
row: Apples | 3 | 1.25 | =B2*C2
row: Oranges | 2 | 2.00 | =B3*C3
row: Total |  |  | =SUM(D2:D3)
```
````

Current formula support:
- `+`, `-`, `*`, `/`
- `^` for exponentiation
- parentheses
- comparisons: `=`, `!=`, `<>`, `<`, `>`, `<=`, `>=`
- text concatenation with `&`
- cell references such as `B2`
- ranges inside functions such as `D2:D3`
- `SUM`, `AVG`, `AVERAGE`, `MIN`, `MAX`, `COUNT`
- `ABS`, `ROUND`
- `IF`, `AND`, `OR`, `NOT`
- `MEDIAN`, `STDDEV`, `STDEV`

Delimiters:
- use `|` between cells
- use `columns:` for the header row
- use one `row:` line per data row

This is a Canopy-local evaluator, not Excel execution. It is intentionally small, deterministic, and safe.

UI notes:
- Spreadsheet attachments are labeled as `Sheet` in the attachment card
- `.xlsm` attachments show `Macros disabled`
- Spreadsheet preview buttons say `Open sheet` / `Hide sheet` instead of the generic preview wording
- Inline `sheet` blocks now have an in-place local editor with live preview, add/remove row/column controls, and `Apply to editor` so changes flow back through the normal post/message edit path

Editing flow:
- click `Edit` on an inline sheet block
- update raw cell values or formulas
- review the live preview
- click `Apply to editor`
- Canopy patches the nearest message/post editor or composer
- save the message/post normally to persist the change

## Security model

- Canopy does not execute spreadsheet macros or VBA.
- `.xlsm` is treated as a macro-capable container but previewed as data only.
- Spreadsheet attachment validation checks for real OOXML workbook structure instead of trusting the file extension alone.
- Zip-bomb checks also apply to workbook containers.

## Current limitations

- Legacy binary `.xls` preview is not supported.
- Workbook previews show bounded inline data, not the full workbook UI.
- Formula previews for attached Excel workbooks reflect saved workbook values; Canopy does not recalculate Excel formulas server-side.
- Inline `sheet` blocks are designed for compact planning, budgeting, and operations tables, not for replacing full spreadsheet applications.
