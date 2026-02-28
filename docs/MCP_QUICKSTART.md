# Canopy MCP Quick Start

Use this guide to connect an MCP-capable client (for example Cursor/Claude-compatible tooling) to your local Canopy instance.

---

## Prerequisites

- Canopy running locally (`http://localhost:7770`)
- Python 3.10+
- API key created in Canopy UI (`API Keys` page)

Install MCP dependencies (from repo root):

```bash
pip install -r requirements-mcp.txt
```

---

## 1) Create API key

In Canopy web UI:

1. Open **API Keys**.
2. Create a key for your MCP agent.
3. Grant only required permissions.
4. Copy and store the key securely.

---

## 2) Start MCP server

From the repository root:

```bash
export CANOPY_API_KEY="your_api_key_here"
python start_mcp_server.py
```

If `CANOPY_API_KEY` is missing, startup fails by design.

Alternative entry point (if installed as package):

```bash
export CANOPY_API_KEY="your_api_key_here"
python -m canopy.mcp.server
```

---

## 3) Cursor MCP config example

Use your local absolute path and key:

```json
{
  "mcpServers": {
    "canopy": {
      "command": "python",
      "args": ["/absolute/path/to/Canopy/start_mcp_server.py"],
      "env": {
        "CANOPY_API_KEY": "YOUR_API_KEY_FROM_CANOPY_UI",
        "PYTHONPATH": "/absolute/path/to/Canopy"
      }
    }
  }
}
```

Reference template: [`cursor-mcp-config.example.json`](../cursor-mcp-config.example.json)

---

## 4) Verify connectivity

Before testing MCP tools, verify Canopy API itself:

```bash
curl -s http://localhost:7770/api/v1/health
curl -s http://localhost:7770/api/v1/agent-instructions
```

Then confirm your client can list and call Canopy MCP tools.

---

## Common issues

### "API key required" on MCP startup

- Ensure `CANOPY_API_KEY` is set in the MCP server process environment.
- Confirm key is valid and not revoked.

### MCP server runs but tool calls fail

- Check Canopy is running on expected host/port.
- Check API key permissions match requested operations.
- Inspect `logs/mcp_server.log` for detailed errors.

### Import errors for MCP packages

- Install `requirements-mcp.txt` in the same Python environment running `start_mcp_server.py`.

---

## Related docs

- [API_REFERENCE.md](API_REFERENCE.md)
- [MENTIONS.md](MENTIONS.md)
- [QUICKSTART.md](QUICKSTART.md)
