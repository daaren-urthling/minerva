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


DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
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
# 2) Prompt Shape And Cache Reuse -> build_prefix, memory_text, prompt
# 3) Structured Tools, Validation, And Permissions -> build_tools, run_tool, validate_tool, approve, parse, path, tool_*
# 4) Context Reduction And Output Management -> clip, history_text
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

    # def complete(self, prompt, tools, max_new_tokens):
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

    # def complete(self, prompt, max_new_tokens):
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
            # mismatched_tool_calls = []

            # Una risposta può contenere più tool call.
            # In streaming vengono ricostruite usando il campo "index".
            tool_calls_by_index = {}

            with urllib.request.urlopen(
                request,
                timeout=self.timeout
            ) as response:

                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()

                    # Nello stream SSE possono esserci righe vuote
                    if not line:
                        continue

                    # OpenAI usa Server-Sent Events:
                    # data: {...}
                    if not line.startswith("data:"):
                        continue

                    payload_text = line[len("data:"):].strip()

                    # Termine dello stream OpenAI-compatible
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
                        # Alcuni eventi possono contenere soltanto metadati
                        # o informazioni sull'utilizzo dei token.
                        continue

                    delta = choices[0].get("delta", {})

                    # Testo normale della risposta
                    content_part = delta.get("content")

                    if content_part:
                        response_parts.append(content_part)

                    # Il campo del reasoning non è completamente standardizzato.
                    # Questi nomi coprono diversi modelli/provider tramite OpenAPI.
                    thinking_part = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                    )

                    if thinking_part:
                        thinking_parts.append(thinking_part)

                    # Tool call native.
                    # Ogni chiamata può arrivare suddivisa in più frammenti.
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
                        
                        # Anche l'id può teoricamente arrivare per frammenti.
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

                    # if tool_call_parts:
                    #     mismatched_tool_calls.extend(tool_call_parts)

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

        # return content, thinking, mismatched_tool_calls
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
        # self.tools = self.build_tools()
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
#region old code
    # def build_tools(self):
    #     tools = {
    #         "CUSTOMER_INFO": {
    #             "schema": {"customer_code": "str"},
    #             "risky": False,
    #             "description": "Returns: company_name, address, telephone and other company info",
    #             "run": self.customer_info,
    #         },

    #         "SEARCH_PEOPLE": {
    #             "schema": {"name": "str", "last_name": "str"},
    #             "risky": False,
    #             "description": "Returns:customer_code or empty result when not found",
    #             "run": self.search_people,
    #         },
    #         "ITEM_INFO": {
    #             "schema": {"item_code": "str"},
    #             "risky": False,
    #             "description": "Returns item information: description, price, etc.",
    #             "run": self.item_info,
    #         },
    #         "SEARCH_ITEM": {
    #             "schema": {"description": "str"},
    #             "risky": False,
    #             "description": "Returns item code or empty result when not found. Wildcards are allowed.",
    #             "run": self.search_item,
    #         },
    #         "STOCK_LEVEL": {
    #             "schema": {"item_code": "str"},
    #             "risky": False,
    #             "description": "Returns the current stock level of the item, and the unit of measure it is expressed: pieces, boxes, etc.",
    #             "run": self.item_stock_level,
    #         },
    #     }
    #     if self.depth < self.max_depth:
    #         tools["DELEGATE"] = {
    #             "schema": {"task": "str", "max_steps": "int=3"},
    #             "risky": False,
    #             "description": "Ask a bounded read-only child agent to investigate.",
    #             "run": self.tool_delegate,
    #         }
    #     return tools
#endregion

    ############################################
    #### 2) Prompt Shape And Cache Reuse #######
    ############################################
    def build_prefix(self):
        
        rules = Path(base_path(), "assets", "system_prompt.md").read_text(encoding="utf-8")

        # tool_lines = []
        # for name, tool in self.tools.items():
        #     fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        #     risk = "approval required" if tool["risky"] else "safe"
        #     tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
        # tool_text = "\n".join(tool_lines)

        # examples = Path(base_path(), "assets", "additional_info.md").read_text(encoding="utf-8")

        subscription_info = Path(base_path(), "assets", "subscription_info.md").read_text(encoding="utf-8")

        return "\n\n".join([
            rules,
            # "**AVAILABLE SERVICE REQUESTS**:\n" + tool_text,
            # examples,
            subscription_info
        ])

    def memory_text(self):
        # memory = self.session["memory"]
        # notes = "\n".join(f"- {note}" for note in memory["notes"]) or "- none"
        # return "\n".join([
        #     "Memory:",
        #     # f"- task: {memory['task'] or '-'}",
        #     "- notes:",
        #     notes,
        # ])
        notes = ""
        if self.session["memory"]:
            notes = self.session["memory"]["notes"]
        return notes

    #####################################################
    #### 4) Context Reduction And Output Management #####
    #####################################################
    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] in ("write_file", "patch_file"):
                path = str(item["args"].get("path", ""))
                seen_reads.discard(path)
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def history_messages(self):
        history = self.session["history"]
        if not history:
            return []
        
        exclude = {"created_at", "thinking"}

        return [
            {k: v for k, v in item.items() if k not in exclude}
            for item in history
        ]

    ########################################################
    #### 2) Prompt Shape And Cache Reuse (Continued) #######
    ########################################################
    def prompt(self, user_message):
        return "\n\n".join([
            self.prefix,
            self.memory_text(),
            "Transcript:\n" + self.history_text(),
            "Current user request:\n" + user_message,
        ])

    def build_user_message(
        self,
        user_input: str,
        memory: str | None = None
    ) -> dict:

        if memory:
            content = f"""
            <cached_context>
            {memory}
            </cached_context>

            <user_request>
            {user_input}
            </user_request>
            """.strip()

        else:
            content = user_input

        return {
            "role": "user",
            "content": content
        }

    def build_messages(self, user_input = None):
        messages = [
            {
                "role" : "system",
                "content" : self.prefix
            }
        ]

        messages.extend(self.history_messages())

        if user_input:
            user_message = self.build_user_message(
                user_input,
                self.memory_text()
            )
    
            messages.append(user_message)

            self.record(user_message | {"created_at": now()})

        return messages

    def log(self, num, messages):
        logfile = os.path.join(self.session_store.root, self.session["id"] + "-prompt.log")
        with open(logfile, "a", encoding="utf-8") as log:
            print(json.dumps({"iteration" : num, "messages" :messages }, indent=2, ensure_ascii=False), file=log)

    ###############################################
    #### 5) Session Memory (Continued) ###########
    ###############################################
    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def note_tool(self, name, args, result):
        memory = self.session["memory"]
        # TODO tool results where truncated: it make sense for file content, but not really for ERP queries
        # however, a stategy for dealing with big batches of returned data should be found
        # note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
        # note = f"{name}: {clip(str(result).replace(chr(10), ' '))}"
        note = json.dumps(memory, separators=(",", ":"), default=str)
        self.remember(memory["notes"], note, 5)

    def ask(self, user_input):
        memory = self.session["memory"]
        # if not memory["task"]:
        #     memory["task"] = clip(user_input.strip(), 300)
        # self.record({"role": "user", "content": user_input, "created_at": now()})

        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        # while tool_steps < self.max_steps and attempts < max_attempts:
        while attempts < max_attempts:
            attempts += 1
            # prompt = self.prompt(user_message)
            messages = self.build_messages(user_input if attempts <= 1 else None)
            # self.log(attempts, prompt)
            self.log(attempts, messages)
            # raw, thinking, tool_calls = self.model_client.complete(prompt, self.max_new_tokens)
            raw, thinking, tool_calls = self.model_client.complete(
                messages = messages, 
                tools = self.tools_definition,
                max_new_tokens = self.max_new_tokens
            )
#region old
            # kind, payload = self.parse(raw)

            # if kind == "tool":
            #     tool_steps += 1
            #     name = payload.get("name", "")
            #     args = payload.get("args", {})
            #     result = self.run_tool(name, args)
            #     self.record(
            #         {
            #             "role": "tool",
            #             "name": name,
            #             "args": args,
            #             "content": result,
            #             "created_at": now(),
            #             "thinking" : thinking,
            #             "mismatched_tools": mismatched_tools
            #         }
            #     )
#endregion

            if tool_calls:
                # È indispensabile conservare anche il messaggio assistant
                # che contiene le richieste dei tool.
                self.record(
                    {
                        "role": "assistant",
                        "content": raw or None,
                        "tool_calls": tool_calls,
                        "created_at": now(),
                        "thinking": thinking,
                    }
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
                                result = self.run_tool(name, args)

                            except ValueError as exc:
                                # Errore previsto e utile al modello:
                                # validazione semantica o argomento non accettabile.
                                result = {
                                    "success": False,
                                    "error": str(exc),
                                }

                            except Exception as exc:
                                # Registrare internamente il dettaglio tecnico,
                                # senza esporlo integralmente al modello.
                                # self.log_tool_error(
                                #     name=name,
                                #     args=args,
                                #     error=exc,
                                # )

                                result = {
                                    "success": False,
                                    "error": "Internal tool execution error",
                                }

                    # Il content del messaggio tool deve essere una stringa.
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
                        }
                    )
                self.note_tool(name, args, result)
                # La prossima iterazione ricostruirà il prompt includendo:
                # 1. il messaggio assistant con tool_calls;
                # 2. un messaggio role=tool per ogni risultato.
                continue

            # if kind == "retry":
            #     self.record({"role": "assistant", "content": payload, "created_at": now(), "thinking" : thinking, "mismatched_tools": mismatched_tools})
            #     continue

            # final = (payload or raw).strip()
            final = (raw or "").strip()

            if not final:
                # Risposta anomala: nessun testo e nessuna tool call.
                self.record(
                    {
                        "role": "assistant",
                        "content": (
                            "The model returned neither content nor tool calls."
                        ),
                        "created_at": now(),
                        "thinking": thinking,
                    }
                )
                continue

            # self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking, "mismatched_tools": mismatched_tools})
            self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking})
            self.remember(memory["notes"], clip(final, 220), 5)
            return final

        final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        
        # self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking, "mismatched_tools": mismatched_tools})
        self.record({"role": "assistant", "content": final, "created_at": now(), "thinking" : thinking})
        
        return final

    #############################################################
    #### 3) Structured Tools, Validation, And Permissions #######
    #############################################################
    def run_tool(self, name: str, args: dict):
        tool = self.tools.get(name)

        if tool is None:
            raise ValueError(f"Unknown tool: {name}")

        tool.validate(args)

        return tool.run(**args)

#region old code    
    # def run_tool(self, name, args):
    #     tool = self.tools.get(name)
    #     if tool is None:
    #         return f"error: unknown tool '{name}'"
    #     try:
    #         self.validate_tool(name, args)
    #     except Exception as exc:
    #         example = Path(base_path(), "assets", "additional_info.md").read_text(encoding="utf-8")
    #         message = f"error: invalid arguments for {name}: {exc}"
    #         if example:
    #             message += f"\nexample: {example}"
    #         return message
    #     if self.repeated_tool_call(name, args):
    #         return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
    #     if tool["risky"] and not self.approve(name, args):
    #         return f"error: approval denied for {name}"
    #     try:
    #         return clip(tool["run"](args))
    #     except Exception as exc:
    #         return f"error: tool {name} failed: {exc}"
#endregion

    def repeated_tool_call(self, name, args):
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    def validate_tool(self, name, args):
        args = args or {}
        # TODO implement the right validation for the new tooling

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

#region old code
    # @staticmethod
    # def parse(raw):
    #     raw = str(raw)
    #     if "<service>" in raw and ("<final>" not in raw or raw.find("<service>") < raw.find("<final>")):
    #         body = MinervaAgent.extract(raw, "service")
    #         try:
    #             payload = json.loads(body)
    #         except Exception:
    #             return "retry", MinervaAgent.retry_notice("model returned malformed service JSON")
    #         if not isinstance(payload, dict):
    #             return "retry", MinervaAgent.retry_notice("service payload must be a JSON object")
    #         if not str(payload.get("name", "")).strip():
    #             return "retry", MinervaAgent.retry_notice("service payload is missing a service name")
    #         args = payload.get("args", {})
    #         if args is None:
    #             payload["args"] = {}
    #         elif not isinstance(args, dict):
    #             return "retry", MinervaAgent.retry_notice()
    #         return "tool", payload
    #     if "<service" in raw and ("<final>" not in raw or raw.find("<service") < raw.find("<final>")):
    #         payload = MinervaAgent.parse_xml_tool(raw)
    #         if payload is not None:
    #             return "service", payload
    #         return "retry", MinervaAgent.retry_notice()
    #     if "<final>" in raw:
    #         final = MinervaAgent.extract(raw, "final").strip()
    #         if final:
    #             return "final", final
    #         return "retry", MinervaAgent.retry_notice("model returned an empty <final> answer")
    #     raw = raw.strip()
    #     if raw:
    #         return "final", raw
    #     return "retry", MinervaAgent.retry_notice("model returned an empty response")

    # @staticmethod
    # def retry_notice(problem=None):
    #     prefix = "Runtime notice"
    #     if problem:
    #         prefix += f": {problem}"
    #     else:
    #         prefix += ": model returned malformed service output"
    #     return (
    #         f"{prefix}. Reply with a valid <service> call or a non-empty <final> answer."
    #     )

    # @staticmethod
    # def parse_xml_tool(raw):
    #     match = re.search(r"<service(?P<attrs>[^>]*)>(?P<body>.*?)</service>", raw, re.S)
    #     if not match:
    #         return None
    #     attrs = MinervaAgent.parse_attrs(match.group("attrs"))
    #     name = str(attrs.pop("name", "")).strip()
    #     if not name:
    #         return None

    #     body = match.group("body")
    #     args = dict(attrs)
    #     for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
    #         if f"<{key}>" in body:
    #             args[key] = MinervaAgent.extract_raw(body, key)

    #     body_text = body.strip("\n")
    #     if name == "write_file" and "content" not in args and body_text:
    #         args["content"] = body_text
    #     if name == "delegate" and "task" not in args and body_text:
    #         args["task"] = body_text.strip()
    #     return {"name": name, "args": args}

    # @staticmethod
    # def parse_attrs(text):
    #     attrs = {}
    #     for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
    #         attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    #     return attrs

    # @staticmethod
    # def extract(text, tag):
    #     start_tag = f"<{tag}>"
    #     end_tag = f"</{tag}>"
    #     start = text.find(start_tag)
    #     if start == -1:
    #         return text
    #     start += len(start_tag)
    #     end = text.find(end_tag, start)
    #     if end == -1:
    #         return text[start:].strip()
    #     return text[start:end].strip()

    # @staticmethod
    # def extract_raw(text, tag):
    #     start_tag = f"<{tag}>"
    #     end_tag = f"</{tag}>"
    #     start = text.find(start_tag)
    #     if start == -1:
    #         return text
    #     start += len(start_tag)
    #     end = text.find(end_tag, start)
    #     if end == -1:
    #         return text[start:]
    #     return text[start:end]
#endregion

    def reset(self):
        self.session["history"] = []
        self.session["memory"] = {"task": "", "notes": []}
        self.session_store.save(self.session)

    def customer_info(self, customer_code):
        return json.dumps({
            "customer_code": "0042",
            "company_name": "ACME ltd",
            "tax_id_number": "02572020101",
            "telephone1": "040 5657746",
            "telephone2": "040 6657288",
            "telex": "123-acme-7756",
            "fax": "040 6577466",
            "internet": "www.acmeglobal.com",
            "e_mail": "sales@acmeglobal.com",
        })

    def search_people(self, name, last_name):
        return json.dumps({
            "customer_code": "0042",
            "working_position": "owner",
        })

    def item_info(self, item_code):
        return

    def search_item(self, description):
        return

    def item_stock_level(self, item_code):
        return

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
        # TODO this is wrong, task must come from input
        child.session["memory"]["task"] = task
        # child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        child.session["memory"]["notes"] = [clip(self.history_messages(), 300)]
        return "delegate_result:\n" + child.ask(task)

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
                print(agent.ask(prompt))
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
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
