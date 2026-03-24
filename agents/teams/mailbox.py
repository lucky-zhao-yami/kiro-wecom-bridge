"""Mailbox — JSONL 消息箱，支持按收件人过滤"""
import os, time

from agents.teams.jsonl_store import JsonlStore


class Mailbox(JsonlStore):

    def __init__(self, session_dir: str):
        super().__init__(os.path.join(session_dir, "mailbox.jsonl"))

    def send(self, from_agent: str, to_agent: str, content: str):
        def _do():
            msgs = self._read_all()
            msgs.append({"from": from_agent, "to": to_agent, "content": content,
                          "ts": int(time.time()), "read_by": []})
            self._write_all(msgs)
        self._with_lock(_do)

    def read_for(self, agent_name: str, mark_read: bool = True) -> list[dict]:
        result = []
        def _do():
            msgs = self._read_all()
            for m in msgs:
                if (m["to"] == agent_name or m["to"] == "all") and agent_name not in m.get("read_by", []):
                    result.append(m)
                    if mark_read:
                        m.setdefault("read_by", []).append(agent_name)
            if mark_read and result:
                self._write_all(msgs)
        self._with_lock(_do)
        return result

    def read_all(self) -> list[dict]:
        return self._with_lock(self._read_all)
