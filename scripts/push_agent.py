"""数据推送 Agent (Push Agent)。

职责：
1. 扫描数据库中尚未推送的商户、演唱会等增量数据。
2. 调用大模型 Agent，用亲切、生动、“说人话”的语气汇总成一份精炼的情报简报。
3. 通过飞书 / Lark 自定义机器人 Webhook 推送富文本卡片。
4. 推送成功后自动在 push_records 中标记推送状态与快照。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", encoding="utf-8")

from database import Connection, connect
import push_records


@dataclass(frozen=True)
class PushAgentConfig:
    """推送 Agent 配置；优先读取通用 AGENT_* 配置，并补充推送相关参数。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_s: int
    retries: int
    lark_webhook_url: str
    lark_webhook_secret: str
    channel: str
    max_items: int

    @classmethod
    def from_env(cls) -> "PushAgentConfig":
        def value(name: str, fallback: str = "") -> str:
            return os.getenv(name, fallback).strip()

        def integer(name: str, fallback: int, minimum: int = 1) -> int:
            raw = value(name, str(fallback))
            try:
                return max(minimum, int(raw))
            except ValueError as exc:
                raise ValueError(f"{name} 必须是整数，当前值为 {raw!r}") from exc

        provider = value("PUSH_AGENT_PROVIDER", value("AGENT_PROVIDER", "openai"))
        api_key = value(
            "PUSH_AGENT_API_KEY",
            value("AGENT_API_KEY", value("MERCHANT_AGENT_API_KEY", value("OPENAI_API_KEY"))),
        )
        base_url = value(
            "PUSH_AGENT_BASE_URL",
            value("AGENT_BASE_URL", value("MERCHANT_AGENT_BASE_URL", value("OPENAI_BASE_URL", "https://api.openai.com/v1"))),
        )
        model = value(
            "PUSH_AGENT_MODEL",
            value("AGENT_MODEL", value("MERCHANT_AGENT_MODEL", value("OPENAI_MODEL", "gpt-4o-mini"))),
        )
        timeout_s = integer("PUSH_AGENT_TIMEOUT_S", integer("AGENT_TIMEOUT_S", 60))
        retries = integer("PUSH_AGENT_RETRIES", integer("AGENT_RETRIES", 2, minimum=0), minimum=0)
        lark_webhook_url = value("LARK_WEBHOOK_URL", value("AGENT_LARK_WEBHOOK_URL"))
        lark_webhook_secret = value("LARK_WEBHOOK_SECRET", value("AGENT_LARK_WEBHOOK_SECRET"))
        channel = value("PUSH_AGENT_CHANNEL", "feishu")
        max_items = integer("PUSH_AGENT_MAX_ITEMS", 30)

        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            timeout_s=timeout_s,
            retries=retries,
            lark_webhook_url=lark_webhook_url,
            lark_webhook_secret=lark_webhook_secret,
            channel=channel,
            max_items=max_items,
        )


def fetch_unpushed_data(
    conn: Connection,
    channel: str = "feishu",
    limit: int = 30,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """查询未推送的商户与演唱会，并附带关联信息（分类、标签、演出场次）。"""
    # 1. 查询未推送商户
    raw_merchants = push_records.find_unpushed_entities(
        conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        channel=channel,
        limit=limit,
    )
    enriched_merchants: list[dict[str, Any]] = []
    if raw_merchants:
        merchant_ids = [m["merchant_id"] for m in raw_merchants]
        cur = conn.execute(
            """SELECT cl.merchant_id, c.name AS category_name
               FROM merchant_category_links cl
               JOIN merchant_categories c ON c.category_id = cl.category_id
               WHERE cl.merchant_id = ANY(%s)""",
            (merchant_ids,),
        )
        categories_map: dict[str, list[str]] = {}
        for row in cur.fetchall():
            categories_map.setdefault(row["merchant_id"], []).append(row["category_name"])

        cur = conn.execute(
            """SELECT tl.merchant_id, t.name AS tag_name
               FROM merchant_tag_links tl
               JOIN merchant_tags t ON t.tag_id = tl.tag_id
               WHERE tl.merchant_id = ANY(%s)""",
            (merchant_ids,),
        )
        tags_map: dict[str, list[str]] = {}
        for row in cur.fetchall():
            tags_map.setdefault(row["merchant_id"], []).append(row["tag_name"])

        for m in raw_merchants:
            mid = m["merchant_id"]
            price_text = ""
            if m.get("price_min") and m.get("price_max"):
                price_text = f"{m.get('currency', 'HKD')} {m['price_min']} - {m['price_max']}"
            elif m.get("price_min"):
                price_text = f"约 {m.get('currency', 'HKD')} {m['price_min']}"

            enriched_merchants.append({
                "merchant_id": mid,
                "name": m.get("canonical_name") or "未命名商户",
                "city": m.get("city") or "",
                "district": m.get("district") or "",
                "cuisine": m.get("cuisine") or [],
                "price": price_text,
                "reservation_info": m.get("reservation_info") or "",
                "categories": categories_map.get(mid, []),
                "tags": tags_map.get(mid, []),
                "updated_at": m.get("updated_at"),
            })

    # 2. 查询未推送演唱会
    raw_concerts = push_records.find_unpushed_entities(
        conn,
        entity_type=push_records.ENTITY_TYPE_CONCERT,
        channel=channel,
        limit=limit,
    )
    enriched_concerts: list[dict[str, Any]] = []
    if raw_concerts:
        concert_ids = [c["concert_id"] for c in raw_concerts]
        cur = conn.execute(
            """SELECT concert_id, start_at, display_date, venue_name, ticket_url, ticket_status
               FROM concert_occurrences_unified
               WHERE concert_id = ANY(%s)
               ORDER BY start_at ASC""",
            (concert_ids,),
        )
        occurrences_map: dict[str, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            occurrences_map.setdefault(row["concert_id"], []).append({
                "display_date": row.get("display_date") or str(row.get("start_at") or ""),
                "venue_name": row.get("venue_name") or "",
                "ticket_status": row.get("ticket_status") or "",
                "ticket_url": row.get("ticket_url") or "",
            })

        for c in raw_concerts:
            cid = c["concert_id"]
            enriched_concerts.append({
                "concert_id": cid,
                "source": c.get("source") or "",
                "title": c.get("title") or "演出",
                "artist_name": c.get("artist_name") or "",
                "venue_name": c.get("venue_name") or "",
                "city": c.get("city") or "Singapore",
                "event_url": c.get("event_url") or "",
                "image_url": c.get("image_url") or "",
                "source_hash": c.get("source_hash") or "",
                "occurrences": occurrences_map.get(cid, []),
                "updated_at": c.get("updated_at"),
            })

    return enriched_merchants, enriched_concerts


def _build_agent_prompt(
    merchants: list[dict[str, Any]],
    concerts: list[dict[str, Any]],
) -> tuple[str, str]:
    """生成给 Agent 的系统提示词与输入上下文。"""
    system_prompt = (
        "你是一位品味在线、亲切风趣的生活与演出情报官（重点关注香港与新加坡两地）。\n"
        "你的职责是将数据库最新入库的商户（美食玩乐）和演唱会/演出信息，汇总成一份精炼、说人话、有温度的数据情报简报。\n\n"
        "【撰写核心准则】\n"
        "1. 说人话：像一位热爱生活的朋友在群聊里热情分享新鲜发现，语气生动、自然，拒绝干瘪、死板的机器表格罗列。\n"
        "2. 突出精炼亮点：\n"
        "   - 商户：重点说清店名、城市/商圈位置、主营特色、招牌/标签（如米其林、适合约会、海景、需提前预约等）以及价格。\n"
        "   - 演唱会：重点说清演出艺人/主题、时间场次、场馆地点，以及购票建议或抢票提示。\n"
        "3. 结构分明（使用飞书友好易读的 Markdown 语法）：\n"
        "   - 导语：1~2句话热情概括本次更新亮点；\n"
        "   - 🍽️ 新增精选商户（如有）：每家店用 2~3 行精炼推荐；\n"
        "   - 🎤 演出与演唱会动态（如有）：突出重点艺人与场次时间；\n"
        "   - 💡 情报官贴士：1~2条贴心的游玩/预约/抢票建议。\n"
        "4. 篇幅精炼：控制整体长度，切忌冗长罗嗦。如果某一类别数据为空，则不需要展示对应模块。"
    )

    context_parts: list[str] = []
    if merchants:
        context_parts.append(f"### 最新收录商户（共 {len(merchants)} 家）：")
        context_parts.append(json.dumps(merchants, ensure_ascii=False, indent=2, default=str))

    if concerts:
        context_parts.append(f"### 最新收录演唱会/演出（共 {len(concerts)} 场）：")
        context_parts.append(json.dumps(concerts, ensure_ascii=False, indent=2, default=str))

    user_prompt = (
        "以下是本次数据库新增的未推送数据，请按照要求生成一份精美、接地气的 Markdown 情报简报：\n\n"
        + "\n\n".join(context_parts)
    )
    return system_prompt, user_prompt


def call_llm(
    config: PushAgentConfig,
    messages: list[dict[str, str]],
    temperature: float = 0.7,
) -> str:
    """调用 OpenAI 兼容的 LLM 接口，带重试与超时控制。"""
    if not config.api_key:
        raise RuntimeError("未配置 AGENT_API_KEY（或 PUSH_AGENT_API_KEY / OPENAI_API_KEY）")

    url = f"{config.base_url}/chat/completions"
    data = json.dumps(
        {
            "model": config.model,
            "messages": messages,
            "temperature": temperature,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(config.retries + 1):
        try:
            with urlopen(req, timeout=config.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                payload = json.loads(raw)
                choices = payload.get("choices") or []
                if not choices:
                    raise ValueError(f"LLM 响应缺少 choices: {raw[:200]}")
                content = (choices[0].get("message") or {}).get("content", "")
                return str(content).strip()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            detail = detail[:400]
            last_error = RuntimeError(f"LLM HTTP {exc.code} {exc.reason}: {detail}")
            if attempt < config.retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            break
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < config.retries:
                time.sleep(min(2 ** attempt, 8))

    raise RuntimeError(f"推送 Agent LLM 生成失败: {last_error}") from last_error


def generate_push_report(
    config: PushAgentConfig,
    merchants: list[dict[str, Any]],
    concerts: list[dict[str, Any]],
) -> str:
    """调用 Agent 生成精简人话数据报告。"""
    system_prompt, user_prompt = _build_agent_prompt(merchants, concerts)
    return call_llm(
        config,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )


def build_lark_card(
    content_markdown: str,
    title: str = "📢 本地生活与演出情报速递",
    item_stats: str = "",
) -> dict[str, Any]:
    """构造飞书 / Lark 交互式消息卡片。"""
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": content_markdown,
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"🤖 智能生活情报 Agent | {item_stats} | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                }
            ],
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
                "template": "blue",
            },
            "elements": elements,
        },
    }


def send_to_lark(
    webhook_url: str,
    card_payload: dict[str, Any],
    secret: str = "",
) -> dict[str, Any]:
    """通过飞书自定义机器人 Webhook 发送卡片消息。"""
    if not webhook_url:
        raise ValueError("未配置飞书 Webhook 地址 (LARK_WEBHOOK_URL)")

    payload = dict(card_payload)
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    req = Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        res_data = json.loads(raw)

    # 飞书成功返回通常为 {"StatusCode": 0} 或 {"code": 0}
    code = res_data.get("code", res_data.get("StatusCode"))
    if code != 0 and code is not None:
        raise RuntimeError(f"飞书 Webhook 返回错误: {raw}")

    return res_data


def run_push_pipeline(
    conn: Connection,
    config: PushAgentConfig,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """执行推送全流程：查询待推送 -> Agent 总结 -> Lark 投递 -> 记录状态。"""
    actual_limit = limit or config.max_items
    merchants, concerts = fetch_unpushed_data(conn, channel=config.channel, limit=actual_limit)

    total_items = len(merchants) + len(concerts)
    if total_items == 0:
        print("[PUSH] 暂无未推送的新数据。")
        return {"status": "empty", "merchants": 0, "concerts": 0}

    print(f"[PUSH] 发现待推送数据: 商户 {len(merchants)} 家, 演唱会 {len(concerts)} 场。正在调用 Agent 总结...")
    report_markdown = generate_push_report(config, merchants, concerts)

    item_stats = f"本次上新: 商户 {len(merchants)} 家, 演唱会 {len(concerts)} 场"
    card = build_lark_card(report_markdown, item_stats=item_stats)

    if dry_run:
        print("\n================ [DRY RUN 预览] ================")
        print(report_markdown)
        print("================================================\n")
        print("[PUSH] dry-run 模式已开启，跳过真实飞书发送与数据库记录写入。")
        return {
            "status": "dry_run",
            "merchants": len(merchants),
            "concerts": len(concerts),
            "report": report_markdown,
        }

    # 真实推送模式
    if not config.lark_webhook_url:
        raise RuntimeError("未在 .env 中配置 LARK_WEBHOOK_URL，无法发送推送。")

    print(f"[PUSH] 正在向飞书 Webhook 投递卡片...")
    try:
        send_to_lark(config.lark_webhook_url, card, secret=config.lark_webhook_secret)
        print("[PUSH] 飞书卡片投递成功！正在更新推送状态...")

        # 标记商户推送成功
        for m in merchants:
            push_records.mark_push_success(
                conn,
                entity_type=push_records.ENTITY_TYPE_MERCHANT,
                entity_id=m["merchant_id"],
                channel=config.channel,
                entity_updated_at=m.get("updated_at"),
                payload_snapshot={"name": m.get("name")},
                commit=False,
            )

        # 标记演唱会推送成功
        for c in concerts:
            push_records.mark_push_success(
                conn,
                entity_type=push_records.ENTITY_TYPE_CONCERT,
                entity_id=c["concert_id"],
                channel=config.channel,
                entity_updated_at=c.get("updated_at"),
                content_hash=c.get("source_hash") or "",
                payload_snapshot={"title": c.get("title")},
                commit=False,
            )

        conn.commit()
        print(f"[PUSH] 成功完成 {total_items} 条实体推送记录登记。")
        return {"status": "success", "merchants": len(merchants), "concerts": len(concerts)}

    except Exception as exc:
        print(f"[PUSH ERROR] 推送失败: {exc}", file=sys.stderr)
        err_msg = str(exc)
        # 记录失败
        for m in merchants:
            push_records.mark_push_failed(
                conn,
                entity_type=push_records.ENTITY_TYPE_MERCHANT,
                entity_id=m["merchant_id"],
                error_message=err_msg,
                channel=config.channel,
                commit=False,
            )
        for c in concerts:
            push_records.mark_push_failed(
                conn,
                entity_type=push_records.ENTITY_TYPE_CONCERT,
                entity_id=c["concert_id"],
                error_message=err_msg,
                channel=config.channel,
                commit=False,
            )
        conn.commit()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="数据推送 Agent（飞书/Lark 简报汇报）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：调用 Agent 生成简报并打印，不发送 Webhook 也不写入 push_records",
    )
    parser.add_argument(
        "--channel",
        type=str,
        default=None,
        help="指定推送渠道（默认读取 PUSH_AGENT_CHANNEL 或 'feishu'）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制每种实体查询的最大条数",
    )
    args = parser.parse_args()

    config = PushAgentConfig.from_env()
    if args.channel:
        # 允许 CLI 临时覆盖渠道
        config = PushAgentConfig(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            timeout_s=config.timeout_s,
            retries=config.retries,
            lark_webhook_url=config.lark_webhook_url,
            lark_webhook_secret=config.lark_webhook_secret,
            channel=args.channel,
            max_items=config.max_items,
        )

    conn = connect(initialize=True)
    try:
        run_push_pipeline(conn, config, dry_run=args.dry_run, limit=args.limit)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
