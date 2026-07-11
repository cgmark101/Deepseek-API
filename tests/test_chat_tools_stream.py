import httpx
import json
from config import BASE_URL, API_KEY

def test_chat_tools_stream():
    print(f"Testing POST {BASE_URL}/chat/completions (stream=True, with tools) ...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Run the command echo hello"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute a command in the local shell",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"]
                }
            }
        }],
        "stream": True
    }
    try:
        with httpx.stream("POST", f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60.0) as r:
            print("Status Code:", r.status_code)
            assert r.status_code == 200
            
            chunks = []
            for line in r.iter_lines():
                if not line:
                    continue
                print(line)
                if line.startswith("data: "):
                    payload_str = line[len("data: "):]
                    if payload_str == "[DONE]":
                        print("-> Stream finished cleanly!")
                        break
                    data = json.loads(payload_str)
                    chunks.append(data)
                    
            print("\nValidating streaming tool call chunks...")
            # Validate last chunk finish reason is tool_calls
            assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
            # Verify tool_calls key exists in delta
            tool_calls_detected = False
            for c in chunks:
                delta = c["choices"][0]["delta"]
                if "tool_calls" in delta:
                    tool_calls_detected = True
                    break
            assert tool_calls_detected
            print("-> Chat Tools Stream PASSED!")
    except Exception as e:
        print("-> Chat Tools Stream FAILED:", e)

if __name__ == "__main__":
    test_chat_tools_stream()
