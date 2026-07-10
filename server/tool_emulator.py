import json
import uuid
from typing import List, Tuple, Optional
from .schemas import ChatMessage

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


def inject_tools_instruction(messages: List[ChatMessage], tools: List[dict]) -> List[ChatMessage]:
    """Inject tool descriptions and execution instructions into the messages history."""
    if not tools:
        return messages
    
    # Format the tools cleanly as JSON
    tool_desc = []
    for t in tools:
        if t.get("type") == "function":
            f = t["function"]
            tool_desc.append(
                f"- Name: {f['name']}\n"
                f"  Description: {f.get('description', '')}\n"
                f"  Parameters Schema: {json.dumps(f.get('parameters', {}))}"
            )
            
    tools_json = "\n\n".join(tool_desc)
    tool_instructions = SYSTEM_TOOL_PROMPT.format(tools_json=tools_json)
    
    new_messages = list(messages)
    # Check if there is an existing system message
    system_msg_idx = -1
    for idx, msg in enumerate(new_messages):
        if msg.role == "system":
            system_msg_idx = idx
            break
            
    if system_msg_idx != -1:
        # Append instructions to the existing system prompt
        existing_content = new_messages[system_msg_idx].content or ""
        if isinstance(existing_content, str):
            new_messages[system_msg_idx].content = existing_content + "\n\n" + tool_instructions
    else:
        # Prepend a new system message
        new_messages.insert(0, ChatMessage(role="system", content=tool_instructions))
        
    return new_messages


def parse_static_tool_call(text: str) -> Tuple[Optional[str], Optional[List[dict]]]:
    """Parse static text response for tool calls.
    
    Returns:
        Tuple[clean_text, tool_calls_list]
        If no tool call is found, returns (text, None).
    """
    if not text:
        return text, None
        
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start:end+1])
            if "tool_calls" in data and isinstance(data["tool_calls"], list):
                tool_calls = []
                for raw_tc in data["tool_calls"]:
                    args = raw_tc.get("arguments", {})
                    args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                    
                    tc = {
                        "id": "call_" + uuid.uuid4().hex[:8],
                        "type": "function",
                        "function": {
                            "name": raw_tc.get("name"),
                            "arguments": args_str
                        }
                    }
                    tool_calls.append(tc)
                # Clean up text by removing the JSON block
                clean_text = text[:start] + text[end+1:]
                clean_text = clean_text.strip()
                return clean_text or None, tool_calls
        except Exception:
            pass
            
    return text, None
