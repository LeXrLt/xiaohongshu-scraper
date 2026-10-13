"""Real PostgreSQL tests for the recommendation API, using isolated test schemas."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web/backend"))
import auth
import main


@pytest.fixture
def merchant_client(db_conn, monkeypatch):
    db_conn.execute("""INSERT INTO merchant_categories (category_id, parent_id, name, aliases, status) VALUES
        ('dining', '', '餐饮', '[]', 'active'),
        ('dining.restaurant', 'dining', '餐厅', '["餐馆"]', 'active'),
        ('wellness', '', '美容休闲', '[]', 'active'),
        ('wellness.massage', 'wellness', '按摩', '[]', 'active'),
        ('old', 'dining', '停用分类', '[]', 'deprecated')""")
    db_conn.execute("""INSERT INTO merchant_tags (tag_id, name, description, status) VALUES
        ('sea', '海景', '窗边看海', 'active'), ('quiet', '安静', '', 'active'),
        ('unused', '未使用', '', 'active'), ('old', '停用标签', '', 'deprecated')""")
    db_conn.execute("""INSERT INTO merchants (merchant_id, canonical_name, city, district, reservation_info) VALUES
        ('m1', '海湾餐厅', '香港', '中环', '需提前两天预约'),
        ('m2', '简餐店', '香港', '旺角', ''),
        ('m3', '舒适按摩', '深圳', '南山', '可线上预约'),
        ('m4', '旧店', '香港', '中环', '')""")
    db_conn.execute("""INSERT INTO xhs_notes (note_id, title, content, note_url, published_at, video_summary) VALUES
        ('n1', '餐厅对比', '海湾餐厅可以看海，简餐店适合午餐。', 'https://example.com/n1', '2026-10-01', ''),
        ('n2', '海湾体验', '海湾餐厅有窗边座位。', 'https://example.com/n2', '', '海湾餐厅窗边体验'),
        ('n3', '舒适按摩体验', '舒适按摩可做肩颈放松。', 'https://example.com/n3', '2026-10-02', '')""")
    db_conn.execute("""INSERT INTO merchant_notes (merchant_id, note_id, relation_type, evidence_text) VALUES
        ('m1', 'n1', 'primary_review', '海湾餐厅可以看海'),
        ('m1', 'n1', 'comparison', '海湾餐厅可以看海'),
        ('m1', 'n2', 'mentioned', '海湾餐厅有窗边座位'),
        ('m2', 'n1', 'mentioned', '简餐店适合午餐'),
        ('m3', 'n3', 'primary_review', '舒适按摩可做肩颈放松')""")
    db_conn.execute("""INSERT INTO merchant_category_links (merchant_id, category_id, source_note_id) VALUES
        ('m1', 'dining.restaurant', 'n1'), ('m1', 'dining.restaurant', 'n2'),
        ('m1', 'dining', 'n1'), ('m2', 'dining.restaurant', 'n1'),
        ('m3', 'wellness.massage', 'n3'), ('m4', 'old', '')""")
    db_conn.execute("""INSERT INTO merchant_tag_links (merchant_id, tag_id, source_note_id) VALUES
        ('m1', 'sea', 'n1'), ('m1', 'sea', 'n2'), ('m1', 'quiet', 'n1'),
        ('m2', 'quiet', 'n1'), ('m1', 'old', 'n1')""")
    db_conn.commit()
    monkeypatch.setenv("API_AUTH_KEY", "test-merchant-api-0123456789abcdef0123456789")
    auth.get_auth_settings.cache_clear()
    try:
        with TestClient(main.app) as client:
            token = client.post('/api/auth/token', headers={'X-API-Key': auth.get_auth_settings().api_key})
            client.headers['Authorization'] = f"Bearer {token.json()['access_token']}"
            yield client
    finally:
        auth.get_auth_settings.cache_clear()


def test_categories_include_descendant_counts_without_duplicate_merchants(merchant_client):
    data = merchant_client.get('/api/merchant-categories').json()
    categories = {c['category_id']: c for c in data['items']}
    assert data['total'] == 4
    assert categories['dining']['merchant_count'] == 2
    assert categories['dining.restaurant']['merchant_count'] == 2
    assert categories['wellness']['merchant_count'] == 1
    assert 'old' not in categories
    filtered = merchant_client.get('/api/merchant-categories', params={'keyword': '餐馆'}).json()
    assert [c['category_id'] for c in filtered['items']] == ['dining.restaurant']


def test_tags_are_scoped_to_category_and_counts_are_distinct(merchant_client):
    data = merchant_client.get('/api/merchant-tags', params={'category_id': 'dining'}).json()
    assert {t['tag_id']: t['merchant_count'] for t in data['items']} == {'quiet': 2, 'sea': 1}
    assert merchant_client.get('/api/merchant-tags', params={'category_id': 'wellness'}).json()['total'] == 0
    all_tags = merchant_client.get('/api/merchant-tags').json()['items']
    assert {t['tag_id'] for t in all_tags} == {'quiet', 'sea', 'unused'}


def test_parent_category_and_all_tag_filters_are_combined(merchant_client):
    response = merchant_client.get('/api/merchants', params=[
        ('category_id', 'dining'), ('tag_ids', 'sea'), ('tag_ids', 'quiet'), ('tag_ids', 'sea'),
    ])
    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 1
    item = data['items'][0]
    assert item['merchant_id'] == 'm1'
    assert item['note_count'] == 2
    assert '海湾餐厅可以看海' in item['description']
    assert {t['tag_id'] for t in item['tags']} == {'sea', 'quiet'}
    assert item['reservation_info'] == '需提前两天预约'
    assert merchant_client.get('/api/merchants', params={'category_id': 'old'}).json()['total'] == 0
    assert merchant_client.get('/api/merchants', params={'tag_ids': 'old'}).json()['total'] == 0


@pytest.mark.parametrize('keyword,expected', [
    ('窗边看海', {'m1'}), ('肩颈', {'m3'}), ('提前两天', {'m1'}),
    ('午餐', {'m2'}), ('安静', {'m1', 'm2'}), ('不存在', set()), ("' OR 1=1 --", set()),
])
def test_keyword_matches_merchant_evidence_tags_and_reservations(merchant_client, keyword, expected):
    response = merchant_client.get('/api/merchants', params={'keyword': keyword})
    assert response.status_code == 200
    assert {m['merchant_id'] for m in response.json()['items']} == expected


def test_search_is_paginated_and_keeps_city_district_constraints(merchant_client):
    params = {'category_id': 'dining', 'city': '香港', 'page_size': 1}
    first = merchant_client.get('/api/merchants', params=params).json()
    second = merchant_client.get('/api/merchants', params={**params, 'page': 2}).json()
    assert first['total'] == second['total'] == 2
    assert first['items'][0]['merchant_id'] != second['items'][0]['merchant_id']
    response = merchant_client.get('/api/merchants', params={**params, 'district': '中环'}).json()
    assert [m['merchant_id'] for m in response['items']] == ['m1']
    assert merchant_client.get('/api/merchants', params={'category_id': 'missing'}).json()['total'] == 0


def test_details_paginate_sources_and_preserve_unpaginated_frontend_behavior(merchant_client):
    all_notes = merchant_client.get('/api/merchants/m1').json()
    assert len(all_notes['notes']) == 3
    assert all_notes['note_count'] == 2
    assert all_notes['notes_total'] == 3  # A note can have more than one relationship.
    assert all_notes['notes_has_more'] is False
    first = merchant_client.get('/api/merchants/m1', params={'notes_page_size': 2}).json()
    assert len(first['notes']) == 2
    assert first['notes_has_more'] is True
    last = merchant_client.get('/api/merchants/m1', params={'notes_page': 2, 'notes_page_size': 2}).json()
    assert len(last['notes']) == 1
    assert last['notes'][0]['note_id'] == 'n2'
    assert last['notes'][0]['video_summary'] == '海湾餐厅窗边体验'
    assert last['notes'][0]['content'] == '海湾餐厅有窗边座位。'
    assert last['notes_has_more'] is False
    assert merchant_client.get('/api/merchants/missing').status_code == 404


@pytest.mark.parametrize('path,params', [
    ('/api/merchants', [('tag_ids', 'x')] * 11),
    ('/api/merchants', {'tag_ids': ''}),
    ('/api/merchants', {'keyword': 'x' * 121}),
    ('/api/merchants/m1', {'notes_page_size': 51}),
    ('/api/merchants/m1', {'notes_page': 0}),
])
def test_query_limits_are_validated(merchant_client, path, params):
    assert merchant_client.get(path, params=params).status_code == 422
