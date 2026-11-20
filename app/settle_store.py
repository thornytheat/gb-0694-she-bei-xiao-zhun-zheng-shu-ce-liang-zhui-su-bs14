"""结算批持久化：草稿可重算、封账不可变、证据与付款台账只追加。"""
import json
import time
from decimal import Decimal

from app.errors import DomainError
from app.settlement import (
    apply_evidence, batch_id, base_records, cert_index, clean_currency,
    clean_evidence, clean_period, clean_supplier, clean_unit_price, compose,
    default_period, digest, evidence_key, ledger_id, money_text, next_period,
    paid_certs, period_bounds,
)
from app.store import connection, get_run, latest

DRAFT, CLOSED = '草稿', '已封账'
EVIDENCE_FIELDS = ('cert', 'kind', 'received_on', 'valid_from', 'valid_to',
                   'parent', 'device', 'alias', 'value', 'note')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS settlements(
        id TEXT PRIMARY KEY, supplier TEXT NOT NULL, period TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '草稿', revision INTEGER NOT NULL DEFAULT 1,
        decision TEXT NOT NULL DEFAULT '待复核', payload TEXT NOT NULL,
        base_run TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        closed_at TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS settlement_evidence(
        id TEXT PRIMARY KEY, settlement_id TEXT NOT NULL, cert TEXT NOT NULL,
        kind TEXT NOT NULL, received_on TEXT NOT NULL, payload TEXT NOT NULL,
        disposition TEXT NOT NULL, amount TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        recorded_at TEXT NOT NULL)''')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(settlement_evidence)')}
    if 'detail' not in columns:
        db.execute("ALTER TABLE settlement_evidence ADD COLUMN detail TEXT NOT NULL DEFAULT '{}'")
    db.execute('''CREATE TABLE IF NOT EXISTS settlement_carry(
        id TEXT PRIMARY KEY, settlement_id TEXT NOT NULL, source_batch TEXT NOT NULL,
        source_evidence TEXT NOT NULL, cert TEXT NOT NULL, kind TEXT NOT NULL,
        amount TEXT NOT NULL, currency TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS settlement_ledger(
        id TEXT PRIMARY KEY, settlement_id TEXT NOT NULL, kind TEXT NOT NULL,
        ref TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
        created_at TEXT NOT NULL)''')


def _rows(db, table, ident):
    return [dict(r) for r in db.execute(
        f'SELECT * FROM {table} WHERE settlement_id=? ORDER BY rowid', (ident,))]


def _document(row, evidence, carries, ledger):
    payload = json.loads(row['payload'])
    impacts = payload.get('impacts', [])
    by_key = {tuple(i['key']): i for i in impacts}
    entries = []
    for entry in evidence:
        item = json.loads(entry['payload'])
        impact = by_key.get(tuple(evidence_key(item)))
        detail = json.loads(entry.get('detail') or '{}')
        entries.append({**item, 'id': entry['id'], 'disposition': entry['disposition'],
                        'amount': entry['amount'], 'recorded_at': entry['recorded_at'],
                        'affected': detail.get('affected', impact['affected'] if impact else 0),
                        'scope': detail.get('scope', impact['scope'] if impact else []),
                        'changes': detail.get('changes', impact['changes'] if impact else [])})
    return {
        'id': row['id'], 'supplier': row['supplier'], 'period': row['period'],
        'status': row['status'], 'revision': row['revision'],
        'decision': row['decision'], 'created_at': row['created_at'],
        'updated_at': row['updated_at'], 'closed_at': row['closed_at'],
        'base_run': row['base_run'],
        'unit_price': payload['unit_price'], 'currency': payload['currency'],
        'snapshot': payload['snapshot'], 'impacts': impacts, 'evidence': entries,
        'carries': [{k: c[k] for k in ('id', 'source_batch', 'source_evidence', 'cert',
                                       'kind', 'amount', 'currency', 'reason', 'created_at')}
                    for c in carries],
        'ledger': [{k: l[k] for k in ('id', 'kind', 'ref', 'amount', 'currency',
                                      'created_at')} for l in ledger],
    }


def get(ident):
    with connection() as db:
        _ensure_schema(db)
        row = db.execute('SELECT * FROM settlements WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '结算批不存在', 404)
        return _document(row, _rows(db, 'settlement_evidence', ident),
                         _rows(db, 'settlement_carry', ident),
                         _rows(db, 'settlement_ledger', ident))


def list_batches():
    with connection() as db:
        _ensure_schema(db)
        rows = db.execute('SELECT * FROM settlements ORDER BY rowid DESC').fetchall()
        return [{k: r[k] for k in ('id', 'supplier', 'period', 'status', 'revision',
                                   'decision', 'base_run', 'created_at', 'closed_at')}
                for r in rows]


def _payload(supplier, period, run_id, price, currency, snapshot, impacts):
    return {'supplier': supplier, 'period': period, 'base_run': run_id,
            'unit_price': money_text(price), 'currency': currency,
            'snapshot': snapshot, 'impacts': impacts}


def _stored_items(db, ident):
    return [{k: json.loads(row['payload']).get(k) for k in EVIDENCE_FIELDS}
            for row in _rows(db, 'settlement_evidence', ident)]


def _carry_items(db, ident):
    return [{'id': c['id'], 'source_batch': c['source_batch'],
             'source_evidence': c['source_evidence'], 'cert': c['cert'],
             'kind': c['kind'], 'amount': c['amount'], 'currency': c['currency'],
             'reason': c['reason']} for c in _rows(db, 'settlement_carry', ident)]


def _compose(supplier, period, run, price, currency, items, db):
    start, end = period_bounds(period)
    base, excluded = base_records(run, start, end)
    return compose(base, cert_index(run), items,
                   _carry_items(db, batch_id(supplier, period)),
                   price, currency, excluded)


def _store_evidence(db, ident, item, disposition, amount):
    eid = digest(ident, 'evidence', *evidence_key(item))[:24]
    db.execute('INSERT OR IGNORE INTO settlement_evidence(id,settlement_id,cert,kind,'
               'received_on,payload,disposition,amount,recorded_at) '
               'VALUES (?,?,?,?,?,?,?,?,?)',
               (eid, ident, item['cert'], item['kind'], item['received_on'],
                json.dumps({k: item.get(k) for k in EVIDENCE_FIELDS}, ensure_ascii=False),
                disposition, amount, _now()))
    return eid


def _store_detail(db, ident, item, detail):
    db.execute('UPDATE settlement_evidence SET detail=? WHERE id=?',
               (json.dumps(detail, ensure_ascii=False),
                digest(ident, 'evidence', *evidence_key(item))[:24]))


def _sync_amounts(db, ident, items, impacts):
    amounts = {tuple(i['key']): i for i in impacts}
    for item in items:
        impact = amounts.get(evidence_key(item))
        db.execute('UPDATE settlement_evidence SET amount=?,detail=? WHERE id=?',
                   ((impact or {}).get('amount', '0.00'),
                    json.dumps({k: impact[k] for k in ('affected', 'scope', 'changes')}
                               if impact else {}, ensure_ascii=False),
                    digest(ident, 'evidence', *evidence_key(item))[:24]))


def _clean_list(raw, certs):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DomainError('EVIDENCE', '晚到证据必须是数组')
    return [clean_evidence(item, certs) for item in raw]


def ensure(supplier, period, run_id=None, unit_price=None, currency=None, evidence=None):
    supplier = clean_supplier(supplier)
    run = get_run(run_id) if run_id else latest()
    if run is None:
        raise DomainError('NOT_FOUND', '没有可用的测量批次', 404)
    if 'verdicts' not in run:
        raise DomainError('RUN', '测量批次尚未完成裁决', 422)
    period = clean_period(period if str(period or '').strip() else default_period(run))
    currency = clean_currency(currency)
    price = clean_unit_price(unit_price)
    ident = batch_id(supplier, period)
    with connection() as db:
        _ensure_schema(db)
        row = db.execute('SELECT * FROM settlements WHERE id=?', (ident,)).fetchone()
        if row is not None and row['status'] == CLOSED:
            closed = _document(row, _rows(db, 'settlement_evidence', ident),
                               _rows(db, 'settlement_carry', ident), [])
            late = _late_items(closed, run) + _clean_list(evidence, closed['snapshot']['certs'])
            for item in late:
                amount = _late_amounts(db, closed, [item], price).get(evidence_key(item), '0.00')
                level = _late_scope(closed['snapshot'], item)
                _record_late(db, closed, item, amount, currency, level)
            reused = True
        else:
            stored = _stored_items(db, ident)
            known = {evidence_key(item) for item in stored}
            items = stored
            for item in _clean_list(evidence, cert_index(run)):
                if evidence_key(item) in known:
                    continue
                known.add(evidence_key(item))
                items.append(item)
            snapshot, impacts = _compose(supplier, period, run, price, currency, items, db)
            encoded = json.dumps(_payload(supplier, period, run['manifest']['run_id'],
                                          price, currency, snapshot, impacts),
                                 ensure_ascii=False, sort_keys=True)
            now = _now()
            if row is None:
                db.execute('INSERT INTO settlements(id,supplier,period,status,revision,decision,'
                           'payload,base_run,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (ident, supplier, period, DRAFT, 1, '待复核', encoded,
                            run['manifest']['run_id'], now, now))
                reused = False
            else:
                old = json.loads(row['payload'])
                changed = json.dumps(old, ensure_ascii=False, sort_keys=True) != encoded
                db.execute('UPDATE settlements SET status=?,revision=?,decision=?,payload=?,'
                           'base_run=?,updated_at=? WHERE id=?',
                           (DRAFT, row['revision'] + (1 if changed else 0),
                            '待复核' if changed else row['decision'], encoded,
                            run['manifest']['run_id'], now, ident))
                reused = not changed
            for item in items:
                _store_evidence(db, ident, item, '本批', '0.00')
            _sync_amounts(db, ident, items, impacts)
        db.commit()
    document = get(ident)
    document['reused'] = reused
    return document


def _sequential_amounts(snapshot, items, price):
    """按收到顺序在冻结快照上叠加重算，返回每份证据的边际金额。

    同一撤销源的多个下游证书只在源头计一次，下游记 0，避免重复扣回。
    """
    from copy import deepcopy
    records = deepcopy(snapshot['measurements'])
    certs = deepcopy(snapshot['certs'])
    ordered = sorted(items, key=lambda e: (e['received_on'],) + evidence_key(e))
    amounts = []
    for item in ordered:
        before = paid_certs(records)
        apply_evidence(records, certs, [item], price)
        amounts.append(money_text((len(paid_certs(records)) - len(before)) * price))
    return ordered, amounts


def _late_amounts(db, closed, new_items, price):
    """冻结快照上的边际金额：先重放已登记证据，再算新证据的增量，避免重复扣回。"""
    existing = _stored_items(db, closed['id'])
    known = {evidence_key(item) for item in existing}
    fresh = [item for item in new_items if evidence_key(item) not in known]
    ordered, amounts = _sequential_amounts(closed['snapshot'], existing + fresh, price)
    found = {tuple(evidence_key(item)): amount for item, amount in zip(ordered, amounts)}
    return {evidence_key(item): found.get(tuple(evidence_key(item)), '0.00')
            for item in fresh}


def _revoked_root(cert, certs):
    """沿父链找最近的实际撤销源，晚到撤销按源头登记，影响范围才完整。"""
    seen = set()
    node = cert
    while node and node not in seen:
        seen.add(node)
        info = certs.get(node)
        if not info:
            return None
        if info.get('revoked') == 'yes':
            return node
        node = info.get('parent')
    return None


def _late_items(closed, run):
    """对比冻结快照与新批次：已付转非应付记撤销/改证，暂扣转应付记补付证据。"""
    certs = cert_index(run)
    current = {}
    for verdict in run.get('verdicts', []):
        current.setdefault(verdict['证书'], []).extend(
            x for x in str(verdict.get('原因码') or '').split(',')
            if x and x != 'ACCEPT')
    measured = {record['cert'] for record in closed['snapshot']['measurements']}
    was_paid = paid_certs(closed['snapshot']['measurements'])
    now_paid = {cert for cert in measured
                if cert in current and not current[cert]}
    stamp = (run['manifest'].get('created_at') or _now())[:10]
    note = f"来源批次 {run['manifest']['run_id']}"
    late, seen = [], set()

    def add(item):
        key = evidence_key(item)
        if key not in seen:
            seen.add(key)
            late.append(item)

    for cert in sorted(was_paid - now_paid):
        reasons = current.get(cert) or []
        source = _revoked_root(cert, certs) if 'REVOKED' in reasons else None
        if source:
            add({'cert': source, 'kind': 'revocation', 'received_on': stamp,
                 'valid_from': None, 'valid_to': None, 'parent': None,
                 'device': None, 'alias': None, 'value': None, 'note': note})
            continue
        info = certs.get(cert) or {}
        add({'cert': cert, 'kind': 'amendment', 'received_on': stamp,
             'valid_from': info.get('valid_from'), 'valid_to': info.get('valid_to'),
             'parent': None, 'device': None, 'alias': None, 'value': None,
             'note': note})
    for cert in sorted(now_paid - was_paid):
        info = certs.get(cert) or {}
        add({'cert': cert, 'kind': 'amendment', 'received_on': stamp,
             'valid_from': info.get('valid_from'), 'valid_to': info.get('valid_to'),
             'parent': None, 'device': None, 'alias': None, 'value': None,
             'note': note + ' 补付'})
    return late


def _carry_target(db, supplier, period):
    """沿结算期向后找到第一个未封账的批，晚到影响记到那里扣回/补付。"""
    target = next_period(period)
    for _ in range(240):
        row = db.execute('SELECT status FROM settlements WHERE id=?',
                         (batch_id(supplier, target),)).fetchone()
        if row is None or row['status'] != CLOSED:
            return target
        target = next_period(target)
    raise DomainError('PERIOD', '无法确定扣回结算期')


def _late_scope(snapshot, item):
    """在冻结快照上取该证据的影响范围与明细变化，供页面复核。"""
    from copy import deepcopy
    certs = deepcopy(snapshot['certs'])
    _, impacts = compose(snapshot['base'], certs, [item], snapshot['adjustments'],
                         1, snapshot['totals']['currency'], snapshot['excluded'])
    impact = impacts[0] if impacts else {'affected': 0, 'scope': [], 'changes': []}
    return {'affected': impact['affected'], 'scope': impact['scope'],
            'changes': impact['changes']}


def _record_late(db, closed, item, amount, currency, detail=None):
    successor = _carry_target(db, closed['supplier'], closed['period'])
    eid = _store_evidence(db, closed['id'], item, f'转 {successor} 期扣回/补付', amount)
    if detail:
        _store_detail(db, closed['id'], item, detail)
    if Decimal(amount) == 0:
        return
    reason = 'LATE_REVOCATION' if item['kind'] == 'revocation' else 'LATE_AMENDMENT'
    db.execute('INSERT OR IGNORE INTO settlement_carry(id,settlement_id,source_batch,'
               'source_evidence,cert,kind,amount,currency,reason,created_at) '
               'VALUES (?,?,?,?,?,?,?,?,?,?)',
               (digest(batch_id(closed['supplier'], successor), 'carry', eid)[:24],
                batch_id(closed['supplier'], successor), closed['id'], eid, item['cert'],
                item['kind'], amount, currency, reason, _now()))


def record_evidence(ident, raw):
    document = get(ident)
    item = clean_evidence(raw, document['snapshot']['certs'])
    price = Decimal(document['unit_price'])
    reused = False
    with connection() as db:
        _ensure_schema(db)
        row = db.execute('SELECT * FROM settlements WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '结算批不存在', 404)
        if row['status'] == CLOSED:
            amount = _late_amounts(db, document, [item], price).get(evidence_key(item), '0.00')
            _record_late(db, document, item, amount, document['currency'],
                         _late_scope(document['snapshot'], item))
            reused = True
        else:
            known = {evidence_key(entry) for entry in document['evidence']}
            reused = evidence_key(item) in known
            if not reused:
                stored = _stored_items(db, ident) + [item]
                run = get_run(row['base_run'])
                snapshot, impacts = _compose(row['supplier'], row['period'], run, price,
                                             document['currency'], stored, db)
                encoded = json.dumps(_payload(row['supplier'], row['period'], row['base_run'],
                                              price, document['currency'], snapshot, impacts),
                                     ensure_ascii=False, sort_keys=True)
                old = json.loads(row['payload'])
                changed = json.dumps(old, ensure_ascii=False, sort_keys=True) != encoded
                db.execute('UPDATE settlements SET revision=?,decision=?,payload=?,updated_at=? '
                           'WHERE id=?',
                           (row['revision'] + (1 if changed else 0),
                            '待复核' if changed else row['decision'], encoded, _now(), ident))
                for entry in stored:
                    _store_evidence(db, ident, entry, '本批', '0.00')
                _sync_amounts(db, ident, stored, impacts)
                reused = not changed
        db.commit()
    return get(ident), reused


def review(ident, revision, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核动作无效')
    with connection() as db:
        _ensure_schema(db)
        row = db.execute('SELECT status FROM settlements WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '结算批不存在', 404)
        if row['status'] == CLOSED:
            raise DomainError('CLOSED', '已封账的结算批不能再复核', 409)
        changed = db.execute('UPDATE settlements SET revision=revision+1,decision=?,'
                             'updated_at=? WHERE id=? AND revision=?',
                             (decision, _now(), ident, revision)).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
        db.commit()
    return get(ident)


def post(ident, revision):
    document = get(ident)
    if document['status'] == CLOSED:
        document['reused'] = True
        return document
    if document['decision'] != '通过':
        raise DomainError('REVIEW', '结算批尚未复核通过，不能封账', 409)
    if document['revision'] != revision:
        raise DomainError('VERSION', '版本已变化，请重新载入', 409)
    totals = document['snapshot']['totals']
    now = _now()
    with connection() as db:
        _ensure_schema(db)
        changed = db.execute('UPDATE settlements SET status=?,closed_at=?,updated_at=? '
                             'WHERE id=? AND revision=? AND status=?',
                             (CLOSED, now, now, ident, revision, DRAFT)).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
        entries = [('PAY', 'payable', totals['payable']),
                   ('WITHHOLD', 'hold', totals['hold'])]
        if Decimal(totals['adjustment']) != 0:
            entries.append(('CARRY', 'adjustment', totals['adjustment']))
        for kind, ref, amount in entries:
            db.execute('INSERT OR IGNORE INTO settlement_ledger(id,settlement_id,kind,ref,'
                       'amount,currency,created_at) VALUES (?,?,?,?,?,?,?)',
                       (ledger_id(ident, kind, ref), ident, kind, ref, amount,
                        totals['currency'], now))
        db.commit()
    document = get(ident)
    document['reused'] = False
    return document
