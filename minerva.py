import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path
from pprint import pprint
from tools import build_tools
from mago_mcp import MagoMCP

HELP_TEXT = "/help, /memory, /session, /thinking, /reset, /exit"
WELCOME_ART = (
"            .:;++++++;;:.",
"        :;+++++++;:  .:;+++;.",
"      +++:        :++.     ;++;",
"    +++;;;:.        .+;      .++;",
"  :++.    .;++:       ;;       .++.",
"  ;+;          ;+;;+;. :+         ++:",
";++;;;+++;: :++:+: .:++++;.       ;+:",
"++        ;+;   : :    ;+.;+;.     ++.",
"++.        +;;+.         ;+   +++:  :+:",
"++++;;;;++++   ;++;+ :   .+:     .:;+++",
"++        :+++:.         :++++;++++;.++",
"++ ;++++++;++ .          +; :+       ++",
";+;.     .;+++++++ .   :+;   +;     :+:",
"++   .++;    ;+++;;+++:     ;+     ++",
":+; ;+.       :+          .+++    ++:",
"  ;++;          ++         .+.    ++:",
"  :++.         :+    :++;:;+   .++.",
"    ;++        :+    .+:     .++;",
"      ;++;.    +;     :+: .;++;",
"        .;+++;++.....::++++;.",
"              .;+++++++;.",
)
HELP_DETAILS = "\n".join(
    [
        "Commands:",
        "/help     Show this help message.",
        "/memory   Show the agent's distilled working memory.",
        "/session  Show the path to the saved session file.",
        "/thinking Shown the last thinking text, and any mismatched tool calls"
        "/reset    Clear the current session history and memory.",
        "/exit     Exit the agent.",
    ]
)
MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000

DEFAULT_LITELLM_HOST = "http://localhost:4000/v1"
DEFAULT_OLLAMA_HOST = "http://localhost:11434/v1"



##############################
#### Six Agent Components ####
##############################
# 1) User Data Context -> SubscriptionContext
# 2) Prompt Shape And Cache Reuse -> build_prefix, memory_text, build_messages
# 3) Structured Tools, Validation, And Permissions -> build_tools, run_tool, tool.validate, approve, tool_*
# 4) Context Reduction And Output Management -> clip, history_messages
# 5) Transcripts, Memory, And Resumption -> SessionStore, record, note_tool, ask, reset
# 6) Delegation And Bounded Subagents -> tool_delegate


def now():
    return datetime.now(timezone.utc).isoformat()

def base_path():
    return Path(__file__).resolve().parent

# Supporting helper for component 4 (context reduction and output management).
def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]

##################################
#### 1) User Data Context ####
##################################
class SubscriptionContext:
    def __init__(self, name):
        self.name = name;

    @classmethod
    def build(cls, name):
        return cls(name)

    def text(self):
        return self.name

##############################
#### 5) Session Memory #######
##############################
class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class FakeModelClient:
    def __init__(self, outputs_file):
        outputs = json.loads(Path(base_path(), "test_outputs", outputs_file).read_text(encoding="utf-8"))

        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, messages, tools, max_new_tokens):
        self.prompts.append(messages)
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")

        output = self.outputs.pop(0)
        
        return output["raw"], output["thinking"], output["tool_calls"]

class OpenAPIModelClient:
    def __init__(self, model, host, api_key, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout

    def complete(self, messages, tools, max_new_tokens):
        
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "max_tokens": max_new_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        request = urllib.request.Request(
            self.host + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            thinking_parts = []
            response_parts = []

            # A response can contain multiple tool calls.
            # When streaming, they are reconstructed using the "index" field.
            tool_calls_by_index = {}

            with urllib.request.urlopen(
                request,
                timeout=self.timeout
            ) as response:

                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()

                    # The SSE stream may contain empty lines.
                    if not line:
                        continue

                    # OpenAI uses Server-Sent Events:
                    # data: {...}   
                    if not line.startswith("data:"):
                        continue

                    payload_text = line[len("data:"):].strip()

                    # End of the OpenAI-compatible stream.
                    if payload_text == "[DONE]":
                        break

                    try:
                        data = json.loads(payload_text)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"Invalid JSON received from OpenAPI: {payload_text}"
                        ) from exc

                    if data.get("error"):
                        error = data["error"]

                        if isinstance(error, dict):
                            error_message = error.get("message", json.dumps(error))
                        else:
                            error_message = str(error)

                        raise RuntimeError(f"OpenAPI error: {error_message}")

                    choices = data.get("choices", [])

                    if not choices:
                        # Some events may contain only metadata
                        # or token usage information.
                        continue

                    delta = choices[0].get("delta", {})

                    # Regular response text.
                    content_part = delta.get("content")

                    if content_part:
                        response_parts.append(content_part)

                    # The reasoning field is not fully standardized.
                    # These names cover different models/providers via OpenAPI.
                    thinking_part = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                    )

                    if thinking_part:
                        thinking_parts.append(thinking_part)

                    # Native tool call.
                    # Each call may arrive split into multiple chunks.
                    for tool_call_part in delta.get("tool_calls", []):

                        index = tool_call_part.get("index", 0)

                        if index not in tool_calls_by_index:
                            tool_calls_by_index[index] = {
                                "id": "",
                                "type": "function",
                                "function": {
                                    "name": "",
                                    "arguments": "",
                                },
                            }

                        tool_call = tool_calls_by_index[index]
                        
                        # The ID can theoretically arrive in multiple chunks as well.
                        call_id_part = tool_call_part.get("id")

                        if call_id_part:
                            tool_call["id"] += call_id_part

                        call_type = tool_call_part.get("type")

                        if call_type:
                            tool_call["type"] = call_type

                        function_part = tool_call_part.get("function", {})

                        name_part = function_part.get("name")

                        if name_part:
                            tool_call["function"]["name"] += name_part

                        arguments_part = function_part.get("arguments")

                        if arguments_part:
                            tool_call["function"]["arguments"] += arguments_part

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")

            raise RuntimeError(
                f"OpenAPI request failed with HTTP {exc.code}: {body}"
            ) from exc

        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach OpenAPI.\n"
                "Make sure the OpenAPI proxy is running and reachable.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        content = "".join(response_parts)
        thinking = "".join(thinking_parts)

        tool_calls = [
            tool_calls_by_index[index]
            for index in sorted(tool_calls_by_index)
        ]

        return content, thinking, tool_calls

class MinervaAgent:
    def __init__(
        self,
        model_client,
        subscription,
        session_store,
        session=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
    ):
        self.model_client = model_client
        self.subscription = subscription
        self.root = base_path()
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "subscription": subscription.name,
            "history": [],
            "memory": {"task": "", "notes": []},
        }
        self.memory_length = 10
        self.mago_mcp = MagoMCP()
        self.tools = build_tools(self)
        self.tools_definition = [
            tool.openai_definition()
            for tool in self.tools.values()
        ]

        self.prefix = self.build_prefix()
        self.session_path = self.session_store.save(self.session)

    @classmethod
    def from_session(cls, model_client, subscription, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            subscription=subscription,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    ###############################################
    #### 3) Structured Tools And Permissions ######
    ###############################################

    # see tools.py

    ############################################
    #### 2) Prompt Shape And Cache Reuse #######
    ############################################
    def build_prefix(self):
        
        rules = Path(base_path(), "assets", "system_prompt.md").read_text(encoding="utf-8")

        subscription_info = Path(base_path(), "assets", "subscription_info.md").read_text(encoding="utf-8")

        return "\n\n".join([
            rules,
            subscription_info
        ])

    def memory_text(self):
        notes = ""
        if self.session["memory"]:
            notes = self.session["memory"]["notes"]
        return notes

    #####################################################
    #### 4) Context Reduction And Output Management #####
    #####################################################

    ########################################################
    #### 2) Prompt Shape And Cache Reuse (Continued) #######
    ########################################################
    def build_user_message(self,user_input):

        memory = self.memory_text()

        if memory:
            content = f"<cached_context>{memory}</cached_context><user_request>{user_input}</user_request>".strip()
        else:
            content = user_input

        return {
            "role": "user",
            "content": content
        }

    def build_messages(self, user_input):
        messages = [
            {
                "role" : "system",
                "content" : self.prefix
            }
        ]

        user_message = self.build_user_message(user_input)

        self.record(user_message | {"created_at": now()}, messages)

        return messages

    def log(self, num, messages):
        logfile = os.path.join(self.session_store.root, self.session["id"] + "-prompt.log")
        with open(logfile, "a", encoding="utf-8") as log:
            print(json.dumps({"iteration" : num, "messages" :messages }, indent=2, ensure_ascii=False), file=log)

    ###############################################
    #### 5) Session Memory (Continued) ###########
    ###############################################
    def record(self, item, messages):
        self.session["history"].append(item)
        messages.append({k: v for k, v in item.items() if k not in {"created_at", "thinking"}})
        self.store_memory(item)

        self.session_path = self.session_store.save(self.session)

    def tool_note(self, name, args, result):
        # TODO tool results where truncated: it make sense for file content, but not really for ERP queries
        # however, a stategy for dealing with big batches of returned data should be found
        argss = ", ".join(f"{k}={v!r}" for k, v in args.items())
        res = json.loads(result)
        if isinstance(res, dict):
            results = ", ".join(f"{k}: {v!r}" for k, v in res.items())
        else:
            results = result;
        return f"[tool] {name}({argss})={{{results}}}"

    def assistant_note(self, content, tool_calls):
        if content:
            return f"[assistant] {content}"
        elif tool_calls:
            names = ", ".join(
                tool_call["function"]["name"]
                for tool_call in tool_calls
            )
            return f"[assistant] requested tool execution: {names}"
        return ""

    def user_note(self, content):
        match = re.search(
            r"<user_request>(.*?)</user_request>",
            content,
            re.DOTALL
        )
        return f"[user] {match.group(1).strip() if match else content.strip()}"

    def store_memory(self, item):
        if not item:
            return

        note = ""

        if item["role"] == "tool":
            note = self.tool_note(item.get("name"), item.get("args"), item.get("content"))
        elif item["role"] == "assistant":
            note = self.assistant_note(item.get("content"), item.get("tool_calls"))
        elif item["role"] == "user":
            note = self.user_note(item.get("content"))
        if not note:
            return

        notes = self.session["memory"]["notes"]
        
        if note in notes:
            notes.remove(note)
        notes.append(note)

        del notes[:-self.memory_length]

    def request(self, user_input):
        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        messages = self.build_messages(user_input)

        while attempts < max_attempts:
            attempts += 1
            self.log(attempts, messages)
            raw, thinking, tool_calls = self.model_client.complete(
                messages = messages, 
                tools = self.tools_definition,
                max_new_tokens = self.max_new_tokens
            )

            if tool_calls:
                # It is essential to also preserve the assistant message
                # containing the tool calls.
                self.record(
                    {
                        "role": "assistant",
                        "content": raw or None,
                        "tool_calls": tool_calls,
                        "created_at": now(),
                        "thinking": thinking,
                    },
                    messages
                )

                for tool_call in tool_calls:
                    call_id = tool_call["id"]
                    function = tool_call["function"]

                    name = function["name"]
                    arguments_json = function.get("arguments", "{}")

                    args = {}

                    try:
                        args = json.loads(arguments_json)

                        if not isinstance(args, dict):
                            raise ValueError(
                                "Tool arguments must be a JSON object"
                            )

                    except (json.JSONDecodeError, ValueError) as exc:
                        result = {
                            "success": False,
                            "error": (
                                f"Invalid arguments for tool {name}: {exc}"
                            ),
                        }

                    else:
                        if tool_steps >= self.max_steps:
                            result = {
                                "success": False,
                                "error": (
                                    "Maximum number of tool calls reached. "
                                    "Do not request additional tools; provide the "
                                    "best possible answer using the available results."
                                ),
                            }

                        else:
                            tool_steps += 1

                            try:
                                # TODO manage the repeated tool calls
                                result = self.run_tool(name, args)

                            except ValueError as exc:
                                # Expected error that is useful to the model:
                                # semantic validation failure or unacceptable argument.
                                result = {
                                    "success": False,
                                    "error": str(exc),
                                }

                            except Exception as exc:
                                # Log the technical details internally,
                                # without exposing them in full to the model.
                                # self.log_tool_error(
                                #     name=name,
                                #     args=args,
                                #     error=exc,
                                # )

                                result = {
                                    "success": False,
                                    "error": "Internal tool execution error",
                                }

                    # The content of the tool message must be a string.
                    if isinstance(result, str):
                        result_content = result
                    else:
                        result_content = json.dumps(
                            result,
                            ensure_ascii=False,
                            default=str,
                        )

                    self.record(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "args": args,
                            "content": result_content,
                            "created_at": now(),
                        },
                        messages
                    )
                # The next iteration will rebuild the prompt including:
                # 1. the assistant message with tool_calls;
                # 2. one role=tool message for each result.
                continue

            final = (raw or "").strip()

            if not final:
                # Abnormal response: no text and no tool calls.
                self.record(
                    {
                        "role": "assistant",
                        "content": (
                            "The model returned neither content nor tool calls."
                        ),
                        "created_at": now(),
                        "thinking": thinking,
                    },
                    messages
                )
                continue

            self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking}, messages)
            self.log(attempts, messages)
            return final

        final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        
        self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking}, messages)
        self.log(attempts, messages)

        return final

    #############################################################
    #### 3) Structured Tools, Validation, And Permissions #######
    #############################################################
    def run_tool(self, name: str, args: dict):
        tool = self.tools.get(name)

        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        # TODO record the invocation of non-existing tools
        # mismatched_tool_calls.extend(tool_call_parts)
        # mismatched_tool_calls = []

        tool.validate(args)

        if tool.risky and not self.approve(name, args):
            raise ValueError(f"error: approval denied for {name}")

        return json.dumps(tool.run(**args), ensure_ascii=False)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def reset(self):
        self.session["history"] = []
        self.session["memory"] = {"task": "", "notes": []}
        self.session_store.save(self.session)

    ###################################################
    #### 6) Delegation And Bounded Subagents ##########
    ###################################################
    def tool_delegate(self, args):
        if self.depth >= self.max_depth:
            raise ValueError("delegate depth exceeded")
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        child = MinervaAgent(
            model_client=self.model_client,
            subscription=self.subscription,
            session_store=self.session_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
        )
        # TODO rewiew delegate calling
        # child.session["memory"]["task"] = task
        # child.session["memory"]["notes"] = [clip(self.history_messages(), 300)]
        child.session["memory"]["notes"] = self.session["memory"]["notes"]
        return "delegate_result:\n" + child.request(task)

def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center("M I N E R V A"),
            divider("-"),
            row(""),
            pair("MODEL", model, "SUBSCRIPTION", agent.subscription.name),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])

def build_agent(args):
    subscription = SubscriptionContext.build(args.subscription)
    store = SessionStore(base_path() / ".minerva" / "sessions")
    if (args.provider == "Ollama"):
        model = OpenAPIModelClient(
            model=args.model,
            host=args.host or DEFAULT_OLLAMA_HOST,
            api_key="ollama",
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.timeout,
        )
    elif (args.provider == "Fake"):
        model = FakeModelClient(
           outputs_file = "test_output_1.json",
        )
    else:
        model = OpenAPIModelClient(
            model=args.model,
            host=args.host or DEFAULT_LITELLM_HOST,
            api_key="sk-yOHDbHo20oItSWOCNEmmYA",
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.timeout,
        )

    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return MinervaAgent.from_session(
            model_client=model,
            subscription=subscription,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
        )
    return MinervaAgent(
        model_client=model,
        subscription=subscription,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal ERP agent for Mago using Ollama or OpenAPI models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--subscription", default="StarBoltTrading", help="User subscription")
    parser.add_argument("--provider", default="Ollama", help="Model provider: Ollama or LiteLLM")
    parser.add_argument("--model", default="qwen3.5:4b", help="Model name.")
    parser.add_argument("--host", default="", help="Model server URL.")
    parser.add_argument("--timeout", type=int, default=300, help="Provider request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for risky tools; auto grants the model arbitrary process execution and data modification.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to the model.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to the model.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    print(build_welcome(agent, model=args.model, host=args.host))

    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.request(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        try:
            user_input = input("\nminerva> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/thinking":
            print(agent.session["history"][-1]["thinking"])
            # pprint(agent.session["history"][-1]["mismatched_tools"])
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.request(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
