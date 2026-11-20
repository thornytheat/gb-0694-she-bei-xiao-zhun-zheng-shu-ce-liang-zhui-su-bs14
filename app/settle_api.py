import csv, io

from flask import Blueprint, Response, jsonify, render_template, request

from app.config import MODULES, TITLE
from app.errors import DomainError
from app.settle_store import ensure, get, list_batches, post, record_evidence, review
from app.settlement import KIND_LABELS, next_period
from app.store import latest

bp = Blueprint('settle', __name__)
STATUS_LEDGER = {'PAY': '应付入账', 'WITHHOLD': '暂扣挂账', 'CARRY': '上期调整'}


def _body():
    return request.get_json(silent=True) or {}


def _revision(body):
    try:
        return int(body.get('revision', 0))
    except (ValueError, TypeError):
        raise DomainError('VERSION', '版本必须为整数')


@bp.route('/settlement')
def page():
    ident = request.args.get('id')
    if ident:
        batch = get(ident)
    else:
        if latest() is None:
            raise DomainError('NOT_FOUND', '还没有测量批次，请先运行样例', 404)
        supplier = request.args.get('supplier', '').strip()
        if supplier:
            batch = ensure(supplier, request.args.get('period') or None)
        else:
            batches = list_batches()
            batch = get(batches[0]['id']) if batches else ensure('样例供应商', None)
    return render_template('settlement.html', title=TITLE, modules=MODULES,
                           run={'manifest': {'run_id': batch['base_run']}},
                           batch=batch, stage=0,
                           next_period=next_period(batch['period']))


@bp.route('/api/settlements')
def index():
    return jsonify(list_batches())


@bp.route('/api/settlements', methods=['POST'])
def create():
    body = _body()
    if not body.get('supplier'):
        raise DomainError('SUPPLIER', '必须指定供应商')
    return jsonify(ensure(body['supplier'], body.get('period'),
                          run_id=body.get('run_id'), unit_price=body.get('unit_price'),
                          currency=body.get('currency'), evidence=body.get('evidence')))


@bp.route('/api/settlements/<ident>')
def detail(ident):
    return jsonify(get(ident))


@bp.route('/api/settlements/<ident>/evidence', methods=['POST'])
def evidence(ident):
    body = _body()
    batch, reused = record_evidence(ident, body.get('evidence', body))
    batch['reused'] = reused
    return jsonify(batch)


@bp.route('/api/settlements/<ident>/review', methods=['POST'])
def decide(ident):
    body = _body()
    return jsonify(review(ident, _revision(body), body.get('decision')))


@bp.route('/api/settlements/<ident>/post', methods=['POST'])
def post_batch(ident):
    return jsonify(post(ident, _revision(_body())))


@bp.route('/api/settlements/<ident>/evidence.csv')
def evidence_csv(ident):
    batch = get(ident)
    rows = []
    for line in batch['snapshot']['lines']:
        rows.append({'记录类型': '结算明细', '证书': line['cert'], '设备': line['device'],
                     '别名': line['alias'], '状态': line['status_label'],
                     '测量条数': line['count'], '原始行号': line['source_lines'],
                     '应付': line['payable'], '暂扣': line['hold'], '拒付': line['rejected'],
                     '原因': line['reasons'], '去向': '本批'})
    for item in batch['evidence']:
        rows.append({'记录类型': '晚到证据', '证书': item['cert'],
                     '状态': KIND_LABELS.get(item['kind'], item['kind']),
                     '原因': item['note'] or '—', '应付': item['amount'],
                     '原始行号': item['received_on'], '去向': item['disposition'],
                     '设备': '、'.join(item.get('scope') or [])})
    for line in batch['carries']:
        rows.append({'记录类型': '上期转来', '证书': line['cert'],
                     '状态': KIND_LABELS.get(line['kind'], line['kind']),
                     '原因': line['reason'], '应付': line['amount'],
                     '去向': '本批调整', '原始行号': line['source_batch']})
    for entry in batch['ledger']:
        rows.append({'记录类型': '付款凭证',
                     '状态': STATUS_LEDGER.get(entry['kind'], entry['kind']),
                     '应付': entry['amount'], '去向': entry['id'],
                     '原因': entry['ref'], '原始行号': entry['created_at']})
    fields = ['结算批', '供应商', '结算期', '结算批状态', '币种', '记录类型', '证书',
              '设备', '别名', '状态', '测量条数', '原始行号', '应付', '暂扣', '拒付',
              '原因', '去向']
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({'结算批': batch['id'], '供应商': batch['supplier'],
                         '结算期': batch['period'], '结算批状态': batch['status'],
                         '币种': batch['currency'], **row})
    return Response('﻿' + out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=settlement.csv'})
