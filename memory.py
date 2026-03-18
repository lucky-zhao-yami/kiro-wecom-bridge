"""L3 知识图谱：SQLite 实体关系 + sqlite-vec 向量检索"""
import json, logging, os, sqlite3, time

log = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("embedding 模型加载完成")
    return _model


def _embed(text: str) -> list[float]:
    return _get_model().encode(text).tolist()


class MemoryDB:
    def __init__(self, db_path: str):
        self._path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                properties TEXT DEFAULT '{}',
                version INTEGER DEFAULT 1,
                updated_at INTEGER,
                source_chatid TEXT
            );
            CREATE TABLE IF NOT EXISTS entity_versions (
                entity_id TEXT,
                version INTEGER,
                description TEXT,
                properties TEXT,
                changed_at INTEGER,
                reason TEXT,
                PRIMARY KEY (entity_id, version)
            );
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                to_id TEXT NOT NULL,
                properties TEXT DEFAULT '{}',
                valid_from INTEGER,
                valid_to INTEGER,
                source_chatid TEXT
            );
        """)
        # FTS5
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(name, description, properties, content=entities, content_rowid=rowid)")
        except Exception:
            pass
        # sqlite-vec
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entities_vec USING vec0(id TEXT PRIMARY KEY, embedding float[384])")
            self._has_vec = True
        except Exception as e:
            log.warning("sqlite-vec 不可用，仅使用 FTS 检索: %s", e)
            self._has_vec = False
        c.commit()

    def save_entity(self, type: str, name: str, description: str,
                    properties: dict | None = None, source_chatid: str = "", reason: str = "") -> str:
        entity_id = f"{type}:{name}"
        now = int(time.time())
        props_json = json.dumps(properties or {}, ensure_ascii=False)
        existing = self._conn.execute("SELECT version, description, properties FROM entities WHERE id = ?", (entity_id,)).fetchone()

        if existing:
            old_ver = existing["version"]
            # 归档旧版本
            self._conn.execute(
                "INSERT OR REPLACE INTO entity_versions (entity_id, version, description, properties, changed_at, reason) VALUES (?,?,?,?,?,?)",
                (entity_id, old_ver, existing["description"], existing["properties"], now, reason)
            )
            new_ver = old_ver + 1
            self._conn.execute(
                "UPDATE entities SET description=?, properties=?, version=?, updated_at=?, source_chatid=? WHERE id=?",
                (description, props_json, new_ver, now, source_chatid, entity_id)
            )
        else:
            self._conn.execute(
                "INSERT INTO entities (id, type, name, description, properties, version, updated_at, source_chatid) VALUES (?,?,?,?,?,1,?,?)",
                (entity_id, type, name, description, props_json, now, source_chatid)
            )

        # 更新 FTS
        self._conn.execute("INSERT OR REPLACE INTO entities_fts(rowid, name, description, properties) SELECT rowid, name, description, properties FROM entities WHERE id=?", (entity_id,))

        # 更新向量
        if self._has_vec and description:
            emb = _embed(description)
            self._conn.execute("INSERT OR REPLACE INTO entities_vec(id, embedding) VALUES (?, ?)", (entity_id, json.dumps(emb)))

        self._conn.commit()
        return entity_id

    def save_relation(self, from_name: str, relation: str, to_name: str,
                      from_type: str = "", to_type: str = "",
                      properties: dict | None = None, source_chatid: str = ""):
        from_id = f"{from_type}:{from_name}" if from_type else self._guess_entity_id(from_name)
        to_id = f"{to_type}:{to_name}" if to_type else self._guess_entity_id(to_name)
        now = int(time.time())

        # 同类旧关系失效
        self._conn.execute(
            "UPDATE relations SET valid_to=? WHERE from_id=? AND relation=? AND to_id=? AND valid_to IS NULL",
            (now, from_id, relation, to_id)
        )
        self._conn.execute(
            "INSERT INTO relations (from_id, relation, to_id, properties, valid_from, source_chatid) VALUES (?,?,?,?,?,?)",
            (from_id, relation, to_id, json.dumps(properties or {}, ensure_ascii=False), now, source_chatid)
        )
        self._conn.commit()

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        results = {}

        # FTS 搜索
        try:
            rows = self._conn.execute(
                "SELECT e.* FROM entities_fts f JOIN entities e ON f.rowid = e.rowid WHERE entities_fts MATCH ? LIMIT ?",
                (query, top_k)
            ).fetchall()
            for r in rows:
                results[r["id"]] = {"entity": dict(r), "score": 1.0, "source": "fts"}
        except Exception:
            pass

        # 向量搜索
        if self._has_vec:
            try:
                emb = _embed(query)
                rows = self._conn.execute(
                    "SELECT id, distance FROM entities_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                    (json.dumps(emb), top_k)
                ).fetchall()
                for r in rows:
                    eid = r["id"]
                    if eid not in results:
                        entity = self._conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
                        if entity:
                            results[eid] = {"entity": dict(entity), "score": 1.0 - r["distance"], "source": "vec"}
            except Exception as e:
                log.warning("向量搜索失败: %s", e)

        # 加载关联关系
        output = []
        for eid, info in results.items():
            rels = self._conn.execute(
                "SELECT * FROM relations WHERE (from_id=? OR to_id=?) AND valid_to IS NULL",
                (eid, eid)
            ).fetchall()
            info["relations"] = [dict(r) for r in rels]
            output.append(info)

        return output

    def get_history(self, entity_name: str) -> list[dict]:
        # 模糊匹配 entity_id
        eid = self._guess_entity_id(entity_name)
        rows = self._conn.execute(
            "SELECT * FROM entity_versions WHERE entity_id=? ORDER BY version",
            (eid,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _guess_entity_id(self, name: str) -> str:
        row = self._conn.execute("SELECT id FROM entities WHERE name=? LIMIT 1", (name,)).fetchone()
        return row["id"] if row else name

    def close(self):
        self._conn.close()
