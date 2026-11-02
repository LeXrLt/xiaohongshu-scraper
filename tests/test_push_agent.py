"""推送 Agent (push_agent) 单元测试。"""

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import pytest

from database import init_schema
import push_agent
import push_records


def test_push_agent_config_from_env(monkeypatch):
    """测试配置读取与优先级回退。"""
    monkeypatch.setenv("AGENT_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_API_KEY", "sk-agent-common")
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.common.com/v1")
    monkeypatch.setenv("AGENT_MODEL", "gpt-agent-common")
    monkeypatch.setenv("AGENT_TIMEOUT_S", "45")
    monkeypatch.setenv("AGENT_RETRIES", "3")
    monkeypatch.setenv("LARK_WEBHOOK_URL", "https://open.feishu.cn/hook/123")
    monkeypatch.setenv("LARK_WEBHOOK_SECRET", "secret-xyz")
    monkeypatch.setenv("PUSH_AGENT_CHANNEL", "feishu_test")
    monkeypatch.setenv("PUSH_AGENT_MAX_ITEMS", "25")

    cfg = push_agent.PushAgentConfig.from_env()
    assert cfg.provider == "openai"
    assert cfg.api_key == "sk-agent-common"
    assert cfg.base_url == "https://api.common.com/v1"
    assert cfg.model == "gpt-agent-common"
    assert cfg.timeout_s == 45
    assert cfg.retries == 3
    assert cfg.lark_webhook_url == "https://open.feishu.cn/hook/123"
    assert cfg.lark_webhook_secret == "secret-xyz"
    assert cfg.channel == "feishu_test"
    assert cfg.max_items == 25


def test_build_lark_card():
    """测试飞书卡片格式生成。"""
    card = push_agent.build_lark_card("### 测试简报", title="自定义标题", item_stats="上新 2 条")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "自定义标题"
    elements = card["card"]["elements"]
    assert any(el.get("tag") == "markdown" and "测试简报" in el.get("content", "") for el in elements)
    assert any("上新 2 条" in el.get("elements", [{}])[0].get("content", "") for el in elements if el.get("tag") == "note")


def test_send_to_lark_success(monkeypatch):
    """测试向飞书成功发送消息。"""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("push_agent.urlopen", return_value=mock_resp):
        res = push_agent.send_to_lark(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock",
            card_payload={"msg_type": "interactive", "card": {}},
            secret="test_secret",
        )
        assert res["code"] == 0


def test_send_to_lark_failure(monkeypatch):
    """测试飞书返回错误码时抛出异常。"""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"code": 19001, "msg": "bad request"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("push_agent.urlopen", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="飞书 Webhook 返回错误"):
            push_agent.send_to_lark(
                webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock",
                card_payload={"msg_type": "interactive"},
            )


def test_run_push_pipeline_dry_run(db_conn, monkeypatch):
    """测试 dry-run 模式：生成报告但不发 webhook，不写库。"""
    init_schema(db_conn)
    db_conn.execute("INSERT INTO merchants (merchant_id, canonical_name) VALUES ('m-mock', '测试干跑店')")
    db_conn.commit()

    config = push_agent.PushAgentConfig(
        provider="openai",
        api_key="sk-mock",
        base_url="https://api.openai.com/v1",
        model="mock-model",
        timeout_s=5,
        retries=0,
        lark_webhook_url="",
        lark_webhook_secret="",
        channel="feishu",
        max_items=10,
    )

    with patch("push_agent.generate_push_report", return_value="模拟报告内容"):
        result = push_agent.run_push_pipeline(db_conn, config, dry_run=True)
        assert result["status"] == "dry_run"
        assert result["merchants"] == 1
        assert "模拟报告内容" in result["report"]

    # 验证未写入 push_records
    assert not push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-mock", channel="feishu")


def test_run_push_pipeline_live_success(db_conn, monkeypatch):
    """测试真实推送模式：成功投递并记录 push_records。"""
    init_schema(db_conn)
    db_conn.execute("INSERT INTO merchants (merchant_id, canonical_name) VALUES ('m-live', '正式推送商户')")
    db_conn.execute("INSERT INTO ticketmaster_concerts (concert_id, title) VALUES ('c-live', '正式演唱会')")
    db_conn.commit()

    config = push_agent.PushAgentConfig(
        provider="openai",
        api_key="sk-mock",
        base_url="https://api.openai.com/v1",
        model="mock-model",
        timeout_s=5,
        retries=0,
        lark_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/mock",
        lark_webhook_secret="",
        channel="feishu",
        max_items=10,
    )

    with patch("push_agent.generate_push_report", return_value="模拟推送报告"), \
         patch("push_agent.send_to_lark", return_value={"code": 0}):
        result = push_agent.run_push_pipeline(db_conn, config, dry_run=False)
        assert result["status"] == "success"
        assert result["merchants"] == 1
        assert result["concerts"] == 1

    # 验证商户和演唱会均被标记为成功推送
    assert push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-live", channel="feishu")
    assert push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_CONCERT, "c-live", channel="feishu")

    # 再次查询待推送，两者均不再出现
    unpushed_m, unpushed_c = push_agent.fetch_unpushed_data(db_conn, channel="feishu")
    assert not any(m["merchant_id"] == "m-live" for m in unpushed_m)
    assert not any(c["concert_id"] == "c-live" for c in unpushed_c)
