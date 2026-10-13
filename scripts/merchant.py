"""商户抽取与候选入库入口。

模型结果先保存到 ``merchant_extraction_candidates``，通过校验后由本脚本幂等
晋级到 ``merchants`` 和来源关系表。所有数据直接写入 PostgreSQL，表结构由根目录的 ``schema.sql`` 维护。所有 Agent 配置从项目根目录
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
from functools import lru_cache
import hashlib
import json
import math
import os
import re
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
PROMPT_PATH = Path(__file__).with_name("merchant_prompt.md")
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
    "tag_ids",
    "city",
    "district",
    "price",
    "reservation_info",
    "relation_type",
    "evidence",
    "confidence",
}

# 业务分类是稳定的受控词表；标签表达场景/环境/消费等特征。
DEFAULT_CATEGORIES = (
    ("dining", "", "餐饮", "餐厅、咖啡、甜品和酒吧等餐饮商户", 10),
    ("dining.restaurant", "dining", "餐厅", "正餐及特色餐饮", 11),
    ("dining.cafe", "dining", "咖啡店", "咖啡、茶饮和轻食", 12),
    ("dining.dessert", "dining", "甜品店", "甜品、烘焙和冰淇淋", 13),
    ("dining.bar", "dining", "酒吧", "酒吧和夜间餐饮", 14),
    ("entertainment", "", "文娱演出", "演唱会、演出和其他文娱活动", 20),
    ("entertainment.concert", "entertainment", "演唱会", "演唱会及音乐现场", 21),
    ("entertainment.event", "entertainment", "演出活动", "展览、演出和主题活动", 22),
    ("transport", "", "交通出行", "机票、用车和租车服务", 30),
    ("transport.flight", "transport", "机票", "航空机票及相关服务", 31),
    ("transport.car", "transport", "用车", "接送机、包车和网约车服务", 32),
    ("transport.rental", "transport", "租车", "汽车租赁服务", 33),
    ("healthcare", "", "医疗健康", "诊所、体检和健康管理", 40),
    ("healthcare.clinic", "healthcare", "诊所", "专科及综合诊所", 41),
    ("healthcare.checkup", "healthcare", "体检", "体检和筛查服务", 42),
    ("healthcare.anti_aging", "healthcare", "抗衰", "抗衰老和健康管理服务", 43),
    ("wellness", "", "美容休闲", "SPA、按摩和美容护理", 50),
    ("wellness.spa", "wellness", "SPA", "SPA及水疗服务", 51),
    ("wellness.massage", "wellness", "按摩", "按摩及身体护理", 52),
    ("wellness.beauty", "wellness", "美容护理", "美容及皮肤护理", 53),
    ("visa", "", "签证服务", "签证、移民和出入境服务", 60),
    ("visa.agency", "visa", "签证代办", "签证申请及代办服务", 61),
)

DEFAULT_TAGS = (
    ("date_friendly", "场景", "适合约会"), ("group_dining", "场景", "适合聚餐"),
    ("celebration", "场景", "适合庆祝"), ("business", "场景", "适合商务"),
    ("solo_friendly", "场景", "适合独处"), ("family_friendly", "场景", "适合家庭"),
    ("photo_friendly", "场景", "适合拍照"), ("group_friendly", "场景", "适合多人"),
    ("sea_view", "环境", "海景餐厅"), ("city_night_view", "环境", "城市夜景"),
    ("quiet", "环境", "氛围安静"), ("lively", "环境", "氛围热闹"),
    ("terrace", "环境", "露台座位"), ("outdoor_seating", "环境", "户外座位"),
    ("window_seat", "环境", "窗边座位"), ("private_room", "环境", "私密包间"),
    ("fine_decor", "环境", "装修精致"), ("value", "消费", "高性价比"),
    ("budget", "消费", "平价餐饮"), ("premium", "消费", "高端餐饮"),
    ("under_100", "消费", "人均百元内"), ("under_300", "消费", "人均三百内"),
    ("under_500", "消费", "人均五百内"), ("pricey", "消费", "价格偏高"),
    ("large_portions", "消费", "分量充足"), ("reservable", "预约", "可预约"),
    ("advance_booking", "预约", "需提前预约"), ("walk_in", "预约", "免预约"),
    ("long_queue", "预约", "排队较久"), ("limited_seats", "预约", "位置紧张"),
    ("michelin", "特色", "米其林"), ("bib_gourmand", "特色", "必比登"),
    ("award_winning", "特色", "获奖餐厅"), ("signature_dish", "特色", "招牌菜"),
    ("seasonal", "特色", "季节限定"), ("local_specialty", "特色", "本地特色"),
    ("creative_cuisine", "特色", "创意菜式"), ("heritage_store", "特色", "传统老店"),
    ("breakfast", "便利", "早餐供应"), ("lunch", "便利", "午餐供应"),
    ("afternoon_tea", "便利", "下午茶"), ("late_night", "便利", "深夜营业"),
    ("child_friendly", "便利", "儿童友好"), ("pet_friendly", "便利", "宠物友好"),
    ("accessible", "便利", "无障碍"),
)


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


def note_text(note: DictRow | dict[str, Any]) -> str:
    """返回发送给 Agent 的公开文本，不包含原始数据、Cookie 或 token。"""
    note = dict(note)
    topics = note.get("topics") or []
    topic_text = " ".join(str(item) for item in topics if str(item).strip())
    return "\n".join(part for part in (
        str(note.get("title") or ""),
        str(note.get("content") or note.get("description") or ""),
        topic_text,
    ) if part.strip())


def source_hash(note: DictRow | dict[str, Any]) -> str:
    return str(note.get("content_hash") or hashlib.sha256(note_text(note).encode("utf-8")).hexdigest())


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
    valid_tags: set[str] | None = None,
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

    raw_tag_ids = candidate.get("tag_ids", [])
    tag_ids = raw_tag_ids if isinstance(raw_tag_ids, list) else []
    if raw_tag_ids is not None and not isinstance(raw_tag_ids, list):
        errors.append("tag_ids 必须是字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in tag_ids):
        errors.append("tag_ids 必须是字符串数组")
    if valid_tags is not None:
        invalid = {str(item) for item in tag_ids} - valid_tags
        if invalid:
            errors.append(f"标签不存在或已停用: {', '.join(sorted(invalid))}")

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
    conn: Connection,
    note: DictRow | dict[str, Any],
    candidate: dict[str, Any],
    config: MerchantAgentConfig,
    errors: Iterable[str] = (),
    raw_response: str = "",
) -> str:
    """幂等保存候选；校验错误进入 needs_review 并保留错误原因。"""
    note_id = str(note["note_id"])
    content_hash = source_hash(note)
    name = str(candidate.get("name", "")).strip()
    cid = candidate_id(note_id, content_hash, config.extractor_version, name)
    validation_errors = list(dict.fromkeys([
        *validate_candidate(candidate, note_text(note), _active_categories(conn) or None, _active_tags(conn) or None),
        *errors,
    ]))
    evidence = _as_list(candidate.get("evidence"))
    price = candidate.get("price") or {}
    status = "needs_review" if validation_errors else "accepted"
    conn.execute(
        """INSERT INTO merchant_extraction_candidates (
            candidate_id, note_id, source_content_hash, candidate_name,
            aliases, category_candidates, tag_candidates, city, district, price,
            reservation_info, relation_type, evidence_text, confidence,
            model_name, prompt_version, extractor_version, status, error_message,
            raw_response, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(candidate_id) DO UPDATE SET
            aliases=excluded.aliases,
            category_candidates=excluded.category_candidates,
            tag_candidates=excluded.tag_candidates,
            city=excluded.city,
            district=excluded.district,
            price=excluded.price,
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
            _json_text(candidate.get("tag_ids"), []),
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


def _active_categories(conn: Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT category_id FROM merchant_categories WHERE status = 'active'"
        )
    }


def _active_tags(conn: Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT tag_id FROM merchant_tags WHERE status = 'active'")
    }


def _active_taxonomy_rows(conn: Connection) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    categories = [
        {"id": row[0], "name": row[1], "parent_id": row[2] or ""}
        for row in conn.execute(
            """SELECT category_id, name, parent_id
               FROM merchant_categories WHERE status = 'active'
               ORDER BY sort_order, category_id"""
        )
    ]
    tags = [
        {"id": row[0], "name": row[1], "group": row[2] or ""}
        for row in conn.execute(
            """SELECT tag_id, name, group_name
               FROM merchant_tags WHERE status = 'active'
               ORDER BY group_name, name, tag_id"""
        )
    ]
    return categories, tags


def seed_taxonomy(conn: Connection) -> dict[str, int]:
    """写入内置分类和标签字典；已有自定义记录不会被覆盖。"""
    for category_id, parent_id, name, description, sort_order in DEFAULT_CATEGORIES:
        conn.execute(
            """INSERT INTO merchant_categories
                (category_id, parent_id, name, description, sort_order, status, taxonomy_version)
                VALUES (%s, %s, %s, %s, %s, 'active', 'v1')
                ON CONFLICT(category_id) DO NOTHING""",
            (category_id, parent_id, name, description, sort_order),
        )
    for tag_id, group_name, name in DEFAULT_TAGS:
        conn.execute(
            """INSERT INTO merchant_tags
                (tag_id, group_name, name, status, taxonomy_version)
                VALUES (%s, %s, %s, 'active', 'v1')
                ON CONFLICT(tag_id) DO NOTHING""",
            (tag_id, group_name, name),
        )
    conn.commit()
    return {"categories": len(DEFAULT_CATEGORIES), "tags": len(DEFAULT_TAGS)}


def _merchant_id(name: str) -> str:
    """由应用逻辑生成稳定 ID；模型不能自行生成正式 merchant_id。"""
    normalized = re.sub(r"\s+", " ", name).strip().casefold()
    return uuid.uuid5(uuid.NAMESPACE_URL, "xiaohongshu-merchant:" + normalized).hex


def promote_candidates(conn: Connection) -> dict[str, int]:
    """将通过确定性校验的候选幂等写入正式商户索引。"""
    seed_taxonomy(conn)
    rows = conn.execute(
        """
        SELECT c.*, n.published_at
        FROM merchant_extraction_candidates c
        JOIN xhs_notes n ON n.note_id = c.note_id
        WHERE c.status = 'accepted' AND TRIM(c.candidate_name) <> ''
        ORDER BY c.updated_at, c.candidate_id
        """
    ).fetchall()
    promoted = 0
    links = 0
    tag_links = 0
    for row in rows:
        merchant_id = _merchant_id(row["candidate_name"])
        aliases = (row["aliases"] or [])
        category_ids = (row["category_candidates"] or [])
        tag_ids = (row["tag_candidates"] or [])
        price = (row["price"] or {})
        if not isinstance(aliases, list):
            aliases = []
        if not isinstance(category_ids, list):
            category_ids = []
        if not isinstance(tag_ids, list):
            tag_ids = []
        if not isinstance(price, dict):
            price = {}
        price_min = price.get("min")
        price_max = price.get("max")
        cuisine = [str(item) for item in category_ids if isinstance(item, str) and item.strip()]
        conn.execute(
            """INSERT INTO merchants (
                merchant_id, canonical_name, aliases, city, district, cuisine,
                price_min, price_max, currency, price_basis, reservation_info,
                confidence, first_seen_at, last_seen_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
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
                first_seen_at=CASE WHEN merchants.first_seen_at IS NULL OR merchants.first_seen_at = '' THEN excluded.first_seen_at ELSE LEAST(merchants.first_seen_at, excluded.first_seen_at) END,
                last_seen_at=CASE WHEN excluded.last_seen_at IS NULL OR excluded.last_seen_at = '' THEN merchants.last_seen_at ELSE GREATEST(COALESCE(merchants.last_seen_at, ''), excluded.last_seen_at) END,
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
            ) VALUES (%s, %s, %s, %s, %s)
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
                "SELECT 1 FROM merchant_categories WHERE category_id = %s AND status = 'active'",
                (category_id,),
            ).fetchone()
            if not category_exists:
                continue
            conn.execute(
                """INSERT INTO merchant_category_links (
                    merchant_id, category_id, source_note_id, evidence_text,
                    confidence, is_primary, taxonomy_version
                ) VALUES (%s, %s, %s, %s, %s, %s, 'v1')
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
                    row["relation_type"] == "primary_review",
                ),
            )
            links += 1
        for tag_id in tag_ids:
            if not isinstance(tag_id, str) or not tag_id.strip():
                continue
            tag_exists = conn.execute(
                "SELECT 1 FROM merchant_tags WHERE tag_id = %s AND status = 'active'",
                (tag_id,),
            ).fetchone()
            if not tag_exists:
                continue
            conn.execute(
                """INSERT INTO merchant_tag_links (
                    merchant_id, tag_id, source_note_id, evidence_text, confidence
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(merchant_id, tag_id, source_note_id) DO UPDATE SET
                    evidence_text=excluded.evidence_text,
                    confidence=excluded.confidence""",
                (
                    merchant_id,
                    tag_id,
                    row["note_id"],
                    row["evidence_text"] or "",
                    row["confidence"],
                ),
            )
            tag_links += 1
    conn.commit()
    return {"promoted": promoted, "category_links": links, "tag_links": tag_links}


def _load_notes(conn: Connection, note_id: str | None, limit: int) -> list[DictRow]:
    if note_id:
        rows = conn.execute("SELECT * FROM xhs_notes WHERE note_id = %s", (note_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM xhs_notes WHERE COALESCE(content, description, title, '') <> '' "
            "ORDER BY crawled_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return list(rows)


def _prompt(note: DictRow, categories: list[dict[str, str]], tags: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "note_id": note["note_id"],
            "title": note["title"] or "",
            "text": note_text(note),
            "available_categories": categories,
            "available_tags": tags,
        },
        ensure_ascii=False,
    )


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """从独立 Markdown 文件读取提示词，避免修改脚本时误改抽取协议。"""
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"读取商户提示词失败: {PROMPT_PATH}: {exc}") from exc
    if not prompt:
        raise RuntimeError(f"商户提示词为空: {PROMPT_PATH}")
    return prompt


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


def call_agent(
    note: DictRow,
    config: MerchantAgentConfig,
    categories: list[dict[str, str]],
    tags: list[dict[str, str]],
) -> tuple[dict[str, Any], str]:
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
                    {"role": "system", "content": load_system_prompt()},
                    {"role": "user", "content": _prompt(note, categories, tags)},
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


def extract_notes(conn: Connection, notes: list[DictRow], config: MerchantAgentConfig) -> dict[str, int]:
    seed_taxonomy(conn)
    valid_categories = _active_categories(conn)
    valid_tags = _active_tags(conn)
    categories, tags = _active_taxonomy_rows(conn)
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
            result, raw = call_agent(note, config, categories, tags)
            merchants = result.get("merchants", [])
            if not isinstance(merchants, list):
                raise ValueError("merchants 必须是数组")
            for item in merchants:
                if not isinstance(item, dict):
                    item = {"name": "", "evidence": []}
                errors = validate_candidate(item, note_text(note), valid_categories or None, valid_tags or None)
                save_candidate(conn, note, item, config, errors, raw)
                stats["needs_review" if errors else "accepted"] += 1
            print(
                f"[MERCHANT] [{index}/{total}] 返回 {len(merchants)} 个候选",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            conn.rollback()
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


def import_candidates(conn: Connection, path: Path, config: MerchantAgentConfig) -> tuple[int, int, int]:
    seed_taxonomy(conn)
    payload = _read_input(path)
    note_id = payload.get("note_id")
    if not note_id:
        raise ValueError("候选文件顶层必须有 note_id")
    note = conn.execute("SELECT * FROM xhs_notes WHERE note_id = %s", (note_id,)).fetchone()
    if note is None:
        raise ValueError(f"找不到笔记: {note_id}")
    merchants = payload.get("merchants", [])
    if not isinstance(merchants, list):
        raise ValueError("merchants 必须是数组")
    valid_categories = _active_categories(conn)
    valid_tags = _active_tags(conn)
    valid = invalid = 0
    for candidate in merchants:
        if not isinstance(candidate, dict):
            candidate = {"name": "", "evidence": []}
        errors = validate_candidate(candidate, note_text(note), valid_categories or None, valid_tags or None)
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
    sub.add_parser("init", help="初始化 PostgreSQL 商户数据表")
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
            taxonomy = seed_taxonomy(conn)
            print(f"PostgreSQL 商户数据表已初始化，已载入 {taxonomy['categories']} 个分类和 {taxonomy['tags']} 个标签")
            return 0
        if args.command in {"import", "validate"}:
            payload = _read_input(args.input)
            note = conn.execute("SELECT * FROM xhs_notes WHERE note_id = %s", (payload.get("note_id", ""),)).fetchone()
            if note is None:
                raise ValueError(f"找不到笔记: {payload.get('note_id', '')}")
            seed_taxonomy(conn)
            categories = _active_categories(conn) or None
            tags = _active_tags(conn) or None
            errors = []
            for index, item in enumerate(payload.get("merchants", [])):
                if not isinstance(item, dict):
                    item = {"name": "", "evidence": []}
                item_errors = validate_candidate(item, note_text(note), categories, tags)
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
