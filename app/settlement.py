"""结算批规则：按证书归并测量，划分应付/暂扣/拒付，并处理晚到证据。

这里只做纯计算，不接触数据库。结算语义见 docs/DATA-DICTIONARY.md：

- 一个供应商一个结算期只有一个结算批，批次身份只由供应商与结算期决定。
- 按证书结算：同一证书本期测量整体定为应付/暂扣/拒付，换设备别名或代记不拆分。
- 晚到撤销/改证会改写证书簿，已出的测量按证书簿重算有效区间、量程与溯源；
  封账前的重算直接进本批，封账后只登记影响并转入后续结算期扣回/补付。
"""
import hashlib
import re
from collections import Counter
from copy import deepcopy
from datetime import date
from decimal import Decimal

from app.config import RULE_VERSION
from app.domain import day, descendants, equivalence, trace
from app.errors import DomainError
from app.numeric import fmt, number

STATUS_LABELS = {'PAY': '应付', 'HOLD': '暂扣', 'REJECT': '拒付'}
KIND_LABELS = {'revocation': '撤销', 'amendment': '改证'}
HOLD_CODES = {'INVALID_WINDOW', 'REFERENCE_CONFLICT', 'MISSING_REFERENCE',
              'REFERENCE_EXPIRED', 'CYCLE'}
REJECT_CODES = {'REVOKED', 'OUT_OF_RANGE'}
PERIOD_RE = re.compile(r'^(\d{4})-(\d{2})$')
CURRENCY_RE = re.compile(r'^[A-Z]{3}$')


def digest(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode('utf-8')).hexdigest()


def batch_id(supplier, period):
    """结算批身份：只取决于供应商和结算期，重跑/换文件都落到同一批。"""
    return digest('settlement', supplier, period)[:16]


def ledger_id(ident, kind, ref):
    return digest(ident, 'ledger', kind, ref)[:24]


def evidence_key(item):
    """证据的稳定身份：同一证书、类型、收到日与变更内容只记一次。"""
    return (item['cert'], item['kind'], item['received_on'],
            item.get('valid_from') or '', item.get('valid_to') or '',
            item.get('parent') or '', item.get('device') or '',
            item.get('alias') or '', item.get('value') or '')


def clean_supplier(value):
    text = str(value or '').strip()
    if not text:
        raise DomainError('SUPPLIER', '必须指定供应商')
    if len(text) > 64:
        raise DomainError('SUPPLIER', '供应商名称过长')
    return text


def period_bounds(period):
    match = PERIOD_RE.fullmatch(str(period or '').strip())
    if not match:
        raise DomainError('PERIOD', '结算期格式应为 YYYY-MM')
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise DomainError('PERIOD', '结算期月份无效')
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def clean_period(value):
    period_bounds(value)
    return str(value).strip()


def next_period(period):
    period_bounds(period)
    year, month = int(period[:4]), int(period[5:])
    return f'{year + 1}-01' if month == 12 else f'{year}-{month + 1:02d}'


def default_period(run):
    dates = [v.get('测量日') for v in run.get('verdicts', []) if v.get('测量日')]
    if not dates:
        raise DomainError('PERIOD', '无法从批次推断结算期，请显式指定 period')
    return max(dates)[:7]


def clean_currency(value):
    text = str(value or 'CNY').strip().upper()
    if not CURRENCY_RE.fullmatch(text):
        raise DomainError('CURRENCY', '币种应为三位大写字母')
    return text


def clean_unit_price(value):
    if value is None or str(value).strip() == '':
        return Decimal('100.00')
    price = number(value)
    if price < 0:
        raise DomainError('PRICE', '结算单价不能为负')
    return price


def clean_evidence(raw, certs, today=None):
    if not isinstance(raw, dict):
        raise DomainError('EVIDENCE', '晚到证据必须是对象')
    cert = str(raw.get('cert') or '').strip()
    if not cert:
        raise DomainError('EVIDENCE', '晚到证据缺少证书号')
    if cert not in certs:
        raise DomainError('CERT', f'证书 {cert} 不在本批证据快照中')
    kind = str(raw.get('kind') or '').strip()
    if kind not in KIND_LABELS:
        raise DomainError('EVIDENCE', '证据类型应为 revocation 或 amendment')
    received = str(raw.get('received_on') or today or date.today().isoformat()).strip()
    day(received)
    item = {'cert': cert, 'kind': kind, 'received_on': received,
            'valid_from': None, 'valid_to': None, 'parent': None,
            'device': None, 'alias': None, 'value': None,
            'note': str(raw.get('note') or '').strip()[:200]}
    if kind == 'revocation':
        return item
    for field in ('valid_from', 'valid_to', 'parent', 'device', 'alias'):
        value = str(raw.get(field) or '').strip()
        item[field] = value or None
    if 'value' in raw and str(raw['value']).strip():
        item['value'] = str(number(raw['value']))
    if not any(item[f] for f in ('valid_from', 'valid_to', 'parent', 'device',
                                 'alias', 'value')):
        raise DomainError('EVIDENCE', '改证须给出有效期、父证书、设备别名或量程变更')
    info = certs[cert]
    start = day(item['valid_from'] or info['valid_from'])
    end = day(item['valid_to'] or info['valid_to'])
    if start >= end:
        raise DomainError('RANGE', '改证后的有效期区间无效')
    return item


def money_text(value):
    return fmt(value, '0.01')


def status_for(reasons):
    codes = [c for c in reasons if c and c != 'ACCEPT']
    if not codes:
        return 'PAY'
    if any(c in REJECT_CODES for c in codes):
        return 'REJECT'
    if all(c in HOLD_CODES for c in codes):
        return 'HOLD'
    return 'REJECT'


def base_records(run, start=None, end=None):
    """把测量裁决固化成结算底稿，只取结算期内的测量；期外测量单独留存复核。"""
    records, excluded = [], []
    for verdict in run.get('verdicts', []):
        record = {
            'cert': verdict['证书'], 'device': verdict.get('设备', ''),
            'measured_at': verdict.get('测量日', ''), 'value': verdict.get('测量值_V', ''),
            'source_line': verdict.get('行号'),
            'reasons': [x for x in str(verdict.get('原因码') or '').split(',')
                        if x and x != 'ACCEPT'],
            'late': [],
        }
        record['status'] = status_for(record['reasons'])
        if start and end and record['measured_at']:
            measured = day(record['measured_at'])
            if not start <= measured < end:
                excluded.append(record)
                continue
        records.append(record)
    return records, excluded


def cert_index(run):
    """证书簿：含规范设备名与别名组，改证换设备时按新归属重算。"""
    certs = {}
    for key, cert in (run.get('certs') or {}).items():
        certs[key] = {
            'parent': cert.get('parent') or '',
            'valid_from': cert.get('valid_from'), 'valid_to': cert.get('valid_to'),
            'device': cert.get('device') or '', 'alias': cert.get('alias') or '',
            'canonical': cert.get('canonical') or cert.get('device') or '',
            'min_v': cert.get('min_v'), 'max_v': cert.get('max_v'),
            'revoked': cert.get('revoked') or 'no',
            'conflicted': bool(cert.get('conflicted')),
        }
    aliases = {}
    for record in run.get('measurements') or []:
        canonical = record.get('canonical') or record.get('device')
        aliases.setdefault(canonical, set()).update(
            {record.get('device'), record.get('alias')} - {None, ''})
    for cert in certs.values():
        names = aliases.get(cert['canonical']) or set()
        names.add(cert['canonical'])
        names.discard('')
        cert['aliases'] = sorted(names)
    return certs


def derive(record, certs):
    """按当前证书簿重排一条测量的原因码。"""
    info = certs.get(record['cert'])
    if not info:
        return ['MISSING_REFERENCE']
    start, end = day(info['valid_from']), day(info['valid_to'])
    try:
        measured = day(record['measured_at'])
    except DomainError:
        return ['INVALID_WINDOW']
    overlaps = [key for key, cert in certs.items()
                if key != record['cert'] and cert['canonical'] == info['canonical']
                and max(day(cert['valid_from']), start) < min(day(cert['valid_to']), end)]
    reasons = []
    if not start <= measured < end or overlaps or info.get('conflicted'):
        reasons.append('INVALID_WINDOW')
    try:
        if not number(info['min_v']) <= number(record['value']) <= number(info['max_v']):
            reasons.append('OUT_OF_RANGE')
    except (DomainError, TypeError):
        reasons.append('OUT_OF_RANGE')
    outcome = trace(record['cert'], certs)
    code = outcome['code']
    conflict = any(certs.get(node, {}).get('conflicted') for node in outcome['path'])
    if conflict:
        code = 'REFERENCE_CONFLICT'
    if not outcome['valid'] or conflict:
        reasons.append(code)
    return reasons


def _amend(certs, item):
    cert = certs[item['cert']]
    if item['kind'] == 'revocation':
        cert['revoked'] = 'yes'
        return
    for field in ('valid_from', 'valid_to'):
        if item.get(field):
            cert[field] = item[field]
    if item.get('parent') is not None:
        cert['parent'] = item['parent']
    if item.get('device'):
        cert['device'] = item['device']
    if item.get('alias'):
        cert['alias'] = item['alias']
    if item.get('value'):
        cert['min_v'] = item['value']


def recanon(certs):
    """改证换设备/别名后重算归属：同组证书共用规范名，别名合并可跨证书。"""
    rows = [{'device': cert['device'], 'alias': cert['alias']}
            for cert in certs.values()]
    names = equivalence(rows)
    groups = {}
    for key, cert in certs.items():
        canonical = names.get(cert['device'], cert['device'])
        cert['canonical'] = canonical
        groups.setdefault(canonical, set()).update(
            {cert['device'], cert['alias']} - {None, ''})
    for cert in certs.values():
        cert['aliases'] = sorted(groups[cert['canonical']])


def paid_certs(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record['cert'], []).append(record)
    return {cert for cert, rows in grouped.items()
            if all(record['status'] == 'PAY' for record in rows)}


def apply_evidence(records, certs, evidence, unit_price):
    """按收到顺序改写证书簿并重算全部测量，返回每份证据的金额与明细变化。

    certs 会被就地改写；records 的 reasons/status/late 会就地更新。金额按
    “应付证书数”变化计，同一张证书只计一次，重复证据不会放大影响。
    """
    impacts = []
    for item in sorted(evidence, key=lambda e: (e['received_on'],) + evidence_key(e)):
        before = paid_certs(records)
        previous = {id(record): record['status'] for record in records}
        _amend(certs, item)
        recanon(certs)
        touched = {item['cert']} | set(descendants(item['cert'], certs))
        for record in records:
            reasons = derive(record, certs)
            if reasons:
                record['late'] = [item['kind']] if record['cert'] in touched else record['late']
            record['reasons'] = reasons
            record['status'] = status_for(reasons)
        after = paid_certs(records)
        changes = [{'cert': record['cert'], 'measured_at': record['measured_at'],
                    'from': previous[id(record)], 'to': record['status']}
                   for record in records if previous[id(record)] != record['status']]
        impacts.append({
            'key': evidence_key(item), 'cert': item['cert'], 'kind': item['kind'],
            'received_on': item['received_on'],
            'amount': money_text((len(after) - len(before)) * unit_price),
            'affected': len(changes), 'scope': sorted(touched), 'changes': changes,
        })
    return impacts


def group_lines(records, certs, unit_price):
    buckets = {}
    for record in records:
        buckets.setdefault(record['cert'], []).append(record)
    lines = []
    for cert in sorted(buckets):
        rows = buckets[cert]
        info = certs.get(cert) or {}
        reasons = Counter()
        for row in rows:
            for code in row['reasons']:
                reasons[code] += 1
        pay = all(row['status'] == 'PAY' for row in rows)
        hold = not pay and all(row['status'] != 'REJECT' for row in rows)
        status = 'PAY' if pay else 'HOLD' if hold else 'REJECT'
        lines.append({
            'cert': cert,
            'device': info.get('device') or '',
            'alias': info.get('alias') or '',
            'canonical': info.get('canonical') or '',
            'aliases': ','.join(info.get('aliases') or []),
            'status': status, 'status_label': STATUS_LABELS[status],
            'count': len(rows),
            'source_lines': ','.join(str(x) for x in sorted(r['source_line'] for r in rows)),
            'measured_from': min(r['measured_at'] for r in rows),
            'measured_to': max(r['measured_at'] for r in rows),
            'reasons': '；'.join(f'{code}×{n}' for code, n in sorted(reasons.items())),
            'payable': money_text(unit_price if status == 'PAY' else 0),
            'hold': money_text(unit_price if status == 'HOLD' else 0),
            'rejected': money_text(unit_price if status == 'REJECT' else 0),
        })
    return lines


def totals_for(lines, adjustments, currency):
    payable = sum((Decimal(line['payable']) for line in lines), Decimal('0'))
    hold = sum((Decimal(line['hold']) for line in lines), Decimal('0'))
    rejected = sum((Decimal(line['rejected']) for line in lines), Decimal('0'))
    adjustment = sum((Decimal(item['amount']) for item in adjustments), Decimal('0'))
    return {
        'cert_count': len(lines),
        'measurement_count': sum(line['count'] for line in lines),
        'pay_count': sum(1 for line in lines if line['status'] == 'PAY'),
        'hold_count': sum(1 for line in lines if line['status'] == 'HOLD'),
        'reject_count': sum(1 for line in lines if line['status'] == 'REJECT'),
        'payable': money_text(payable), 'hold': money_text(hold),
        'rejected': money_text(rejected), 'adjustment': money_text(adjustment),
        'net': money_text(payable + adjustment), 'currency': currency,
    }


def compose(base, certs, evidence, adjustments, unit_price, currency, excluded=None):
    """由底稿 + 本批晚到证据 + 上批转来的调整，合成不可变结算快照。"""
    records = deepcopy(base)
    working = deepcopy(certs)
    impacts = apply_evidence(records, working, evidence, unit_price)
    lines = group_lines(records, working, unit_price)
    totals = totals_for(lines, adjustments, currency)
    snapshot = {
        'base': base, 'measurements': records, 'lines': lines, 'certs': working,
        'adjustments': adjustments, 'totals': totals,
        'excluded': excluded or [], 'rule_version': RULE_VERSION,
    }
    return snapshot, impacts
