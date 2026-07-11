import sys
import httpx
import json

def main():
    base_url = "http://127.0.0.1:8001/v1"
    headers = {"Authorization": "Bearer not-needed"}
    
    # 1. List the models first
    print("=== Listing Models from API at port 8001 ===")
    try:
        r = httpx.get(f"{base_url}/models", headers=headers, timeout=10.0)
        if r.status_code != 200:
            print(f"Error: Server returned status {r.status_code}")
            print(r.text)
            return
        data = r.json()
        models = data.get("data", [])
        for m in models:
            print(f"- Model ID: {m.get('id')} (owned by {m.get('owned_by')})")
    except Exception as e:
        print(f"Error fetching models: {e}")
        return

    # 2. Select the first model as default
    model_name = models[0].get("id") if models else "DeepSeek V4"
    print(f"\n=== Sending Streaming Chat Request using model '{model_name}' ===")
    
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Dí hola y preséntate brevemente en español."}],
        "stream": True
    }
    
    try:
        with httpx.stream("POST", f"{base_url}/chat/completions", headers=headers, json=payload, timeout=30.0) as r:
            if r.status_code != 200:
                print(f"Server returned status {r.status_code}")
                # read and print the body
                r.read()
                print(r.text)
                return
                
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    payload_str = line[len("data: "):]
                    if payload_str == "[DONE]":
                        print("\n\n=== Stream Finished ===")
                        break
                    try:
                        chunk = json.loads(payload_str)
                        delta = chunk["choices"][0]["delta"]
                        
                        # Print content if present
                        if "content" in delta and delta["content"]:
                            print(delta["content"], end="", flush=True)
                            
                        # Print tool calls if present
                        if "tool_calls" in delta and delta["tool_calls"]:
                            print("\n[Tool Call Detected!]")
                            for tc in delta["tool_calls"]:
                                func = tc.get("function", {})
                                print(f"  - Function: {func.get('name')}")
                                if func.get("arguments"):
                                    print(f"    Arguments: {func.get('arguments')}")
                    except Exception as e:
                        # Print raw payload if json parsing fails
                        print(f"\n[Raw Chunk]: {payload_str}")
    except Exception as e:
        print(f"\nError during streaming completion: {e}")

if __name__ == "__main__":
    main()
