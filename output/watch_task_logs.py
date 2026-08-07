"""Watch for new task logs while the user replays the request in the Web UI."""
import glob
import json
import os
import time

TASK_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "store", "task_logs")
KNOWN = set(glob.glob(os.path.join(TASK_LOG_DIR, "*.json")))


def summarize(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return f"  (cannot read: {exc})"
    fields = {}
    for key in ("task_id", "workflow_id", "status", "error", "user_id", "created_at"):
        if key in data:
            fields[key] = data[key]
    return json.dumps(fields, ensure_ascii=False, default=str)[:800]


print("Watching", TASK_LOG_DIR, flush=True)
print("Existing logs:", len(KNOWN), flush=True)
seen = 0
while seen < 3:
    current = set(glob.glob(os.path.join(TASK_LOG_DIR, "*.json")))
    new = current - KNOWN
    for path in sorted(new):
        print(f"\n[NEW TASK LOG] {os.path.basename(path)}", flush=True)
        print(summarize(path), flush=True)
        seen += 1
    KNOWN |= new
    time.sleep(2)
print("\nDONE watching (3 new logs seen)", flush=True)
