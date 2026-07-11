import httpx
import json
from config import BASE_URL, API_KEY

def test_chat_tools_static():
    print(f"Testing POST {BASE_URL}/chat/completions (stream=False, with tools) ...")
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
        "stream": False
    }
    try:
        r = httpx.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60.0)
        print("Status Code:", r.status_code)
        data = r.json()
        print("Response JSON:\n", json.dumps(data, indent=2))
        assert r.status_code == 200
        
        choice = data["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        message = choice["message"]
        assert message["content"] is None or message["content"] == ""
        assert len(message["tool_calls"]) == 1
        tc = message["tool_calls"][0]
        assert tc["function"]["name"] == "bash"
        assert "arguments" in tc["function"]
        print("-> Chat Tools Static PASSED!")
    except Exception as e:
        print("-> Chat Tools Static FAILED:", e)

if __name__ == "__main__":
    test_chat_tools_static()
