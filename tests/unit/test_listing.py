import asyncio
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from ecomops.core import llm
from ecomops.core.auth import issue_token
from ecomops.core.errors import install_error_handlers
from ecomops.core.storage import LocalStore
from ecomops.listing.checkers import extract_pdf, extract_structure, review_document
from ecomops.listing.router import router

HEATER = '品名：储水式热水器\n额定功率：2000W\n容量：60L\n能效等级：一级\n3C：20241234567890'
SHAMPOO = '品名：洗发水\n净含量：500ml\n保质期：三年\n备案号：粤妆网备字2024123456'


def pdf(text, two_columns=False):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    stream = BytesIO()
    c = canvas.Canvas(stream)
    c.setFont('STSong-Light', 12)
    for i, line in enumerate(text.splitlines()):
        c.drawString(40, 790 - i * 24, line)
    if two_columns:
        c.drawString(320, 790, '右栏第一行')
        c.drawString(320, 766, '右栏第二行')
    c.save()
    return stream.getvalue()


def headers(tenant='tenant-a', role='operator'):
    return {'Authorization': f'Bearer {issue_token("tester", role, tenant)}'}


@pytest.fixture(autouse=True)
def reset_failures():
    llm.set_failures([])
    yield
    llm.set_failures([])


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr('ecomops.listing.router.get_store', lambda: LocalStore(str(tmp_path)))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    with TestClient(app) as c:
        yield c


CASES = [
    (HEATER + '\n行业第一', 'HOME_APPLIANCE_HEATER', 'REJECTED_BLOCKING'),
    (SHAMPOO + '\n治疗头皮问题', 'PERSONAL_SHAMPOO', 'REJECTED_BLOCKING'),
    (SHAMPOO + '\n防脱', 'PERSONAL_SHAMPOO', 'REJECTED_BLOCKING'),
    (HEATER.replace('能效等级：一级\n', ''), 'HOME_APPLIANCE_HEATER', 'REJECTED_BLOCKING'),
    (HEATER.replace('20241234567890', 'ABC'), 'HOME_APPLIANCE_HEATER', 'PENDING_MANUAL_VERIFY'),
    (HEATER, 'HOME_APPLIANCE_HEATER', 'PASSED_WITH_SUGGESTIONS'),
    (SHAMPOO, 'PERSONAL_SHAMPOO', 'PASSED_WITH_SUGGESTIONS'),
    ('品名：毛巾\n规格：一条', 'DAILY_GENERAL', 'PASSED_WITH_SUGGESTIONS'),
    ('品名：新品', 'UNKNOWN', 'PASSED_WITH_SUGGESTIONS'),
]


@pytest.mark.parametrize('content,category,verdict', CASES)
def test_listing_rules_without_database(content, category, verdict):
    document = extract_structure(extract_pdf(pdf(content)))
    report = asyncio.run(review_document(document, category))
    assert report['verdict'] == verdict
    assert report['fallback_used'] is False
    if verdict == 'PASSED_WITH_SUGGESTIONS':
        assert report['advisory_suggestions']


@pytest.mark.parametrize('checker', ['extreme_words', 'required_params', 'medical_claims', 'cert_extract', 'readability'])
def test_listing_each_fallback(checker):
    llm.set_failures([checker])
    report = asyncio.run(review_document(extract_structure(HEATER), 'HOME_APPLIANCE_HEATER'))
    assert report['fallback_used'] is True
    assert report['verdict'] == 'PENDING_MANUAL_VERIFY'
    assert any(c['checker'] == checker and c['fallback_used'] for c in report['checks'])


def test_listing_two_column_order():
    extracted = extract_pdf(pdf('左栏第一行\n左栏第二行', two_columns=True))
    assert extracted.index('左栏第二行') < extracted.index('右栏第一行')


@pytest.mark.parametrize('content,category,verdict', CASES)
def test_listing_create_persist(db_name, client, content, category, verdict):
    response = client.post('/api/v1/listing/reviews', headers=headers(),
                           files={'file': ('listing.pdf', pdf(content), 'application/pdf')},
                           data={'declared_category': category})
    assert response.status_code == 201, response.text
    report = response.json()
    assert report['verdict'] == verdict
    assert report['advisory_suggestions']
    assert client.get('/api/v1/listing/reviews/' + report['review_id'], headers=headers()).json() == report
    assert client.get('/api/v1/listing/reviews', headers=headers()).json()['reviews'] == [report]


def test_listing_fallback_persist(db_name, client):
    llm.set_failures(['cert_extract'])
    response = client.post('/api/v1/listing/reviews', headers=headers(),
                           files={'file': ('listing.pdf', pdf(HEATER), 'application/pdf')},
                           data={'declared_category': 'HOME_APPLIANCE_HEATER'})
    assert response.status_code == 201, response.text
    assert response.json()['fallback_used'] is True
    assert response.json()['verdict'] == 'PENDING_MANUAL_VERIFY'


def test_listing_tenant_isolation(db_name, client):
    response = client.post('/api/v1/listing/reviews', headers=headers(),
                           files={'file': ('listing.pdf', pdf(HEATER), 'application/pdf')},
                           data={'declared_category': 'HOME_APPLIANCE_HEATER'})
    assert response.status_code == 201, response.text
    key = response.json()['review_id']
    assert client.get('/api/v1/listing/reviews/' + key, headers=headers('tenant-b')).status_code == 404
    assert client.get('/api/v1/listing/reviews', headers=headers('tenant-b')).json() == {'reviews': []}


@pytest.mark.parametrize('data,mime', [(b'hello', 'text/plain'), (b'', 'application/pdf'), (b'%PDF-invalid', 'application/pdf')])
def test_listing_invalid_file(client, data, mime):
    response = client.post('/api/v1/listing/reviews', headers=headers(),
                           files={'file': ('file.pdf', data, mime)}, data={'declared_category': 'DAILY_GENERAL'})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'UNSUPPORTED_FILE'


def test_listing_roles(client):
    for path in ('/api/v1/listing/reviews', '/api/v1/listing/reviews/invalid'):
        assert client.get(path, headers=headers(role='cs_agent')).status_code == 403
        assert client.get(path).status_code == 401
    assert client.post('/api/v1/listing/reviews', headers=headers(role='cs_agent'),
                       files={'file': ('a.pdf', pdf(HEATER), 'application/pdf')},
                       data={'declared_category': 'HOME_APPLIANCE_HEATER'}).status_code == 403
