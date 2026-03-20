"""FunASR HTTP 语音识别服务 — 运行在 Docker 容器内"""
import base64, io, json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

MODEL_DIR = os.getenv("MODEL_DIR", "/workspace/models/SenseVoiceSmall")
PORT = int(os.getenv("PORT", "10095"))

model = None

def get_model():
    global model
    if model is None:
        from funasr import AutoModel
        model = AutoModel(model=MODEL_DIR, device="cpu")
        print(f"模型加载完成: {MODEL_DIR}", flush=True)
    return model

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/v1/asr":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        audio_b64 = body.get("audio", "")
        if not audio_b64:
            self._json(400, {"error": "missing audio"})
            return
        audio_bytes = base64.b64decode(audio_b64)
        # 写临时文件（funasr 需要文件路径）
        tmp = "/tmp/asr_input.audio"
        with open(tmp, "wb") as f:
            f.write(audio_bytes)
        try:
            result = get_model().generate(input=tmp)
            text = result[0]["text"] if result else ""
            self._json(200, {"text": text})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self.send_error(404)

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[ASR] {args[0]}", flush=True)

if __name__ == "__main__":
    print(f"预加载模型...", flush=True)
    get_model()
    print(f"FunASR HTTP 服务启动 port={PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
