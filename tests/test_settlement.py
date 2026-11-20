import pytest

from app.pipeline import calculate
from app.settle_store import ensure, get, post, record_evidence, review
from app.settlement import (
    base_records, batch_id, cert_index, clean_evidence, compose,
    group_lines, next_period, paid_certs, period_bounds, status_for,
)


@pytest.fixture
def batch(client):
    client.post('/api/run', json={})
    return client.post('/api/settlements', json={'supplier': '供应商甲'}).get_json()


def test_batch_identity_is_supplier_and_period():
    assert batch_id('甲', '2026-11') == batch_id('甲', '2026-11')
    assert batch_id('甲', '2026-11') != batch_id('乙', '2026-11')
    assert batch_id('甲', '2026-11') != batch_id('甲', '2026-12')


def test_period_bounds_are_half_open():
    start, end = period_bounds('2026-11')
    assert start.isoformat() == '2026-11-01' and end.isoformat() == '2026-12-01'
    assert next_period('2026-12') == '2027-01'


def test_rerun_same_batch_does_not_double_book(client, batch):
    again = client.post('/api/settlements', json={'supplier': '供应商甲'}).get_json()
    assert again['id'] == batch['id'] and again['reused'] is True
    assert again['revision'] == batch['revision']
    assert len(client.get('/api/settlements').get_json()) == 1


def test_line_is_per_certificate_and_alias_does_not_split(client):
    run = calculate()
    base, _ = base_records(run)
    certs = cert_index(run)
    alias = clean_evidence({'cert': 'C0001', 'kind': 'amendment', 'alias': '仪器1-代记'}, certs)
    snapshot, _ = compose(base, certs, [alias], [], 100, 'CNY')
    c0001 = [line for line in snapshot['lines'] if line['cert'] == 'C0001']
    assert len(c0001) == 1 and c0001[0]['count'] == 1
    assert '仪器1-代记' in c0001[0]['aliases']


def test_alias_amendment_recomputes_ownership_and_window(client):
    run = calculate()
    base, _ = base_records(run)
    certs = cert_index(run)
    before = {line['cert']: line['status'] for line in group_lines(base, certs, 100)}
    assert before['C0001'] == 'PAY'
    merge = clean_evidence({'cert': 'C0001', 'kind': 'amendment', 'alias': '仪器3'}, certs)
    snapshot, impacts = compose(base, certs, [merge], [], 100, 'CNY')
    after = {line['cert']: line['status'] for line in snapshot['lines']}
    # 同一设备的别名并组后，C0001 与 C0003 的证书有效期重叠，不能再按可采用结算。
    assert after['C0001'] == 'HOLD' and after['C0003'] == 'HOLD'
    assert impacts[0]['affected'] >= 2


def test_late_revocation_before_close_recomputes_batch(client, batch):
    cert = 'C0001'
    assert cert in paid_certs(get(batch['id'])['snapshot']['measurements'])
    updated, _ = record_evidence(batch['id'], {'cert': cert, 'kind': 'revocation',
                                               'received_on': '2026-11-20'})
    assert cert not in paid_certs(updated['snapshot']['measurements'])
    assert updated['revision'] == batch['revision'] + 1
    assert updated['decision'] == '待复核'
    assert updated['evidence'][0]['disposition'] == '本批'


def test_duplicate_late_evidence_is_idempotent(client, batch):
    item = {'cert': 'C0002', 'kind': 'revocation', 'received_on': '2026-11-20'}
    first, reused = record_evidence(batch['id'], item)
    second, reused_again = record_evidence(batch['id'], item)
    assert reused is False and reused_again is True
    assert len(second['evidence']) == 1
    assert first['snapshot']['totals'] == second['snapshot']['totals']


def test_review_then_post_is_single_ledger_entry(client, batch):
    passed = review(batch['id'], batch['revision'], '通过')
    assert passed['decision'] == '通过'
    closed = post(batch['id'], passed['revision'])
    assert closed['status'] == '已封账'
    assert {entry['kind'] for entry in closed['ledger']} >= {'PAY', 'WITHHOLD'}
    again = post(batch['id'], passed['revision'])
    assert again['reused'] is True and len(again['ledger']) == len(closed['ledger'])


def test_post_requires_review(client, batch):
    with pytest.raises(Exception):
        post(batch['id'], batch['revision'])
    assert get(batch['id'])['status'] == '草稿'


def test_late_evidence_after_close_goes_to_next_period(client, batch):
    passed = review(batch['id'], batch['revision'], '通过')
    closed = post(batch['id'], passed['revision'])
    ledger_before = closed['ledger']
    totals_before = closed['snapshot']['totals']
    amended = record_evidence(batch['id'], {'cert': 'C0001', 'kind': 'revocation',
                                            'received_on': '2026-11-25'})[0]
    assert amended['status'] == '已封账'
    assert amended['ledger'] == ledger_before
    assert amended['snapshot']['totals'] == totals_before
    assert amended['evidence'][0]['disposition'].startswith('转 2026-12')
    # C0001 已付、C0021 本就超量程拒付，因此只扣回 C0001 一份。
    assert amended['evidence'][0]['amount'] == '-100.00'
    assert 'C0021' in amended['evidence'][0]['scope']

    created = ensure('供应商甲', '2026-12')
    assert created['snapshot']['totals']['adjustment'] == '-100.00'
    assert [carry['cert'] for carry in created['carries']] == ['C0001']


def test_closed_batch_cannot_be_reviewed_again(client, batch):
    passed = review(batch['id'], batch['revision'], '通过')
    post(batch['id'], passed['revision'])
    with pytest.raises(Exception):
        review(batch['id'], passed['revision'] + 1, '退回')


def test_period_filter_excludes_other_months(client, batch):
    december = ensure('供应商甲', '2026-12')
    assert december['snapshot']['totals']['cert_count'] == 0
    assert len(december['snapshot']['excluded']) == 42
    assert december['snapshot']['totals']['payable'] == '0.00'


def test_revocation_cascades_through_references(client):
    run = calculate()
    base, _ = base_records(run)
    certs = cert_index(run)
    revoke = clean_evidence({'cert': 'ROOT', 'kind': 'revocation'}, certs)
    snapshot, _ = compose(base, certs, [revoke], [], 100, 'CNY')
    assert snapshot['totals']['pay_count'] == 0


def test_pages_and_csv_are_reviewable(client, batch):
    page = client.get('/settlement?id=' + batch['id'])
    assert page.status_code == 200
    text = page.data.decode()
    assert '按证书结算明细' in text and '晚到证据去向' in text and '付款凭证' in text
    csv_text = client.get(f"/api/settlements/{batch['id']}/evidence.csv").data.decode('utf-8-sig')
    assert '结算明细' in csv_text and '原始行号' in csv_text
    assert 'C0001' in csv_text


def test_invalid_period_and_currency_rejected(client, batch):
    assert client.post('/api/settlements', json={'supplier': '甲', 'period': '2026-13'}).status_code == 422
    assert client.post('/api/settlements', json={'supplier': '甲', 'currency': '人民币'}).status_code == 422
    assert client.post('/api/settlements', json={'supplier': ''}).status_code == 422


def test_unknown_evidence_certificate_rejected(client, batch):
    response = client.post(f"/api/settlements/{batch['id']}/evidence",
                           json={'cert': 'NOPE', 'kind': 'revocation'})
    assert response.status_code == 422 and response.get_json()['code'] == 'CERT'


def test_status_precedence():
    assert status_for([]) == 'PAY'
    assert status_for(['INVALID_WINDOW']) == 'HOLD'
    assert status_for(['OUT_OF_RANGE']) == 'REJECT'
    assert status_for(['INVALID_WINDOW', 'OUT_OF_RANGE']) == 'REJECT'


def test_upstream_revocation_after_close_records_source_not_every_child(client):
    from app.config import SAMPLE
    client.post('/api/run', json={})
    created = client.post('/api/settlements', json={'supplier': '甲'}).get_json()
    passed = review(created['id'], created['revision'], '通过')
    post(created['id'], passed['revision'])
    text = SAMPLE.read_text(encoding='utf-8-sig').replace(
        'C0001,D0001,仪器1,ROOT,2026-01-01,2027-01-01,0,10,V,no',
        'C0001,D0001,仪器1,ROOT,2026-01-01,2027-01-01,0,10,V,yes')
    client.post('/api/run', json={'csv': text})
    again = client.post('/api/settlements', json={'supplier': '甲'}).get_json()
    assert [item['cert'] for item in again['evidence']] == ['C0001']
    # C0021 本就超量程拒付，撤销 C0001 只扣回 C0001 一份。
    assert again['evidence'][0]['amount'] == '-100.00'
    assert again['evidence'][0]['scope'] == ['C0001', 'C0021']


def test_carry_is_not_multiplied_by_repeated_reruns(client):
    client.post('/api/run', json={})
    created = client.post('/api/settlements', json={'supplier': '甲'}).get_json()
    passed = review(created['id'], created['revision'], '通过')
    post(created['id'], passed['revision'])
    for _ in range(3):
        client.post(f"/api/settlements/{created['id']}/evidence",
                    json={'cert': 'C0006', 'kind': 'revocation'})
    successor = ensure('甲', '2026-12')
    assert successor['snapshot']['totals']['adjustment'] == '-200.00'
    assert len(successor['carries']) == 1
