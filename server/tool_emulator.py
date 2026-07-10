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


def detect_tool_call_in_text(text: str) -> Optional[dict]:
    """Check if the text contains a complete or starting tool call.
    
    Returns a dict with metadata if found, else None.
    """
    # 1. XML check first
    xml_match = re.search(r'<tool_call>(.*?)(?:</tool_call>|$)', text, re.DOTALL)
    if xml_match:
        content = xml_match.group(1).strip()
        is_complete = '</tool_call>' in text
        return {"format": "xml", "content": content, "is_complete": is_complete, "matched_text": xml_match.group(0)}
        
    # 2. Extract from first '{' to last '}'
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    
    if first_brace != -1:
        has_tool_key = '"tool"' in text or '"name"' in text
        
        if last_brace != -1 and last_brace > first_brace:
            content = text[first_brace:last_brace+1].strip()
            if has_tool_key:
                is_complete = False
                try:
                    json.loads(content)
                    is_complete = True
                except ValueError:
                    pass
                matched_text = text[first_brace:last_brace+1]
                return {"format": "json", "content": content, "is_complete": is_complete, "matched_text": matched_text}
                
        # If it hasn't closed yet but contains the tool key, it's a partial tool call that we can start streaming!
        if has_tool_key and text.strip().startswith("{") and len(text.strip()) < 150:
            content = text[first_brace:].strip()
            return {"format": "json", "content": content, "is_complete": False, "matched_text": text[first_brace:]}
            
        # If it starts with a brace but doesn't have the key yet, keep buffering as partial
        if text.strip().startswith("{") and len(text.strip()) < 150:
            return {"format": "partial"}
            
    # Check if XML or markdown block is starting
    if "<tool_call" in text or "```json" in text or "```xml" in text:
        return {"format": "partial"}
        
    return None


def parse_static_tool_call(text: str) -> Tuple[Optional[str], Optional[List[dict]]]:
    """Parse static text response for tool calls (XML tag, markdown JSON, or raw JSON).
    
    Returns:
        Tuple[clean_text, tool_calls_list]
        If no tool call is found, returns (text, None).
    """
    if not text:
        return text, None
        
    detection = detect_tool_call_in_text(text)
    if not detection or detection["format"] == "partial":
        return text, None
        
    content = detection["content"]
    matched_text = detection["matched_text"]
    
    try:
        data = json.loads(content)
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
        self.buffer = ""
        self.flushed = False
        self.in_tool_call = False
        self.tool_call_id = None
        self.function_name = ""
        self.has_sent_initial_delta = False
        self.sent_args_len = 0
        
    def feed(self, chunk: str) -> List[dict]:
        """Feed a text chunk into the parser.
        
        Returns a list of OpenAI choices delta dicts to yield.
        """
        deltas = []
        if self.flushed:
            return [{"content": chunk}]
            
        self.buffer += chunk
        
        detection = detect_tool_call_in_text(self.buffer)
        if detection:
            if detection["format"] == "partial":
                return []
                
            self.in_tool_call = True
            if self.tool_call_id is None:
                self.tool_call_id = "call_" + uuid.uuid4().hex[:12]
                
            content = detection["content"]
            is_complete = detection["is_complete"]
            
            # Emit normal content that came before the tool call
            matched_text = detection["matched_text"]
            before_text = self.buffer.split(matched_text, 1)[0]
            if before_text and not self.has_sent_initial_delta:
                deltas.append({"content": before_text})
                
            parsed_deltas = self._parse_json_buffer(content, finalize=is_complete)
            deltas.extend(parsed_deltas)
            
            if is_complete:
                self.in_tool_call = False
                self.flushed = True
                self.buffer = ""
            return deltas
            
        # If no tool call is detected and the buffer is larger than 250 characters
        if len(self.buffer) > 250:
            self.flushed = True
            deltas.append({"content": self.buffer})
            self.buffer = ""
            
        return deltas

    def flush(self) -> List[dict]:
        """Call this at the end of the stream to yield any remaining buffered text."""
        deltas = []
        if self.in_tool_call:
            detection = detect_tool_call_in_text(self.buffer)
            if detection and detection["format"] != "partial":
                parsed_deltas = self._parse_json_buffer(detection["content"], finalize=True)
                deltas.extend(parsed_deltas)
            self.in_tool_call = False
            self.buffer = ""
        elif self.buffer:
            deltas.append({"content": self.buffer})
            self.buffer = ""
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
