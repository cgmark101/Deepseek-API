import json
import re
import uuid
from typing import List, Tuple, Optional
from .schemas import ChatMessage

SYSTEM_TOOL_PROMPT = """[SYSTEM INSTRUCTION: TOOL CALLING EMULATION]
You have access to the following tools/functions that you can call if needed:
{tools_json}

If you decide that you need to call one of the tools above to answer the user request, you MUST respond ONLY with a tool call in the following XML format:
<tool_call>
{{"name": "function_name", "arguments": {{"parameter_name": "value"}}}}
</tool_call>

IMPORTANT RULES:
1. Do NOT explain your choice or add any other text before, after, or around the <tool_call>...</tool_call> tags.
2. The content inside the <tool_call> tags MUST be a single, valid JSON object matching the tool schema.
3. If no tool is needed, respond normally with plain text.
"""


def inject_tools_instruction(messages: List[ChatMessage], tools: List[dict]) -> List[ChatMessage]:
    """Inject tool descriptions and execution instructions into the messages history."""
    if not tools:
        return messages
    
    # Format the tools cleanly as JSON
    tools_json = json.dumps(tools, indent=2, ensure_ascii=False)
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
    """Parse static text response for tool calls (XML tag or raw JSON block).
    
    Returns:
        Tuple[clean_text, tool_calls_list]
        If no tool call is found, returns (text, None).
    """
    if not text:
        return text, None
        
    # Try XML tag first
    match = re.search(r'<tool_call>(.*?)</tool_call>', text, re.DOTALL)
    json_str = None
    matched_text = None
    
    if match:
        json_str = match.group(1).strip()
        matched_text = match.group(0)
    else:
        # Try finding any JSON block containing "tool" or "name" with "arguments"
        json_match = re.search(r'(\{.*\})', text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
            matched_text = json_match.group(0)
            
    if not json_str:
        return text, None
        
    try:
        data = json.loads(json_str)
        func_name = data.get("name") or data.get("tool")
        arguments = data.get("arguments")
        
        if func_name and arguments is not None:
            # Build the OpenAI tool call format
            tool_call = {
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments)
                }
            }
            # Clean up text by removing the matched block
            clean_text = text.replace(matched_text, "").strip()
            return clean_text or None, [tool_call]
    except Exception:
        pass
        
    return text, None


class StreamToolParser:
    def __init__(self):
        self.in_tool_call = False
        self.start_with_json = False
        self.tool_call_id = None
        self.tag_buffer = ""
        self.json_buffer = ""
        self.sent_args_len = 0
        
        # Target start/end tags
        self.start_tag = "<tool_call>"
        self.end_tag = "</tool_call>"
        
        # To yield parsed events
        self.function_name = ""
        self.has_sent_initial_delta = False
        
    def feed(self, chunk: str) -> List[dict]:
        """Feed a text chunk into the parser.
        
        Returns a list of OpenAI choices delta dicts to yield.
        """
        deltas = []
        
        # Case 1: We are already inside the tool call tags/JSON block.
        if self.in_tool_call:
            self.json_buffer += chunk
            
            if self.start_with_json:
                # In raw JSON mode, we check periodically and finalize at flush time.
                parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=False)
                deltas.extend(parsed_deltas)
            else:
                # XML mode: check for end tag
                if self.end_tag in self.json_buffer:
                    json_part, remainder = self.json_buffer.split(self.end_tag, 1)
                    self.json_buffer = remainder
                    self.in_tool_call = False
                    
                    parsed_deltas = self._parse_json_buffer(json_part, finalize=True)
                    deltas.extend(parsed_deltas)
                else:
                    parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=False)
                    deltas.extend(parsed_deltas)
                    
            return deltas
            
        # Case 2: We are not in a tool call, but we might be starting one.
        self.tag_buffer += chunk
        
        # If the tag buffer starts with '{' (ignoring leading whitespace), check for raw JSON tool call
        stripped_buffer = self.tag_buffer.strip()
        if stripped_buffer.startswith("{"):
            if '"name"' in stripped_buffer or '"tool"' in stripped_buffer:
                self.in_tool_call = True
                self.start_with_json = True
                self.tool_call_id = "call_" + uuid.uuid4().hex[:12]
                self.function_name = ""
                self.has_sent_initial_delta = False
                self.sent_args_len = 0
                self.json_buffer = stripped_buffer
                self.tag_buffer = ""
                
                parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=False)
                deltas.extend(parsed_deltas)
                return deltas
            elif len(stripped_buffer) > 100:
                # Flush as normal text if it grows too large without match
                deltas.append({"content": self.tag_buffer})
                self.tag_buffer = ""
            return deltas
            
        # Check if the start XML tag is fully matched
        if self.start_tag in self.tag_buffer:
            self.in_tool_call = True
            self.start_with_json = False
            self.tool_call_id = "call_" + uuid.uuid4().hex[:12]
            self.function_name = ""
            self.has_sent_initial_delta = False
            self.sent_args_len = 0
            
            before_tag, remainder = self.tag_buffer.split(self.start_tag, 1)
            if before_tag:
                deltas.append({"content": before_tag})
                
            self.tag_buffer = ""
            self.json_buffer = remainder
            if remainder:
                parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=False)
                deltas.extend(parsed_deltas)
            return deltas
            
        # Check if the tag buffer is a prefix of the start tag.
        if not self.start_tag.startswith(self.tag_buffer) and not self.tag_buffer.strip().startswith("{"):
            deltas.append({"content": self.tag_buffer})
            self.tag_buffer = ""
            
        return deltas

    def flush(self) -> List[dict]:
        """Call this at the end of the stream to yield any remaining buffered text."""
        deltas = []
        if self.in_tool_call and self.start_with_json:
            self.in_tool_call = False
            parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=True)
            deltas.extend(parsed_deltas)
            self.json_buffer = ""
        elif self.tag_buffer:
            deltas.append({"content": self.tag_buffer})
            self.tag_buffer = ""
        return deltas

    def _parse_json_buffer(self, json_str: str, finalize: bool = False) -> List[dict]:
        """Try to extract name and arguments from the accumulated JSON string."""
        deltas = []
        
        if not self.has_sent_initial_delta:
            # Look for "name" or "tool"
            match = re.search(r'"(?:name|tool)"\s*:\s*"([^"]*)"', json_str)
            if match:
                self.function_name = match.group(1)
                
            if self.function_name or finalize:
                self.has_sent_initial_delta = True
                deltas.append({
                    "tool_calls": [{
                        "index": 0,
                        "id": self.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": self.function_name or "unknown",
                            "arguments": ""
                        }
                    }]
                })
                
        # Now, extract the arguments string.
        match_args = re.search(r'"arguments"\s*:\s*', json_str)
        if match_args:
            args_start = match_args.end()
            args_content = json_str[args_start:].strip()
            
            if finalize:
                if args_content.endswith("}"):
                    # Strip the final closing brace of the outer object
                    args_content = args_content[:-1].rstrip()
                    
            if args_content:
                new_args_slice = args_content[self.sent_args_len:]
                if new_args_slice:
                    self.sent_args_len += len(new_args_slice)
                    deltas.append({
                        "tool_calls": [{
                            "index": 0,
                            "function": {
                                "arguments": new_args_slice
                            }
                        }]
                    })
                    
        return deltas
