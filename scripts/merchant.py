"""商户抽取与候选入库入口。

模型结果先保存到 ``merchant_extraction_candidates``，通过校验后由本脚本幂等
晋级到 ``merchants`` 和来源关系表。SQLite 表由 ``xhs_storage.connect`` 创建；
PostgreSQL 表由根目录的 ``schema.sql`` 创建。所有 Agent 配置从项目根目录
``.env`` 读取。

常用命令::

    python scripts/merchant.py init
    python scripts/merchant.py extract --limit 10
    python scripts/merchant.py import --input candidates.json
    python scripts/merchant.py promote
    python scripts/merchant.py validate --input candidates.json

输入结果只允许包含商户名称、别名、分类、城市/区域、价格、预约信息、关系和
证据。商户没有地址字段：连锁店和搬迁会让地址无法代表商户本身。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv(ROOT / ".env", encoding="utf-8")

import xhs_storage  # noqa: E402  (需要先把 scripts 加到 sys.path)


RELATION_TYPES = {"primary_review", "mentioned", "listicle", "comparison"}
PRICE_BASES = {"per_person", "set", "unknown"}
STATUSES = {"pending", "accepted", "rejected", "needs_review"}
ALLOWED_FIELDS = {
    "name",
    "aliases",
    "category_ids",
    "city",
    "district",
    "price",
    "reservation_info",
    "relation_type",
    "evidence",
    "confidence",
}


@dataclass(frozen=True)
class MerchantAgentConfig:
    """Agent 配置；优先使用商户专用变量，兼容通用 OpenAI 变量。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_s: int
    retries: int
    batch_size: int
    max_notes: int
    extractor_version: str
    prompt_version: str

    @classmethod
    def from_env(cls) -> "MerchantAgentConfig":
        def value(name: str, fallback: str = "") -> str:
            return os.getenv(name, fallback).strip()

        def integer(name: str, fallback: int, minimum: int = 1) -> int:
            raw = value(name, str(fallback))
            try:
                return max(minimum, int(raw))
            except ValueError as exc:
                raise ValueError(f"{name} 必须是整数，当前值为 {raw!r}") from exc

        provider = value("MERCHANT_AGENT_PROVIDER", value("AGENT_PROVIDER", "openai"))
        api_key = value("MERCHANT_AGENT_API_KEY", value("OPENAI_API_KEY"))
        base_url = value(
            "MERCHANT_AGENT_BASE_URL",
            value("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        model = value("MERCHANT_AGENT_MODEL", value("OPENAI_MODEL", "gpt-4o-mini"))
        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            timeout_s=integer("MERCHANT_AGENT_TIMEOUT_S", 60),
            retries=integer("MERCHANT_AGENT_RETRIES", 2, minimum=0),
            batch_size=integer("MERCHANT_AGENT_BATCH_SIZE", 5),
            max_notes=integer("MERCHANT_AGENT_MAX_NOTES", 100),
            extractor_version=value("MERCHANT_EXTRACTOR_VERSION", "v1"),
            prompt_version=value("MERCHANT_PROMPT_VERSION", "v1"),
        )


def note_text(note: sqlite3.Row | dict[str, Any]) -> str:
    """返回发送给 Agent 的最小公开文本，不包含 raw_json、Cookie 或 token。"""
    keys = set(note.keys()) if isinstance(note, sqlite3.Row) else set(note)
    title = str(note.get("title", "") if isinstance(note, dict) else note["title"] or "")
    content_key = "content" if "content" in keys else "description"
    if isinstance(note, sqlite3.Row):
        content = note[content_key] or ""
        topics = note["topics"] or "[]"
    else:
        content = note.get(content_key, "") or ""
        topics = note.get("topics", []) or []
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except (TypeError, json.JSONDecodeError):
            topics = []
    topic_text = " ".join(str(item) for item in topics if str(item).strip())
    return "\n".join(part for part in (title, str(content), topic_text) if part.strip())


def source_hash(note: sqlite3.Row | dict[str, Any]) -> str:
    existing = note["content_hash"] if isinstance(note, sqlite3.Row) else note.get("content_hash", "")
    if existing:
        return str(existing)
    return hashlib.sha256(note_text(note).encode("utf-8")).hexdigest()


def _contains(source: str, value: str) -> bool:
    if value in source:
        return True
    normalize = lambda text: re.sub(r"\s+", " ", text).strip()
    return normalize(value) in normalize(source)


def _contains_name(source: str, value: str) -> bool:
    """名称允许英文大小写差异；证据仍使用严格原文匹配。"""
    return _contains(source, value) or value.casefold() in source.casefold()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def validate_candidate(
    candidate: dict[str, Any],
    source: str,
    valid_categories: set[str] | None = None,
) -> list[str]:
    """校验一个候选，并返回全部错误；模型不能通过此函数直接写正式表。"""
    errors: list[str] = []
    unknown = set(candidate) - ALLOWED_FIELDS
    if unknown:
        errors.append(f"包含不支持的字段: {', '.join(sorted(unknown))}")

    name = candidate.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name 必须是非空字符串")
    elif not _contains_name(source, name.strip()):
        errors.append("name 不在笔记标题、正文或话题中")

    raw_aliases = candidate.get("aliases", [])
    aliases = raw_aliases if isinstance(raw_aliases, list) else []
    if raw_aliases is not None and not isinstance(raw_aliases, list):
        errors.append("aliases 必须是字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in aliases):
        errors.append("aliases 必须是字符串数组")

    raw_category_ids = candidate.get("category_ids", [])
    category_ids = raw_category_ids if isinstance(raw_category_ids, list) else []
    if raw_category_ids is not None and not isinstance(raw_category_ids, list):
        errors.append("category_ids 必须是字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in category_ids):
        errors.append("category_ids 必须是字符串数组")
    if valid_categories is not None:
        invalid = {str(item) for item in category_ids} - valid_categories
        if invalid:
            errors.append(f"分类不存在或已停用: {', '.join(sorted(invalid))}")

    relation_type = candidate.get("relation_type", "mentioned")
    if relation_type not in RELATION_TYPES:
        errors.append(f"relation_type 必须是: {', '.join(sorted(RELATION_TYPES))}")

    raw_evidence = candidate.get("evidence", [])
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    if raw_evidence is not None and not isinstance(raw_evidence, list):
        errors.append("evidence 必须是非空字符串数组")
    if not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
        errors.append("evidence 必须是非空字符串数组")
    else:
        for item in evidence:
            if not _contains(source, item.strip()):
                errors.append(f"证据不在原文中: {item[:40]}")

    confidence = candidate.get("confidence")
    if confidence is not None:
        try:
            if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
                errors.append("confidence 必须在 0 到 1 之间")
        except (TypeError, ValueError):
            errors.append("confidence 必须是数字")

    price = candidate.get("price")
    if price is not None:
        if not isinstance(price, dict):
            errors.append("price 必须是对象")
        else:
            basis = price.get("basis", "unknown")
            if basis not in PRICE_BASES:
                errors.append(f"price.basis 必须是: {', '.join(sorted(PRICE_BASES))}")
            for field in ("min", "max"):
                if price.get(field) is not None and not isinstance(price[field], (int, float)):
                    errors.append(f"price.{field} 必须是数字")
                elif price.get(field) is not None and not _contains(source, str(price[field])):
                    errors.append(f"price.{field} 没有在原文中找到对应证据")
            if price.get("min") is not None and price.get("max") is not None:
                if float(price["min"]) > float(price["max"]):
                    errors.append("price.min 不能大于 price.max")

    for field in ("city", "district", "reservation_info"):
        if candidate.get(field) is not None and not isinstance(candidate[field], str):
            errors.append(f"{field} 必须是字符串")
        elif candidate.get(field) and not _contains(source, candidate[field].strip()):
            errors.append(f"{field} 没有在原文中找到对应证据")
    return errors


def _json_text(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"))


def candidate_id(note_id: str, content_hash: str, extractor_version: str, name: str) -> str:
    key = "\0".join((note_id, content_hash, extractor_version, name.strip().casefold()))
    return uuid.uuid5(uuid.NAMESPACE_URL, "xiaohongshu-merchant:" + key).hex


def save_candidate(
    conn: sqlite3.Connection,
    note: sqlite3.Row | dict[str, Any],
    candidate: dict[str, Any],
    config: MerchantAgentConfig,
    errors: Iterable[str] = (),
    raw_response: str = "",
) -> str:
    """幂等保存候选；校验错误进入 needs_review 并保留错误原因。"""
    note_id = str(note["note_id"] if isinstance(note, sqlite3.Row) else note["note_id"])
    content_hash = source_hash(note)
    name = str(candidate.get("name", "")).strip()
    cid = candidate_id(note_id, content_hash, config.extractor_version, name)
    validation_errors = list(dict.fromkeys([
        *validate_candidate(candidate, note_text(note), _active_categories(conn) or None),
        *errors,
    ]))
    evidence = _as_list(candidate.get("evidence"))
    price = candidate.get("price") or {}
    status = "needs_review" if validation_errors else "accepted"
    conn.execute(
        """INSERT INTO merchant_extraction_candidates (
            candidate_id, note_id, source_content_hash, candidate_name,
            aliases_json, category_candidates_json, city, district, price_json,
            reservation_info, relation_type, evidence_text, confidence,
            model_name, prompt_version, extractor_version, status, error_message,
            raw_response, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(candidate_id) DO UPDATE SET
            aliases_json=excluded.aliases_json,
            category_candidates_json=excluded.category_candidates_json,
            city=excluded.city,
            district=excluded.district,
            price_json=excluded.price_json,
            reservation_info=excluded.reservation_info,
            relation_type=excluded.relation_type,
            evidence_text=excluded.evidence_text,
            confidence=excluded.confidence,
            model_name=excluded.model_name,
            prompt_version=excluded.prompt_version,
            status=excluded.status,
            error_message=excluded.error_message,
            raw_response=excluded.raw_response,
            updated_at=CURRENT_TIMESTAMP""",
        (
            cid,
            note_id,
            content_hash,
            name,
            _json_text(candidate.get("aliases"), []),
            _json_text(candidate.get("category_ids"), []),
            candidate.get("city", "") or "",
            candidate.get("district", "") or "",
            _json_text(price, {}),
            candidate.get("reservation_info", "") or "",
            candidate.get("relation_type", "mentioned"),
            "\n".join(str(item) for item in evidence),
            float(candidate["confidence"]) if candidate.get("confidence") is not None else None,
            config.model,
            config.prompt_version,
            config.extractor_version,
            status,
            "; ".join(validation_errors),
            raw_response,
        ),
    )
    conn.commit()
    return cid


def _active_categories(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT category_id FROM merchant_categories WHERE status = 'active'"
        )
    }


def _merchant_id(name: str) -> str:
    """由应用逻辑生成稳定 ID；模型不能自行生成正式 merchant_id。"""
    normalized = re.sub(r"\s+", " ", name).strip().casefold()
    return uuid.uuid5(uuid.NAMESPACE_URL, "xiaohongshu-merchant:" + normalized).hex


def _json_load(value: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, json.JSONDecodeError):
        return default


def promote_candidates(conn: sqlite3.Connection) -> dict[str, int]:
    """将通过确定性校验的候选幂等写入正式商户索引。"""
    rows = conn.execute(
        """
        SELECT c.*, n.published_at
        FROM merchant_extraction_candidates c
        JOIN notes n ON n.note_id = c.note_id
        WHERE c.status = 'accepted' AND TRIM(c.candidate_name) <> ''
        ORDER BY c.updated_at, c.candidate_id
        """
    ).fetchall()
    promoted = 0
    links = 0
    for row in rows:
        merchant_id = _merchant_id(row["candidate_name"])
        aliases = _json_load(row["aliases_json"], [])
        category_ids = _json_load(row["category_candidates_json"], [])
        price = _json_load(row["price_json"], {})
        if not isinstance(aliases, list):
            aliases = []
        if not isinstance(category_ids, list):
            category_ids = []
        if not isinstance(price, dict):
            price = {}
        price_min = price.get("min")
        price_max = price.get("max")
        cuisine = [str(item) for item in category_ids if isinstance(item, str) and item.strip()]
        conn.execute(
            """INSERT INTO merchants (
                merchant_id, canonical_name, aliases, city, district, cuisine,
                price_min, price_max, currency, price_basis, reservation_info,
                status, confidence, first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'uncertain', ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(merchant_id) DO UPDATE SET
                canonical_name=excluded.canonical_name,
                aliases=CASE WHEN excluded.aliases <> '[]' THEN excluded.aliases ELSE merchants.aliases END,
                city=CASE WHEN excluded.city <> '' THEN excluded.city ELSE merchants.city END,
                district=CASE WHEN excluded.district <> '' THEN excluded.district ELSE merchants.district END,
                cuisine=CASE WHEN excluded.cuisine <> '[]' THEN excluded.cuisine ELSE merchants.cuisine END,
                price_min=COALESCE(excluded.price_min, merchants.price_min),
                price_max=COALESCE(excluded.price_max, merchants.price_max),
                currency=CASE WHEN excluded.currency <> '' THEN excluded.currency ELSE merchants.currency END,
                price_basis=CASE WHEN excluded.price_basis <> 'unknown' THEN excluded.price_basis ELSE merchants.price_basis END,
                reservation_info=CASE WHEN excluded.reservation_info <> '' THEN excluded.reservation_info ELSE merchants.reservation_info END,
                confidence=CASE WHEN excluded.confidence IS NOT NULL AND (merchants.confidence IS NULL OR excluded.confidence > merchants.confidence) THEN excluded.confidence ELSE merchants.confidence END,
                first_seen_at=CASE WHEN merchants.first_seen_at IS NULL OR merchants.first_seen_at = '' THEN excluded.first_seen_at ELSE MIN(merchants.first_seen_at, excluded.first_seen_at) END,
                last_seen_at=CASE WHEN excluded.last_seen_at IS NULL OR excluded.last_seen_at = '' THEN merchants.last_seen_at ELSE MAX(COALESCE(merchants.last_seen_at, ''), excluded.last_seen_at) END,
                updated_at=CURRENT_TIMESTAMP""",
            (
                merchant_id,
                row["candidate_name"],
                _json_text(aliases, []),
                row["city"] or "",
                row["district"] or "",
                _json_text(cuisine, []),
                price_min if isinstance(price_min, (int, float)) and not isinstance(price_min, bool) else None,
                price_max if isinstance(price_max, (int, float)) and not isinstance(price_max, bool) else None,
                str(price.get("currency", "") or ""),
                str(price.get("basis", "unknown") or "unknown"),
                row["reservation_info"] or "",
                float(row["confidence"]) if row["confidence"] is not None else None,
                row["published_at"] or "",
                row["published_at"] or "",
            ),
        )
        promoted += 1

        conn.execute(
            """INSERT INTO merchant_notes (
                merchant_id, note_id, relation_type, evidence_text, confidence
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(merchant_id, note_id, relation_type) DO UPDATE SET
                evidence_text=excluded.evidence_text,
                confidence=excluded.confidence""",
            (
                merchant_id,
                row["note_id"],
                row["relation_type"] or "mentioned",
                row["evidence_text"] or "",
                row["confidence"],
            ),
        )

        for category_id in category_ids:
            if not isinstance(category_id, str) or not category_id.strip():
                continue
            category_exists = conn.execute(
                "SELECT 1 FROM merchant_categories WHERE category_id = ? AND status = 'active'",
                (category_id,),
            ).fetchone()
            if not category_exists:
                continue
            conn.execute(
                """INSERT INTO merchant_category_links (
                    merchant_id, category_id, source_note_id, evidence_text,
                    confidence, is_primary, taxonomy_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'v1')
                ON CONFLICT(merchant_id, category_id, source_note_id) DO UPDATE SET
                    evidence_text=excluded.evidence_text,
                    confidence=excluded.confidence,
                    is_primary=excluded.is_primary""",
                (
                    merchant_id,
                    category_id,
                    row["note_id"],
                    row["evidence_text"] or "",
                    row["confidence"],
                    1 if row["relation_type"] == "primary_review" else 0,
                ),
            )
            links += 1
    conn.commit()
    return {"promoted": promoted, "category_links": links}


def _load_notes(conn: sqlite3.Connection, note_id: str | None, limit: int) -> list[sqlite3.Row]:
    if note_id:
        rows = conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notes WHERE COALESCE(content, description, title, '') <> '' "
            "ORDER BY crawled_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return list(rows)


def _prompt(note: sqlite3.Row) -> str:
    return json.dumps(
        {
            "note_id": note["note_id"],
            "title": note["title"] or "",
            "text": note_text(note),
        },
        ensure_ascii=False,
    )


SYSTEM_PROMPT = """你是商户信息抽取器。只输出 JSON，不要 Markdown 或解释。
格式为 {\"merchants\":[{\"name\":string,\"aliases\":[],\"category_ids\":[],
\"city\":string|null,\"district\":string|null,\"price\":{\"min\":number|null,
\"max\":number|null,\"currency\":string,\"basis\":\"per_person\"|\"set\"|\"unknown\"},
\"reservation_info\":string|null,\"relation_type\":\"primary_review\"|\"mentioned\"|
\"listicle\"|\"comparison\",\"evidence\":[],\"confidence\":number}]}。
商户名称和所有 evidence 必须逐字来自输入文本。没有证据的字段使用 null 或空数组。
不要输出 address 字段，也不要推断地址、营业状态或实时信息。"""


def _decode_agent_content(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("Agent 响应缺少 choices")
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    content = str(content).strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Agent 顶层结果必须是对象")
    return result


def call_agent(note: sqlite3.Row, config: MerchantAgentConfig) -> tuple[dict[str, Any], str]:
    if not config.api_key:
        raise RuntimeError("未配置 MERCHANT_AGENT_API_KEY（或 OPENAI_API_KEY）")
    if config.provider.lower() not in {"openai", "openai-compatible", "api"}:
        raise ValueError(f"暂不支持的 Agent provider: {config.provider}")
    request = Request(
        config.base_url + "/chat/completions",
        data=json.dumps(
            {
                "model": config.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _prompt(note)},
                ],
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(config.retries + 1):
        try:
            with urlopen(request, timeout=config.timeout_s) as response:
                raw = response.read().decode("utf-8")
            return _decode_agent_content(json.loads(raw)), raw
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            detail = detail[:500]
            last_error = RuntimeError(
                f"Agent HTTP {exc.code} {exc.reason}"
                + (f": {detail}" if detail else "")
            )
            if attempt < config.retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            break
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < config.retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Agent 调用失败: {last_error}") from last_error


def extract_notes(conn: sqlite3.Connection, notes: list[sqlite3.Row], config: MerchantAgentConfig) -> dict[str, int]:
    valid_categories = _active_categories(conn)
    stats = {"notes": 0, "accepted": 0, "needs_review": 0, "failed": 0}
    selected = notes[: config.max_notes]
    total = len(selected)
    for index, note in enumerate(selected, start=1):
        stats["notes"] += 1
        print(
            f"[MERCHANT] [{index}/{total}] 抽取笔记 {note['note_id']}",
            file=sys.stderr,
            flush=True,
        )
        try:
            result, raw = call_agent(note, config)
            merchants = result.get("merchants", [])
            if not isinstance(merchants, list):
                raise ValueError("merchants 必须是数组")
            for item in merchants:
                if not isinstance(item, dict):
                    item = {"name": "", "evidence": []}
                errors = validate_candidate(item, note_text(note), valid_categories or None)
                save_candidate(conn, note, item, config, errors, raw)
                stats["needs_review" if errors else "accepted"] += 1
            print(
                f"[MERCHANT] [{index}/{total}] 返回 {len(merchants)} 个候选",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            stats["failed"] += 1
            print(
                f"[MERCHANT] [{index}/{total}] 失败: {exc}",
                file=sys.stderr,
                flush=True,
            )
            save_candidate(
                conn,
                note,
                {"name": "", "evidence": []},
                config,
                [str(exc)],
            )
    stats.update(promote_candidates(conn))
    return stats


def _read_input(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return {"merchants": value}
    if not isinstance(value, dict):
        raise ValueError("候选文件必须是对象或数组")
    return value


def import_candidates(conn: sqlite3.Connection, path: Path, config: MerchantAgentConfig) -> tuple[int, int, int]:
    payload = _read_input(path)
    note_id = payload.get("note_id")
    if not note_id:
        raise ValueError("候选文件顶层必须有 note_id")
    note = conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
    if note is None:
        raise ValueError(f"找不到笔记: {note_id}")
    merchants = payload.get("merchants", [])
    if not isinstance(merchants, list):
        raise ValueError("merchants 必须是数组")
    valid_categories = _active_categories(conn)
    valid = invalid = 0
    for candidate in merchants:
        if not isinstance(candidate, dict):
            candidate = {"name": "", "evidence": []}
        errors = validate_candidate(candidate, note_text(note), valid_categories or None)
        save_candidate(conn, note, candidate, config, errors, json.dumps(payload, ensure_ascii=False))
        if errors:
            invalid += 1
        else:
            valid += 1
    promoted = promote_candidates(conn)
    return valid, invalid, promoted["promoted"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="商户抽取候选数据工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化 SQLite 商户数据表")
    extract = sub.add_parser("extract", help="调用 .env 中配置的 Agent 抽取笔记")
    extract.add_argument("--note-id")
    extract.add_argument("--limit", type=int, default=None)
    import_cmd = sub.add_parser("import", help="校验并导入 Agent 结果 JSON")
    import_cmd.add_argument("--input", required=True, type=Path)
    sub.add_parser("promote", help="将 accepted 候选幂等写入正式商户索引")
    validate = sub.add_parser("validate", help="校验 Agent 结果 JSON，不写库")
    validate.add_argument("--input", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = MerchantAgentConfig.from_env()
    conn = xhs_storage.connect()
    try:
        if args.command == "init":
            print("SQLite 商户数据表已初始化")
            return 0
        if args.command in {"import", "validate"}:
            payload = _read_input(args.input)
            note = conn.execute("SELECT * FROM notes WHERE note_id = ?", (payload.get("note_id", ""),)).fetchone()
            if note is None:
                raise ValueError(f"找不到笔记: {payload.get('note_id', '')}")
            categories = _active_categories(conn) or None
            errors = []
            for index, item in enumerate(payload.get("merchants", [])):
                if not isinstance(item, dict):
                    item = {"name": "", "evidence": []}
                item_errors = validate_candidate(item, note_text(note), categories)
                errors.extend(f"#{index + 1}: {message}" for message in item_errors)
            if args.command == "validate":
                print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
                return 0 if not errors else 2
            valid, invalid, promoted = import_candidates(conn, args.input, config)
            print(f"导入完成：{valid} 个通过，{invalid} 个进入 needs_review，{promoted} 个候选已写入商户索引")
            return 0
        if args.command == "promote":
            result = promote_candidates(conn)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        limit = args.limit or config.max_notes
        stats = extract_notes(conn, _load_notes(conn, args.note_id, limit), config)
        print(json.dumps(stats, ensure_ascii=False))
        return 0 if stats["failed"] == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
