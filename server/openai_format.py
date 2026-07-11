"""Translate between OpenAI's chat-completions shapes and our DeepSeek client.

DeepSeek's protocol has no system/role channel — just a single `prompt` string.
So we flatten the OpenAI `messages` array into one prompt, and wrap DeepSeek's
text output back into OpenAI response/stream objects.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Iterable, List

from .schemas import ChatMessage

from typing import Iterable, List, Optional

# ... rest of helper imports ...
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant"}


def _text_of(content) -> str:
    """Extract plain text from a message's content (string or list-of-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


def messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Flatten a chat history into a single prompt DeepSeek can answer.

    A lone user message is sent verbatim. Multi-turn / system-prompted
    conversations are serialised with role labels and a trailing 'Assistant:'
    cue so the model continues in the right voice.
    """
    if len(messages) == 1 and messages[0].role == "user":
        return _text_of(messages[0].content)

    lines = []
    for m in messages:
        label = _ROLE_LABELS.get(m.role, m.role.capitalize())
        lines.append(f"{label}: {_text_of(m.content)}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) — DeepSeek's web API gives us no count."""
    return max(1, len(text) // 4)


def completion_response(model: str, content: Optional[str], prompt: str,
                        conversation_id: str = None, reasoning_content: str = None,
                        tool_calls: Optional[List[dict]] = None) -> dict:
    """A full (non-streaming) OpenAI chat.completion object.

    `conversation_id` is an extra top-level field (outside OpenAI's schema) you
    send back to resume the conversation.
    """
    pt = _est_tokens(prompt)
    ct = _est_tokens(content or "")
    
    message = {"role": "assistant", "content": content}
    finish_reason = "stop"
    
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
        
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
        finish_reason = "tool_calls"
        
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def stream_chunks(model: str, stream: Iterable[tuple[str, str]], emulate_tools: bool = False) -> Iterable[str]:
    """Yield OpenAI SSE lines (`data: {...}\n\n`) for a streamed completion.

    `stream` is the client's stream object yielding (type, text) tuples.
    """
    cid, created = _id(), _now()

    def frame(delta: dict, finish=None, extra: dict = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # First frame announces the assistant role.
    yield frame({"role": "assistant"})
    
    finish_reason = "stop"
    stream_iterator = iter(stream)
    
    if emulate_tools:
        buffer_text = ""
        is_tool_call = False
        detection_chars_needed = 200
        
        # Pull chunks until we can determine if it starts a tool call
        for chunk_type, d in stream_iterator:
            if not d:
                continue
            if chunk_type == "thinking":
                yield frame({"reasoning_content": d})
            else:
                buffer_text += d
                # If we have gathered enough content characters, check for tool call
                if len(buffer_text) >= detection_chars_needed:
                    break
                    
        stripped = buffer_text.strip()
        if ("```" in stripped or "{" in stripped) and ("tool_calls" in stripped or "name" in stripped):
            is_tool_call = True
            
        if is_tool_call:
            # Buffer the rest of the stream
            for chunk_type, d in stream_iterator:
                if d and chunk_type == "content":
                    buffer_text += d
                    
            # Parse the tool call from the complete buffer
            tool_calls = None
            start = buffer_text.find("{")
            end = buffer_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(buffer_text[start:end+1])
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
                except Exception as e:
                    print(f"[server] Warning: Failed to parse tool call JSON in stream: {e}")
                    
            if tool_calls:
                # Phase 1: Emit metadata and function name
                init_tc_list = []
                for idx, tc in enumerate(tool_calls):
                    init_tc_list.append({
                        "index": idx,
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": ""
                        }
                    })
                yield frame({"role": "assistant", "tool_calls": init_tc_list})
                
                # Phase 2: Emit arguments string
                args_tc_list = []
                for idx, tc in enumerate(tool_calls):
                    args_tc_list.append({
                        "index": idx,
                        "function": {
                            "arguments": tc["function"]["arguments"]
                        }
                    })
                yield frame({"tool_calls": args_tc_list})
                finish_reason = "tool_calls"
            else:
                # False alarm: yield buffered text first as content
                if buffer_text:
                    yield frame({"content": buffer_text})
        else:
            # Yield buffered text first
            if buffer_text:
                yield frame({"content": buffer_text})
            # Continue streaming normally
            for chunk_type, d in stream_iterator:
                if d:
                    if chunk_type == "thinking":
                        yield frame({"reasoning_content": d})
                    else:
                        yield frame({"content": d})
    else:
        for chunk_type, d in stream_iterator:
            if d:
                if chunk_type == "thinking":
                    yield frame({"reasoning_content": d})
                else:
                    yield frame({"content": d})
                    
    conversation_id = getattr(stream, "conversation_id", None)
    yield frame({}, finish=finish_reason, extra={"conversation_id": conversation_id})
    yield "data: [DONE]\n\n"
