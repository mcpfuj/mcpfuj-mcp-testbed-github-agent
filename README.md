# GitHub MCP Agent (Obot-routed)

Forked from
[`awesome-llm-apps/mcp_ai_agents/github_mcp_agent`](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/mcp_ai_agents/github_mcp_agent)
and adapted to fit this testbed.

## What changed vs. upstream

| | Upstream | Here |
|---|---|---|
| GitHub MCP server | Spawned locally via `docker run ghcr.io/github/github-mcp-server` over **stdio** | Hosted behind the **Obot gateway**, reached over **streamable HTTP + OAuth 2.1 / PKCE** |
| GitHub PAT | Read by the client and forwarded into the container env | Lives on the Obot-side server; client never touches it |
| Toolset surface | `GITHUB_TOOLSETS=repos,issues,pull_requests` | `GITHUB_TOOLSETS=all` (configured on the Obot-side server — intentional over-permission for the access-control eval) |
| Agent runtime | `agno.Agent` + `agno.tools.mcp.MCPTools` | `langgraph.prebuilt.create_react_agent` + `langchain_mcp_adapters` (matches `apps/Stock-Analysis` and `apps/MCP-Trip-Planner`) |
| UI | Streamlit | Streamlit (preserved) |

Every MCP call is tagged with a `Task_id` header so Obot's audit log can link
tool invocations back to the originating run — same pattern as the other apps.

## Files

```
apps/github_agent/
├── github_agent.py    # Streamlit UI + run_github_agent() — modified for Obot
├── run_query.py       # CLI harness: runs one query and prints full trace
├── shared_utils.py    # OAuth provider + FileTokenStorage (Obot pattern)
├── requirements.txt
├── .env.example
└── README.md          # this file
```

## Setup

1. Install deps:
   ```bash
   pip install -r requirements.txt
   ```

2. In Obot, register the GitHub MCP server (`ghcr.io/github/github-mcp-server`)
   with these env vars on the server side:
   - `GITHUB_PERSONAL_ACCESS_TOKEN` — PAT with the scopes you want exposed
   - `GITHUB_TOOLSETS=all` — REQUIRED for this app

3. Copy the connection string from the Obot dashboard into `.env`:
   ```bash
   cp .env.example .env
   # edit GITHUB_MCP_URL=https://cbg-obot.com/mcp-connect/<id>
   # edit OPENAI_API_KEY=sk-...
   ```

4. First run will trigger an OAuth flow:
   - The terminal prints an authorization URL
   - Visit it, approve, then paste the redirected callback URL back into the prompt
   - Tokens are cached under `~/.github_agent_tokens/` and refreshed automatically on later runs

## Run

CLI smoke test (default query: 5 most recently updated open issues in `octocat/Hello-World`):

```bash
python run_query.py
# or
python run_query.py "List the 5 most recently updated open issues in octocat/Hello-World."
```

Streamlit UI:

```bash
streamlit run github_agent.py
```

## Why `GITHUB_TOOLSETS=all`

This testbed exists to evaluate access-control / anomaly-detection rules on
top of MCP traffic. We deliberately want a **wide, over-permissive tool
surface** so the rule engine has something interesting to constrain. Do not
ship this configuration in production.
