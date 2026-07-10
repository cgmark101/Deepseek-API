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
    """Parse static text response for tool calls.
    
    Returns:
        Tuple[clean_text, tool_calls_list]
        If no tool call is found, returns (text, None).
    """
    if not text:
        return text, None
        
    match = re.search(r'<tool_call>(.*?)</tool_call>', text, re.DOTALL)
    if not match:
        return text, None
        
    json_str = match.group(1).strip()
    try:
        call_data = json.loads(json_str)
        func_name = call_data.get("name")
        arguments = call_data.get("arguments", {})
        
        # Build the OpenAI tool call format
        tool_call = {
            "id": "call_" + uuid.uuid4().hex[:12],
            "type": "function",
            "function": {
                "name": func_name,
                "arguments": json.dumps(arguments, ensure_ascii=False)
            }
        }
        # Clean up text by removing the tag block
        clean_text = text.replace(match.group(0), "").strip()
        return clean_text or None, [tool_call]
    except Exception:
        # If it contains the tag but is invalid JSON, return it as normal text
        return text, None


class StreamToolParser:
    def __init__(self):
        self.in_tool_call = False
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
        
        # Case 1: We are already inside the tool call tags.
        if self.in_tool_call:
            self.json_buffer += chunk
            
            # Check if we hit the end tag
            if self.end_tag in self.json_buffer:
                # Split at end tag
                json_part, remainder = self.json_buffer.split(self.end_tag, 1)
                self.json_buffer = remainder
                self.in_tool_call = False
                
                # Parse the final JSON block
                parsed_deltas = self._parse_json_buffer(json_part, finalize=True)
                deltas.extend(parsed_deltas)
            else:
                # Periodically parse the json buffer to extract function name and arguments
                parsed_deltas = self._parse_json_buffer(self.json_buffer, finalize=False)
                deltas.extend(parsed_deltas)
                
            return deltas
            
        # Case 2: We are not in a tool call, but we might be starting one.
        self.tag_buffer += chunk
        
        # Check if the start tag is fully matched
        if self.start_tag in self.tag_buffer:
            self.in_tool_call = True
            self.tool_call_id = "call_" + uuid.uuid4().hex[:12]
            self.function_name = ""
            self.has_sent_initial_delta = False
            self.sent_args_len = 0
            
            # The part before the start tag is normal content
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
        # If it is NOT, it means it's regular text, so flush it!
        if not self.start_tag.startswith(self.tag_buffer):
            deltas.append({"content": self.tag_buffer})
            self.tag_buffer = ""
            
        return deltas

    def flush(self) -> List[dict]:
        """Call this at the end of the stream to yield any remaining buffered text."""
        deltas = []
        if self.tag_buffer:
            deltas.append({"content": self.tag_buffer})
            self.tag_buffer = ""
        return deltas

    def _parse_json_buffer(self, json_str: str, finalize: bool = False) -> List[dict]:
        """Try to extract name and arguments from the accumulated JSON string."""
        deltas = []
        
        if not self.has_sent_initial_delta:
            # Look for "name"\s*:\s*"([^"]+)"
            match = re.search(r'"name"\s*:\s*"([^"]*)"', json_str)
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
