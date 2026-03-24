#!/usr/bin/env python3
"""记忆系统 HTTP 服务 — 常驻进程，避免每次 spawn Python"""
import json, os, sys, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory import MemoryDB

SESSIONS_DIR = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all"), "wecom-sessions")
PORT = int(os.getenv("MEMORY_PORT", "8901"))

# 缓存 DB 连接，按 chatid
_db_cache: dict[str, MemoryDB] = {}


def get_db(chatid: str) -> MemoryDB:
    if chatid not in _db_cache:
        _db_cache[chatid] = MemoryDB(os.path.join(SESSIONS_DIR, chatid, "memory.db"))
    return _db_cache[chatid]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        chatid = body.get("chatid", "")
        if not chatid:
            return self._json(400, {"error": "chatid required"})

        action = self.path.strip("/")
        args = body.get("args", {})
        db = get_db(chatid)

        try:
            if action == "search":
                result = db.search(args["query"], top_k=args.get("top_k", 5))
            elif action == "save_entity":
                eid = db.save_entity(
                    type=args["type"], name=args["name"], description=args["description"],
                    properties=args.get("properties"), source_chatid=chatid, reason=args.get("reason", ""))
                result = {"saved": eid}
            elif action == "save_relation":
                db.save_relation(
                    from_name=args["from_name"], relation=args["relation"], to_name=args["to_name"],
                    from_type=args.get("from_type", ""), to_type=args.get("to_type", ""),
                    source_chatid=chatid)
                result = {"saved": f"{args['from_name']} -{args['relation']}-> {args['to_name']}"}
            elif action == "delete_entity":
                ok = db.delete_entity(args["name"])
                result = {"deleted": ok, "name": args["name"]}
            elif action == "delete_relation":
                ok = db.delete_relation(args["from_name"], args["relation"], args["to_name"])
                result = {"deleted": ok}
            elif action == "get_history":
                result = db.get_history(args["entity_name"])
            else:
                return self._json(400, {"error": f"unknown action: {action}"})

            self._json(200, result)
        except KeyError as e:
            self._json(400, {"error": f"missing field: {e}"})
        except Exception as e:
            self._json(500, {"error": str(e), "trace": traceback.format_exc()})

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        pass  # 静默日志


if __name__ == "__main__":
    print(f"Memory service starting on :{PORT}")
    # 预热：加载 embedding 模型
    get_db("_warmup")
    print(f"Memory service ready on :{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
