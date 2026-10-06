"""Talk to a local `llmgate dev` with the official OpenAI SDK (pip install openai).

    llmgate dev                      # in one terminal
    python examples/quickstart.py    # in another
"""

import json
from pathlib import Path

from openai import OpenAI

creds = json.loads(Path("data/dev-credentials.json").read_text())  # written by `llmgate dev` on first run
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=creds["api_key"])  # or /api/v1 (OpenRouter-style)

print("models:", [m.id for m in client.models.list().data])

reply = client.chat.completions.create(model="dev-chat", messages=[{"role": "user", "content": "hello"}], user="me")
print(reply.choices[0].message.content, "| tokens:", reply.usage.total_tokens, "| cost: $", reply.usage.cost)  # cost is a gateway extra

print("stream: ", end="")
for chunk in client.chat.completions.create(model="dev-chat", stream=True, messages=[{"role": "user", "content": "stream"}]):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
print()

# OpenRouter-style routing options go through extra_body; tags show up under "spend by tag" in the dashboard.
client.chat.completions.create(model="dev-chat", messages=[{"role": "user", "content": "hi"}],
                               extra_body={"provider": {"sort": "price"}, "tags": ["quickstart"], "cache": {"ttl": 60}})
print("now open http://127.0.0.1:8080/dashboard/ (admin token: dev-admin) and look at 사용 로그")
