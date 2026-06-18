"""
CLI runner for the GitHub MCP agent — used to smoke-test the Obot wiring
without launching the Streamlit UI.

Prints:
  - The tools/list returned by the Obot-fronted GitHub MCP server
  - A streamed trace of agent reasoning + tool calls
  - The final response

Usage:
  python run_query.py "List the 5 most recently updated open issues in octocat/Hello-World."
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from textwrap import shorten

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from github_agent import GITHUB_AGENT_INSTRUCTIONS
from shared_utils import create_oauth_provider

load_dotenv()


DEFAULT_QUERY = (
    "List the 5 most recently updated open issues in the octocat/Hello-World "
    "repository. For each one, give title, number, last-updated timestamp, "
    "author, and link."
)


def _format_args(args) -> str:
    try:
        return shorten(json.dumps(args, default=str), width=200, placeholder="…")
    except Exception:
        return shorten(str(args), width=200, placeholder="…")


def _print_trace(messages) -> None:
    print("\n" + "=" * 72)
    print("AGENT TRACE")
    print("=" * 72)
    for i, m in enumerate(messages):
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            print(f"\n[{i}] USER")
            print(m.content)
        elif isinstance(m, AIMessage):
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                print(f"\n[{i}] ASSISTANT -> tool_calls")
                for tc in tool_calls:
                    name = tc.get("name", "?")
                    args = tc.get("args", {})
                    print(f"  • {name}({_format_args(args)})")
            if m.content:
                print(f"\n[{i}] ASSISTANT")
                print(m.content)
        elif isinstance(m, ToolMessage):
            name = getattr(m, "name", "?")
            content = m.content if isinstance(m.content, str) else str(m.content)
            print(f"\n[{i}] TOOL  {name}")
            print(shorten(content, width=2000, placeholder="…"))
        else:
            print(f"\n[{i}] {type(m).__name__}: {m}")


async def main() -> int:
    query = " ".join(sys.argv[1:]).strip() or DEFAULT_QUERY
    task_id = f"smoketest__{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    github_mcp_url = os.getenv("GITHUB_MCP_URL")
    if not github_mcp_url:
        print("ERROR: GITHUB_MCP_URL is not set. Copy .env.example to .env and fill it in.")
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.")
        return 2

    print(f"Query:    {query}")
    print(f"Task_id:  {task_id}")
    print(f"GitHub MCP URL: {github_mcp_url}")

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
        print(f"\nLoaded {len(tools)} tools from the GitHub MCP server (via Obot):")
        for t in tools:
            desc = (t.description or "").strip().splitlines()[0] if t.description else ""
            print(f"  - {t.name:40}  {shorten(desc, width=80, placeholder='…')}")

        llm = ChatOpenAI(model="gpt-4o", temperature=0.1)
        agent = create_react_agent(llm, tools)

        result = await asyncio.wait_for(
            agent.ainvoke({
                "messages": [
                    SystemMessage(content=GITHUB_AGENT_INSTRUCTIONS),
                    HumanMessage(content=query),
                ]
            }),
            timeout=180.0,
        )

        _print_trace(result["messages"])

        print("\n" + "=" * 72)
        print("FINAL RESPONSE")
        print("=" * 72)
        print(result["messages"][-1].content)
        return 0
    except asyncio.TimeoutError:
        print("ERROR: Timed out after 180s")
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            await stack.__aexit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
