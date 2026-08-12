from fastapi import FastAPI
import json
import os
from pathlib import Path

app = FastAPI()

_ROOT = Path(__file__).resolve().parent
_REGISTRY_PATH = _ROOT / "mock_remote_registry.json"

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/resources")
async def resources():
    payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    serialized = json.dumps(payload)
    serialized = serialized.replace(
        "http://127.0.0.1:8010",
        f"http://127.0.0.1:{int(os.getenv('ANNUAL_LEAVE_REMOTE_AGENT_PORT', '8010'))}",
    )
    serialized = serialized.replace(
        "http://127.0.0.1:8011",
        f"http://127.0.0.1:{int(os.getenv('ANNUAL_LEAVE_REMOTE_TOOL_PORT', '8011'))}",
    )
    return json.loads(serialized)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("ANNUAL_LEAVE_REMOTE_REGISTRY_PORT", "8012")),
    )
