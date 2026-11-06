# qnx-docs MCP server

Lets an AI assistant search and read the public QNX documentation at
https://www.qnx.com/developers/docs/.

- **Search** uses the Oxygen WebHelp search index that the docs site publishes
  (`oxygen-webhelp/app/search/index/*.js`), the same one the site's search box uses.
  The index is cached in `~/.cache/qnx-docs-mcp/` and refreshed every 7 days.
- **Reading** fetches one topic page live when asked and converts it to Markdown.
  Pages are kept only in memory. Nothing is mirrored or redistributed.
- Reads only URLs under `https://www.qnx.com/developers/docs/`.
  robots.txt allows these paths.

## Tools

| Tool | Purpose |
|---|---|
| `search_qnx_docs(query, docset="8.0", limit=10)` | Ranked topics with path, URL and summary. Words match exactly; `word*` matches a prefix. |
| `lookup_qnx_function(name, docset="8.0", full_description=False)` | One C API entry, trimmed for coding: synopsis (`#include` + prototype), arguments, library to link, returns, errno values, classification/safety table, shortened description. |
| `read_qnx_doc(path_or_url, docset="8.0", offset=0, max_chars=20000)` | Topic as Markdown, with absolute links you can follow. Use `offset` to page through long topics. |
| `list_qnx_docsets()` | `8.0`, `qnxeverywhere`, `relnotes8.0`, `BSP8.0`, `7.1` |

## Run / register

Requires [uv](https://docs.astral.sh/uv/). Dependencies are in `pyproject.toml`, and the first
`uv run` creates `.venv` in the project automatically.

```sh
claude mcp add --scope user qnx-docs -- uv run -q --directory /Users/rose/qnx-docs-mcp server.py
```

Remove it with `claude mcp remove qnx-docs -s user`.

Other MCP clients (Claude Desktop, Cursor, ...) use the same command,
`uv run -q --directory /Users/rose/qnx-docs-mcp server.py`, over stdio.

## Development

Run `uv sync` to create `.venv` with the server and dev dependencies (pylint).
`.vscode/settings.json` points VS Code at that environment, so imports resolve and the linter
works with no extra setup. Lint from the command line with `uv run pylint server.py`.

## Notes

- First search in a docset downloads its index: about 14 MB for 8.0, under 1 MB for qnxeverywhere.
  Later searches load from the disk cache in about 1 to 2 s per process.
- Search has no stemming, because the site's index has none. `window` won't match `windows`,
  so use `window*`.
- If the docs site changes its WebHelp version, the index format may change. The parser is in `DocIndex`.
