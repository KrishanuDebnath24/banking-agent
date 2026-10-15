"""python -m api   ->  http://127.0.0.1:8000  (API_HOST / PORT to change)"""

import os

import uvicorn

if __name__ == "__main__":
    host, port = os.environ.get("API_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"Banking assistant: http://{host}:{port}   (the bank servers must be running: python -m mcp_servers.run)")
    uvicorn.run("api.app:app", host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "warning"))
