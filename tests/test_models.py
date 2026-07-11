import httpx
import json
from config import BASE_URL, API_KEY

def test_models():
    print(f"Testing GET {BASE_URL}/models ...")
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        r = httpx.get(f"{BASE_URL}/models", headers=headers, timeout=10.0)
        print("Status Code:", r.status_code)
        data = r.json()
        print("Response JSON:\n", json.dumps(data, indent=2))
        assert r.status_code == 200
        assert data["object"] == "list"
        assert len(data["data"]) > 0
        print("-> GET /models PASSED!")
    except Exception as e:
        print("-> GET /models FAILED:", e)

if __name__ == "__main__":
    test_models()
