import httpx
import json
from config import BASE_URL, API_KEY

def test_chat_stream():
    print(f"Testing POST {BASE_URL}/chat/completions (stream=True, no tools) ...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Dí hola de nuevo de forma concisa."}],
        "stream": True
    }
    try:
        with httpx.stream("POST", f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60.0) as r:
            print("Status Code:", r.status_code)
            assert r.status_code == 200
            
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
                    assert data["choices"][0]["index"] == 0
                    
        print("-> Chat Stream PASSED!")
    except Exception as e:
        print("-> Chat Stream FAILED:", e)

if __name__ == "__main__":
    test_chat_stream()
