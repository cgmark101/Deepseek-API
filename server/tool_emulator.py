import json, uuid, re
from typing import List, Tuple, Optional
from .schemas import ChatMessage

NL = chr(10)

SYSTEM_TOOL_PROMPT = """[SYSTEM INSTRUCTION]
You have access to the following tools/functions:
{tools_json}

If you need to use a tool to answer the user's question, you MUST reply with a JSON code block matching the following schema and NOTHING else:
```json
{{
  "tool_calls": [
    {{
      "name": "TOOL_NAME",
      "arguments": {{}}
    }}
  ]
}}
```
where 'arguments' is a JSON object containing parameter values. Do not output any conversational text before or after this JSON block when calling a tool. If you do not need any tools, reply normally in plain text.
"""

def inject_tools_instruction(messages, tools):
    if not tools:
        return messages
    tool_desc = []
    for t in tools:
        if t.get("type") == "function":
            f = t["function"]
            tool_desc.append(
                f"- Name: {f['name']}" + NL +
                f"  Description: {f.get('description', '')}" + NL +
                f"  Parameters Schema: {json.dumps(f.get('parameters', {}))}"
            )
    tools_json = (NL * 2).join(tool_desc)
    ti = SYSTEM_TOOL_PROMPT.format(tools_json=tools_json)
    msgs = list(messages)
    idx = -1
    for i, m in enumerate(msgs):
        if m.role == "system":
            idx = i
            break
    if idx != -1:
        c = msgs[idx].content or ""
        if isinstance(c, str):
            msgs[idx].content = c + NL * 2 + ti
    else:
        msgs.insert(0, ChatMessage(role="system", content=ti))
    return msgs

def _extract_json(text):
    # Strip thinking tags first (reasoning models)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()
    # Try triple backtick block
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        c = m.group(1).strip()
        if c.startswith("{"):
            return c
    # Try single backtick prefix
    m = re.search(r"\`(?:json)?\s*({[\s\S]*?})\s*\`", text)
    if m:
        c = m.group(1).strip()
        if c.startswith("{"):
            try:
                json.loads(c)
                return c
            except:
                pass
    # Try bare JSON object (no backticks)
    start = text.find('{"tool_calls"')
    if start == -1:
        start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except:
            pass
    return None

def parse_static_tool_call(text):
    if not text:
        return text, None
    json_str = _extract_json(text)
    if not json_str:
        return text, None
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return text, None
    if "tool_calls" not in data or not isinstance(data["tool_calls"], list):
        return text, None
    tool_calls = []
    for raw in data["tool_calls"]:
        if not isinstance(raw, dict):
            continue
        args = raw.get("arguments", {})
        args_str = json.dumps(args) if isinstance(args, dict) else str(args)
        tool_calls.append({
            "id": "call_" + uuid.uuid4().hex[:8],
            "type": "function",
            "function": {
                "name": raw.get("name"),
                "arguments": args_str
            }
        })
    clean = text
    if json_str in clean:
        clean = clean.replace(json_str, "")
    clean = re.sub(r"```(?:json)?\s*", "", clean).strip()
    clean = re.sub(r"```", "", clean).strip()
    return clean or None, tool_calls
