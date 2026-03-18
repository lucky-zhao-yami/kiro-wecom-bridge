#!/usr/bin/env python3
"""记忆系统 CLI — skill 通过 execute_bash 调用"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory import MemoryDB

SESSIONS_DIR = os.path.join(os.getenv("KIRO_WORK_DIR", "/mnt/d/workspace/all"), "wecom-sessions")
CHATID = os.getenv("MEMORY_CHATID", "")

if not CHATID:
    print(json.dumps({"error": "MEMORY_CHATID not set"}))
    sys.exit(1)

db = MemoryDB(os.path.join(SESSIONS_DIR, CHATID, "memory.db"))


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: memory_cli.py <action> '<json_args>'"}))
        sys.exit(1)

    action = sys.argv[1]
    args = json.loads(sys.argv[2])

    if action == "search":
        results = db.search(args["query"], top_k=args.get("top_k", 5))
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif action == "save_entity":
        eid = db.save_entity(
            type=args["type"], name=args["name"], description=args["description"],
            properties=args.get("properties"), source_chatid=CHATID, reason=args.get("reason", "")
        )
        print(json.dumps({"saved": eid}))

    elif action == "save_relation":
        db.save_relation(
            from_name=args["from_name"], relation=args["relation"], to_name=args["to_name"],
            from_type=args.get("from_type", ""), to_type=args.get("to_type", ""),
            source_chatid=CHATID
        )
        print(json.dumps({"saved": f"{args['from_name']} -{args['relation']}-> {args['to_name']}"}))

    elif action == "get_history":
        history = db.get_history(args["entity_name"])
        print(json.dumps(history, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"unknown action: {action}"}))
        sys.exit(1)

    db.close()


if __name__ == "__main__":
    main()
