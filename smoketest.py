from dotenv import load_dotenv
import sys
import os
import httpx

load_dotenv()

api_key = os.getenv("FIREWORK_API_KEY")
url = os.getenv("FIREWORK_URL")
model = os.getenv("LLM_MODEL")


if len(sys.argv) != 2:
    message = input("Enter your message: ")
else:
    message = sys.argv[1:]


messages = [{"role": "user", "content": " ".join(message)}]

response = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, 
json={"model": model,
 "messages": messages},
 timeout=10.0)

print(response.json())