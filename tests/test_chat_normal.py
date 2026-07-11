import httpx
import json
from config import BASE_URL, API_KEY

def test_chat_normal():
    print(f"Testing POST {BASE_URL}/chat/completions (stream=False, no tools) ...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Dí hola de nuevo de forma concisa."}],
        "stream": False
    }
    try:
        r = httpx.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60.0)
        print("Status Code:", r.status_code)
        data = r.json()
        print("Response JSON:\n", json.dumps(data, indent=2))
        assert r.status_code == 200
        choice = data["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert len(choice["message"]["content"]) > 0
        assert choice["finish_reason"] == "stop"
        print("-> Chat Normal PASSED!")
    except Exception as e:
        print("-> Chat Normal FAILED:", e)

if __name__ == "__main__":
    test_chat_normal()
