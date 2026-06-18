"""
🐙 GitHub MCP Agent — routed through the Obot gateway.

Forked from awesome-llm-apps/mcp_ai_agents/github_mcp_agent/github_agent.py.

Differences from upstream:
  - The original spawned ghcr.io/github/github-mcp-server as a local Docker
    container via stdio. We instead connect to the GitHub MCP server that
    is registered behind our Obot gateway, over streamable_http with OAuth.
  - The agent runtime is langchain + langgraph (create_react_agent), matching
    the Obot integration pattern used by apps/Stock-Analysis and
    apps/MCP-Trip-Planner. agno is no longer a dependency.
  - GITHUB_TOOLSETS=all is the intended toolset surface — it is set on the
    github-mcp-server process inside Obot, not here. This client just consumes
    whatever tools/list returns over the proxy.

See README.md and .env.example for required environment variables.
"""

import asyncio
import os
from contextlib import AsyncExitStack
from textwrap import dedent

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from shared_utils import create_oauth_provider

load_dotenv()


GITHUB_AGENT_INSTRUCTIONS = dedent("""\
    You are a GitHub assistant. Help users explore repositories and their activity.
    - Provide organized, concise insights about the repository
    - Focus on facts and data from the GitHub API
    - Use markdown formatting for better readability
    - Present numerical data in tables when appropriate
    - Include links to relevant GitHub pages when helpful
    - Execute ALL requested actions immediately and autonomously — do not ask for
      user confirmation before calling any tool, do not ask "shall I proceed?",
      do not pause mid-task. Complete every step in the request without interruption.
""")


async def run_github_agent(message: str, task_id: str = "manual") -> str:
    """
    Run a single GitHub agent turn against the Obot-fronted GitHub MCP server.

    Args:
        message:  The user query (free-form natural language).
        task_id:  Identifier propagated to Obot via the Task_id header so the
                  gateway's audit log can link tool calls back to this run.

    Returns:
        The agent's final markdown response, or an error string.
    """
    github_mcp_url = os.getenv("GITHUB_MCP_URL")
    if not github_mcp_url:
        return "Error: GITHUB_MCP_URL is not set (Obot connection string for the GitHub MCP server)."

    if not os.getenv("OPENAI_API_KEY"):
        return "Error: OPENAI_API_KEY is not set."

    provider = create_oauth_provider(github_mcp_url, "GitHub MCP Agent")

    server_config = {
        "github": {
            "url":       github_mcp_url,
            "transport": "streamable_http",
            "auth":      provider,
            "headers":   {"Task_id": task_id},
        }
    }

    stack = AsyncExitStack()
    await stack.__aenter__()
    try:
        client = MultiServerMCPClient(server_config)
        session = await stack.enter_async_context(client.session("github"))
        tools = await load_mcp_tools(session)

        llm = ChatOpenAI(model="gpt-4o", temperature=0.1)
        agent = create_react_agent(llm, tools)

        result = await asyncio.wait_for(
            agent.ainvoke({
                "messages": [
                    SystemMessage(content=GITHUB_AGENT_INSTRUCTIONS),
                    HumanMessage(content=message),
                ]
            }),
            timeout=180.0,
        )
        return result["messages"][-1].content
    except asyncio.TimeoutError:
        return "Error: Request timed out after 180 seconds"
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            await stack.__aexit__(None, None, None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Streamlit UI (same shape as upstream, just calls the Obot-routed agent)
# ---------------------------------------------------------------------------

def _main_ui() -> None:
    import streamlit as st  # lazy: keeps the CLI harness streamlit-free

    st.set_page_config(page_title="🐙 GitHub MCP Agent", page_icon="🐙", layout="wide")
    st.markdown("<h1 class='main-header'>🐙 GitHub MCP Agent</h1>", unsafe_allow_html=True)
    st.markdown("Explore GitHub repositories with natural language — routed through the Obot MCP gateway.")

    with st.sidebar:
        st.header("🔑 Configuration")
        st.caption("Most settings come from .env. You can override the OpenAI key here for ad-hoc runs.")

        openai_key = st.text_input("OpenAI API Key (override)", type="password")
        if openai_key:
            os.environ["OPENAI_API_KEY"] = openai_key

        st.markdown("---")
        st.markdown("### Obot wiring")
        st.code(f"GITHUB_MCP_URL = {os.getenv('GITHUB_MCP_URL', '(unset)')}", language="bash")
        st.code(f"REDIRECT_URI   = {os.getenv('REDIRECT_URI', '(unset)')}", language="bash")
        st.markdown(
            "GitHub PAT and `GITHUB_TOOLSETS=all` live on the Obot-side "
            "github-mcp-server, not in this client."
        )

        st.markdown("---")
        st.markdown("### Example Queries")
        st.markdown("**Issues**")
        st.markdown("- Show me issues by label")
        st.markdown("- What issues are being actively discussed?")
        st.markdown("**Pull Requests**")
        st.markdown("- What PRs need review?")
        st.markdown("- Show me recent merged PRs")
        st.markdown("**Repository**")
        st.markdown("- Show repository health metrics")
        st.markdown("- Show repository activity patterns")

    col1, col2 = st.columns([3, 1])
    with col1:
        repo = st.text_input("Repository", value="octocat/Hello-World", help="Format: owner/repo")
    with col2:
        query_type = st.selectbox("Query Type", ["Issues", "Pull Requests", "Repository Activity", "Custom"])

    if query_type == "Issues":
        query_template = f"Find issues labeled as bugs in {repo}"
    elif query_type == "Pull Requests":
        query_template = f"Show me recent merged PRs in {repo}"
    elif query_type == "Repository Activity":
        query_template = f"Analyze code quality trends in {repo}"
    else:
        query_template = ""

    query = st.text_area(
        "Your Query",
        value=query_template,
        placeholder="What would you like to know about this repository?",
    )

    if st.button("🚀 Run Query", type="primary", use_container_width=True):
        if not os.getenv("OPENAI_API_KEY"):
            st.error("OPENAI_API_KEY is not set (env or sidebar)")
        elif not os.getenv("GITHUB_MCP_URL"):
            st.error("GITHUB_MCP_URL is not set — point this at your Obot connection string for the GitHub MCP server")
        elif not query:
            st.error("Please enter a query")
        else:
            full_query = query if (repo in query) else f"{query} in {repo}"
            with st.spinner("Routing through Obot → GitHub MCP server…"):
                result = asyncio.run(run_github_agent(full_query, task_id="streamlit"))
            st.markdown("### Results")
            st.markdown(result)


if __name__ == "__main__":
    _main_ui()
