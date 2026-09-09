"""
AuditAgent v4 — Chatbot
A conversational interface over the audit's graph + RAG + report data.
Lets you ask things like "why is checkout slow?" or "what breaks if the
login page goes down?" and get an answer grounded in the actual graph —
not a static recap of the report.

Uses a portable ReAct-style loop rather than either provider's native
tool-calling API: Anthropic's and Gemini's function-calling formats differ
enough that supporting both natively would mean real duplication. Instead,
the model is instructed to respond with either a TOOL call (as JSON) or a
final ANSWER — one format, works identically against either provider.

Requires an API key, same as architect.py:
    export ANTHROPIC_API_KEY=sk-ant-...     (or GEMINI_API_KEY)

Usage:
    python chatbot.py report.json
    python chatbot.py report.json --endpoints endpoints.json --api-base-url https://api.example.com
    python chatbot.py report.json --provider gemini
    python chatbot.py report.json --question "what breaks if login fails?"   # one-shot, no REPL
"""

import argparse
import json
import re
import sys

from chat_context import AuditContext, TOOL_DESCRIPTIONS, call_tool
from llm_provider import chat_completion

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MAX_TOOL_ITERATIONS = 5  # hard cap so a confused model can't loop forever


def build_system_prompt(context: AuditContext) -> str:
    return f"""You are an assistant answering questions about a web application audit.
App: {context.crawl_report.get('start_url')}
Audit summary: {json.dumps(context.summary())}

{TOOL_DESCRIPTIONS}

To use a tool, respond with EXACTLY this format (nothing else in the message):
TOOL: <tool_name>
ARGS: {{"arg_name": "value"}}

To give your final answer, respond with EXACTLY this format:
ANSWER: <your answer in plain, conversational language>

Always ground your answer in real data from the tools — cite actual URLs, numbers, and
node names rather than speaking generically. If a question needs data you don't have yet,
call a tool first rather than guessing."""


def parse_model_response(text: str) -> tuple[str, dict | str]:
    """Returns ('tool', {'name':..., 'args':...}) or ('answer', text)."""
    text = text.strip()

    tool_match = re.search(r"TOOL:\s*(\w+)\s*\nARGS:\s*(\{.*\})", text, re.DOTALL)
    if tool_match:
        tool_name = tool_match.group(1).strip()
        try:
            args = json.loads(tool_match.group(2).strip())
        except json.JSONDecodeError:
            args = {}
        return ("tool", {"name": tool_name, "args": args})

    answer_match = re.search(r"ANSWER:\s*(.*)", text, re.DOTALL)
    if answer_match:
        return ("answer", answer_match.group(1).strip())

    # Fallback: model didn't follow the format — treat the whole response
    # as the answer rather than erroring out or looping.
    return ("answer", text)


def ask(context: AuditContext, question: str, provider: str, model: str | None, verbose: bool = True) -> str:
    messages = [
        {"role": "user", "content": build_system_prompt(context) + f"\n\nQuestion: {question}"}
    ]

    for iteration in range(MAX_TOOL_ITERATIONS):
        response_text = chat_completion(messages, provider=provider, model=model)
        kind, payload = parse_model_response(response_text)

        if kind == "answer":
            return payload

        tool_name, args = payload["name"], payload["args"]
        if verbose:
            print(f"  [calling tool: {tool_name}({args})]")
        result = call_tool(context, tool_name, args)

        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": f"Tool result: {json.dumps(result, default=str)}\n\n"
                                                       f"Continue: call another tool if needed, or give your ANSWER."})

    return ("I wasn't able to reach a confident answer within the tool-call limit — "
            "the question might need a more specific rephrasing.")


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v4 — chat with your audit results")
    parser.add_argument("report", help="Path to crawler.py's JSON report")
    parser.add_argument("--endpoints", type=str, default=None, help="Path to static_analysis.py's JSON output (optional, enables endpoint-gap questions)")
    parser.add_argument("--api-base-url", type=str, default=None, help="Same as merge.py — base URL where the API actually lives")
    parser.add_argument("--provider", type=str, default="gemini", choices=["anthropic", "gemini"])
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--question", type=str, default=None, help="Ask one question and exit, instead of an interactive REPL")
    args = parser.parse_args()

    print("Loading audit data and rebuilding the graph...")
    context = AuditContext(args.report, endpoints_path=args.endpoints, api_base_url=args.api_base_url)
    print(f"Ready. {context.summary()['num_graph_nodes']} nodes, "
          f"{context.summary()['num_graph_edges']} edges loaded.\n")

    if args.question:
        try:
            answer = ask(context, args.question, args.provider, args.model)
            print(f"\n{answer}")
        except RuntimeError as e:
            print(f"\n⚠ {e}")
            sys.exit(1)
        return

    print("Ask questions about your audit (e.g. \"what breaks if login fails?\"). Ctrl+C to exit.\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not question:
            continue
        try:
            answer = ask(context, question, args.provider, args.model)
            print(f"\n{answer}\n")
        except RuntimeError as e:
            print(f"\n⚠ {e}\n")


if __name__ == "__main__":
    main()
