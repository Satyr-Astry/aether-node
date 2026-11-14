"""最小 OpenAI 兼容 mock，用于验证「自有外部 API」路由。

只实现 /v1/models 与 /v1/chat/completions，不校验鉴权（鉴权由调用方日志验证）。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
ANSWER = "这是来自外部 API 的回答。"
PROMPT_TOKENS = 11
COMPLETION_TOKENS = 7


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict, code: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send({
                "object": "list",
                "data": [{"id": "mock-model", "object": "model", "created": 0, "owned_by": "mock"}],
            })
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if not self.path.startswith("/v1/chat/completions"):
            self._send({"error": "not found"}, 404)
            return
        auth = self.headers.get("authorization") or ""
        self._send({
            "id": "mock-1",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model") or "mock-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": PROMPT_TOKENS,
                "completion_tokens": COMPLETION_TOKENS,
                "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            },
            "mock_auth_seen": bool(auth),
        })

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"mock listening on {PORT}", flush=True)
    # 用多线程版本：压测时单线程 server 会成为瓶颈，测出来的延迟不是代理层的
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
