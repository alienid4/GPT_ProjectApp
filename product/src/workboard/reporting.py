import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from flask import Blueprint, g, jsonify, request, send_file
from sqlalchemy import select

from .auth import authenticated
from .errors import require
from .sorting import ordered
from .models import Allocation, Capacity, Event, Person, Task, utcnow
from .policy import can_edit, global_capability, readable_task, task_read_clause, work_eligible
from .services import audit, day, expected, fields, person, task_payload, text

reports = Blueprint('reports', __name__, url_prefix='/api')
LABELS = {'todo': '待處理', 'doing': '進行中', 'waiting': '等待', 'review': '待確認', 'done': '已完成'}


def week_start(value=None):
    today = day(value) or (utcnow() + timedelta(hours=8)).date()
    return today - timedelta(days=today.weekday())


def hours(value, optional=True):
    if value is None and optional:
        return None
    require(type(value) in {int, float} and 0 <= value <= 168, '時數須介於 0 與 168。')
    return Decimal(str(value)).quantize(Decimal('0.01'))


@reports.get('/weekly')
@authenticated
def weekly():
    start = week_start(request.args.get('week'))
    since = datetime.combine(start, time.min) - timedelta(hours=8)
    until = since + timedelta(days=7)
    tasks = list(g.db.scalars(select(Task).where(task_read_clause(g.user))))
    ids = [t.id for t in tasks]
    events = list(g.db.scalars(select(Event).where(Event.resource_type == 'task', Event.resource_id.in_(ids),
                                                  Event.visibility == 'read', Event.created_at >= since, Event.created_at < until).order_by(Event.sequence)))
    activity = {}
    for event in events:
        activity.setdefault(event.resource_id, []).append({'action': event.action, 'actor_id': event.actor_id,
                                                          'at': event.created_at.isoformat() + 'Z', 'reason': event.reason})
    return jsonify({'week': start.isoformat(), 'scope': '目前授權範圍', 'as_of': utcnow().isoformat() + 'Z',
                    'items': [{'task': task_payload(g.db, g.user, t), 'activities': activity.get(t.id, []),
                               'ongoing_without_activity': t.id not in activity}
                              for t in tasks if t.id in activity or (t.status != 'done' and not t.cancelled)]})


@reports.get('/workload')
@authenticated
def workload():
    require(global_capability(g.user, 'department_view'), '無權檢視部門負荷。', 403)
    week = week_start(request.args.get('week'))
    tasks = list(g.db.scalars(select(Task).where(task_read_clause(g.user), Task.cancelled.is_(False), Task.status != 'done')))
    members = [p for p in g.db.scalars(select(Person).where(Person.department == g.user.department)) if work_eligible(p)]
    items = []
    for member in members:
        related = [t for t in tasks if t.assignment != 'proposed' and can_edit(g.db, member, t)]
        capacity = g.db.scalar(select(Capacity).where(Capacity.person_id == member.id, Capacity.week == week))
        values = list(g.db.scalars(select(Allocation).where(Allocation.task_id.in_([t.id for t in related]), Allocation.person_id == member.id, Allocation.week == week)))
        recorded = {a.task_id: a for a in values}
        unknown = [t for t in related if t.id not in recorded or recorded[t.id].remaining is None]
        known = sum(float(a.remaining) for a in values if a.remaining is not None)
        cap = float(capacity.hours) if capacity and capacity.hours is not None else None
        items.append({'person_id': member.id, 'name': member.name, 'capacity': cap,
                      'known_remaining': known, 'unknown_tasks': len(unknown),
                      'ratio': round(known / cap, 3) if not unknown and cap is not None and cap > 0 else None,
                      'capacity_revision': capacity.revision if capacity else 0,
                      'visible_tasks': [task_payload(g.db, g.user, t) for t in related],
                      'waiting': sum(t.status == 'waiting' for t in related),
                      'pending_confirmation': sum(t.status == 'review' for t in related),
                      'unacknowledged': sum(t.assignment == 'assigned' and not t.acknowledged for t in related),
                      'proposals': sum(t.assignment == 'proposed' and t.proposed_owner_id == member.id for t in tasks)})
    # Unknown workload is listed first, never treated as a zero or ranked last.
    items.sort(key=lambda i: (i['ratio'] is not None, -(i['ratio'] or 0), i['name']))
    return jsonify({'week': week.isoformat(), 'scope': '目前授權範圍', 'as_of': utcnow().isoformat() + 'Z', 'items': items})


@reports.put('/capacity/<person_id>')
@authenticated
def capacity(person_id):
    require(global_capability(g.user, 'capacity'), '無權調整容量。', 403)
    target = person(g.db, g.user, person_id)
    data = request.get_json(silent=True)
    fields(data, {'revision', 'week', 'hours', 'reason'})
    reason = text(data, 'reason', required=True)
    week = week_start(data.get('week'))
    row = g.db.scalar(select(Capacity).where(Capacity.person_id == target.id, Capacity.week == week))
    before = float(row.hours) if row and row.hours is not None else None
    if row:
        expected(row, data)
    else:
        require(data.get('revision') == 0, '容量已變更，請重新載入。', 409)
        row = Capacity(person_id=target.id, week=week)
        g.db.add(row)
    row.hours = hours(data.get('hours'), False)
    audit(g.db, g.user, target, 'capacity', reason, {'hours': before}, {'hours': str(row.hours), 'week': week}, visibility='admin')
    g.db.flush()
    return jsonify({'ok': True, 'revision': row.revision})


@reports.get('/tasks/<task_id>/allocations')
@authenticated
def allocations(task_id):
    task = readable_task(g.db, g.user, task_id)
    return jsonify({'items': [{'id': a.id, 'person_id': a.person_id, 'week': a.week.isoformat(),
                               'hours': float(a.hours) if a.hours is not None else None,
                               'remaining': float(a.remaining) if a.remaining is not None else None, 'revision': a.revision}
                              for a in g.db.scalars(select(Allocation).where(Allocation.task_id == task.id).order_by(Allocation.week))]})


@reports.put('/tasks/<task_id>/allocations')
@authenticated
def allocation(task_id):
    task = readable_task(g.db, g.user, task_id)
    require(not task.cleaned, '工作內容已到期清理。', 409)
    require(can_edit(g.db, g.user, task), '無權記錄此工作投入。', 403)
    data = request.get_json(silent=True)
    fields(data, {'revision', 'week', 'hours', 'remaining', 'reason'})
    week = week_start(data.get('week'))
    row = g.db.scalar(select(Allocation).where(Allocation.task_id == task.id, Allocation.person_id == g.user.id, Allocation.week == week))
    if row:
        expected(row, data)
    else:
        require(data.get('revision') == 0, '投入紀錄已變更。', 409)
        row = Allocation(task_id=task.id, person_id=g.user.id, week=week)
        g.db.add(row)
    before = {'hours': row.hours, 'remaining': row.remaining}
    row.hours, row.remaining = hours(data.get('hours')), hours(data.get('remaining'))
    audit(g.db, g.user, task, 'allocation', text(data, 'reason'), before,
          {'hours': row.hours, 'remaining': row.remaining, 'person_id': g.user.id, 'week': week})
    g.db.flush()
    return jsonify({'ok': True, 'revision': row.revision})


def safe_cell(value):
    if isinstance(value, (int, float)):
        return value
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


@reports.get('/exports/tasks.<format>')
@authenticated
def export_tasks(format):
    require(format in {'csv', 'xlsx', 'md'}, '不支援此匯出格式。', 404)
    tasks = list(g.db.scalars(select(Task).where(task_read_clause(g.user)).order_by(Task.code)))
    view = request.args.get('view', 'all')
    mode = request.args.get('execution', 'active')
    if mode == 'active':
        tasks = [t for t in tasks if not t.cancelled]
    elif mode == 'cancelled':
        tasks = [t for t in tasks if t.cancelled]
    elif mode == 'paused':
        tasks = [t for t in tasks if t.paused and not t.cancelled]
    if view == 'mine':
        from .policy import current_relation
        tasks = [t for t in tasks if g.user.id in {t.owner_id, t.proposed_owner_id} or current_relation(g.db, g.user, t, ['collaborator'])]
    elif view in {'adhoc', 'self'}:
        tasks = [t for t in tasks if t.source == view]
    elif view == 'reviews':
        from .policy import can_manage
        tasks = [t for t in tasks if t.status == 'review' and can_manage(g.db, g.user, t, 'confirm')]
    records = [task_payload(g.db, g.user, task) for task in tasks]
    query = request.args.get('q', '').lower().strip()[:200]
    if query:
        records = [r for r in records if any(query in str(r.get(k) or '').lower()
                   for k in ['title', 'code', 'owner_name', 'phase', 'result'])]
    sort = request.args.get('sort', 'priority')
    require(sort in {'priority', 'title', 'code', 'target_on', 'status'}, '排序欄位不符。')
    records = ordered(records, sort, request.args.get('direction') == 'desc')
    rows = [['工作編號', '名稱', '來源', '狀態', '主責', '階段', '計畫開始', '計畫結束', '目標日期', '下一步', '成果']]
    for task in records:
        rows.append([task['code'], task['title'], {"project":"專案","self":"自主","adhoc":"臨時","assigned":"交辦"}[task['source']], '已取消' if task['cancelled'] else LABELS[task['status']],
                     task['owner_name'], task['phase'], task['planned_start'], task['planned_end'],
                     task['target_on'], task['next_action'], task['result']])
    return export_rows(rows, format, '工作清單')


def export_rows(rows, format, title):
    require(format in {'csv', 'xlsx', 'md'}, '不支援此匯出格式。', 404)
    stamp = utcnow().strftime('%Y%m%dT%H%M%SZ')
    if format == 'xlsx':
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = title[:31]
        for row in rows:
            sheet.append([safe_cell(v) for v in row])
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        stream = io.BytesIO()
        book.save(stream)
        mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif format == 'csv':
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerows([[safe_cell(v) for v in row] for row in rows])
        stream, mime = io.BytesIO(out.getvalue().encode('utf-8-sig')), 'text/csv'
    else:
        def escaped(value):
            return ('' if value is None else str(value)).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')
        lines = ['# ' + title, '', '範圍：目前授權資料', '匯出時間（UTC）：' + stamp, '']
        for index, row in enumerate(rows):
            lines.append('| ' + ' | '.join(escaped(v) for v in row) + ' |')
            if index == 0:
                lines.append('|' + '|'.join(['---'] * len(row)) + '|')
        stream, mime = io.BytesIO(('\n'.join(lines) + '\n').encode()), 'text/markdown'
    stream.seek(0)
    return send_file(stream, mimetype=mime, as_attachment=True, download_name='workboard-' + stamp + '.' + format, etag=False, conditional=False)


# Display labels are presentation data, never authorization rules.
CLEANUP_LABELS = {'task':'工作','project':'專案','comment':'留言','document':'附件','version':'附件版本',
 'submission':'成果提交','confirmation':'確認紀錄','project_submission':'專案提交','project_confirmation':'專案確認',
 'event':'操作歷程','allocation':'投入紀錄','coordination':'協調','service_term':'服務條件','service_log':'維運紀錄',
 'template':'範本','security_event':'存取紀錄','cleanup_history':'清理紀錄','capacity':'可安排時數','cleanup_state':'清理處理原因','grant':'操作授權紀錄'}
CLEANUP_STATUSES = {'pending':'待處理','deferred':'已暫緩','done':'已清理','pending_file':'待移除檔案',
                    'failed_file':'檔案移除失敗','failed':'處理失敗'}


def visible_projects():
    from .models import Project
    from .projects import payload
    from .policy import can_read_project
    return [payload(g.db, g.user, p) for p in g.db.scalars(select(Project).where(
        Project.department == g.user.department)) if can_read_project(g.db, g.user, p)]


def export_wbs(dataset, format, query, sort, descending):
    if dataset == 'department':
        require(global_capability(g.user, 'department_view'), '無權檢視部門分解。', 403)
    projects = [p for p in visible_projects() if not p['cancelled']]
    tasks = [task_payload(g.db, g.user, t) for t in g.db.scalars(select(Task).where(
        task_read_clause(g.user), Task.cancelled.is_(False)))]
    if dataset == 'wbs':
        tasks = [t for t in tasks if t['project_id']]
    rows = [['層級', '類型', '編號', '名稱', '狀態', '上層編號', '計畫開始', '計畫結束']]
    nodes = {n['id']: n for n in [*projects, *tasks]}
    seen = set()
    def append(node, kind, depth, parent):
        require(depth <= 64 and node['id'] not in seen, '工作階層異常。', 409)
        seen.add(node['id'])
        label = node['name'] if kind == '專案' else node['title']
        if not query or query in (label + node['code']).lower():
            rows.append([depth, kind, node['code'], label, LABELS[node['status']],
                         nodes[parent]['code'] if parent in nodes else '', node['planned_start'], node['planned_end']])
    def task_rows(parent, project_id, depth):
        for node in ordered([t for t in tasks if t['project_id'] == project_id and t['parent_id'] == parent], sort, descending):
            if node['id'] in seen:
                continue
            append(node, '工作', depth, parent or project_id)
            task_rows(node['id'], project_id, depth + 1)
    def project_rows(parent, depth):
        for node in ordered([p for p in projects if p['parent_id'] == parent], sort, descending, 'name'):
            append(node, '專案', depth, parent)
            project_rows(node['id'], depth + 1)
            task_rows(None, node['id'], depth + 1)
    project_rows(None, 0)
    for node in ordered(tasks, sort, descending):
        if node['id'] not in seen:
            append(node, '工作', 0, node['parent_id'] or node['project_id'])
            task_rows(node['id'], node['project_id'], 1)
    require(len(seen) == len(nodes), '工作階層異常。', 409)
    return export_rows(rows, format, '部門分解' if dataset == 'department' else '專案分解')


@reports.get('/exports/<dataset>.<format>')
@authenticated
def export_dataset(dataset, format):
    require(format in {'csv', 'xlsx', 'md'}, '不支援此匯出格式。', 404)
    from .api import admin_people
    from .governance import templates, terms, coordinations
    from .retention import candidates
    query = request.args.get('q', '').lower().strip()[:200]
    sort, descending = request.args.get('sort', ''), request.args.get('direction') == 'desc'
    if dataset in {'wbs', 'department'}:
        require(sort in {'', 'priority', 'title', 'code', 'target_on', 'status'}, '排序欄位不符。')
        return export_wbs(dataset, format, query, sort or 'priority', descending)
    # Keep raw records for matching and ordering; translate only at serialization.
    if dataset == 'projects':
        records, title, header, fallback = visible_projects(), '專案清單', ['專案編號','名稱','狀態','計畫開始','計畫結束'], 'name'
        keys, search = {'name','code','status'}, lambda r: r['name'] + r['code']
        row = lambda r: [r['code'], r['name'], '已取消' if r['cancelled'] else LABELS[r['status']], r['planned_start'], r['planned_end']]
    elif dataset == 'weekly':
        records = [{**r['task'], 'activities': r['activities']} for r in weekly().get_json()['items']]
        title, header, fallback = '工作週摘要', ['編號','工作','狀態','本週紀錄','下一步'], 'title'
        keys, search = {'title','code','status'}, lambda r: r['title'] + r['code']
        row = lambda r: [r['code'], r['title'], LABELS[r['status']], len(r['activities']), r['next_action']]
    elif dataset == 'load':
        records, title, header, fallback = workload().get_json()['items'], '部門工作負荷', ['人員','剩餘時數','未估算工作','每週容量','負荷比例','等待','待確認'], 'name'
        keys, search = {'name','known_remaining','ratio'}, lambda r: r['name']
        row = lambda r: [r['name'], r['known_remaining'], r['unknown_tasks'], r['capacity'], r['ratio'], r['waiting'], r['pending_confirmation']]
    elif dataset == 'accounts':
        records, title, header, fallback = admin_people().get_json()['items'], '帳號管理', ['帳號','姓名','使用資格','AD資格'], 'name'
        keys, search = {'name','username','active'}, lambda r: r['name'] + r['username']
        row = lambda r: [r['username'], r['name'], '啟用' if r['active'] else '停用', '啟用' if r['directory_active'] else '停用']
    elif dataset == 'templates':
        records, title, header, fallback = templates().get_json()['items'], '工作範本', ['名稱','版本','狀態','階段'], 'name'
        keys, search = {'name','version','status'}, lambda r: r['name']
        row = lambda r: [r['name'], r['version'], {'draft':'草稿','active':'使用中','retired':'已停用'}[r['status']], '、'.join(r['phases'])]
    elif dataset == 'terms':
        records, title, header, fallback = terms().get_json()['items'], '服務條件', ['名稱','服務範圍','開始日期','結束日期','合約參考','聯絡窗口'], 'name'
        keys, search = {'name','start_on','end_on'}, lambda r: r['name']
        row = lambda r: [r['name'], r['scope'], r['start_on'], r['end_on'], r['contract_reference'], r['contact']]
    elif dataset == 'coordinations':
        records, title, header, fallback = coordinations().get_json()['items'], '協調事項', ['事項','狀態','追蹤日','處理說明'], 'subject'
        labels = {'pending':'待受理','accepted':'處理中','completed':'已完成','withdrawn':'已撤回','forwarded':'已轉交','restore_review':'待複核','expired':'已清理'}
        keys, search = {'subject','status','follow_on'}, lambda r: r['subject']
        row = lambda r: [r['subject'], labels[r['status']], r['follow_on'], r['resolution']]
    elif dataset == 'cleanup':
        records, title, header, fallback = candidates().get_json()['items'], '到期清理', ['類型','識別','建立日','到期日','狀態','下次處理'], 'label'
        records = [{**r, 'label': CLEANUP_LABELS.get(r['resource_type'], r['resource_type']) + ' ' + r['resource_id']} for r in records]
        keys, search = {'resource_type','expires_at','status'}, lambda r: r['label']
        row = lambda r: [CLEANUP_LABELS.get(r['resource_type'], r['resource_type']), r['resource_id'], r['created_at'], r['expires_at'], CLEANUP_STATUSES.get(r['status'],r['status']), r['deferred_until']]
    elif dataset == 'files':
        from .models import DocumentVersion
        from .policy import can_read_version_record
        records, title, header, fallback = [], '附件清單', ['工作編號','檔名','版本','建立日期','到期日期','工作名稱'], 'filename'
        for task in g.db.scalars(select(Task).where(task_read_clause(g.user))):
            for version in g.db.scalars(select(DocumentVersion).where(DocumentVersion.task_id == task.id)):
                if can_read_version_record(g.db, g.user, task, version):
                    records.append({'id':version.id, 'filename':version.filename, 'number':version.number,
                                    'created_at':version.created_at.isoformat()+'Z', 'expires_at':version.expires_at.isoformat()+'Z',
                                    'task_code':task.code, 'task_title':task.title})
        keys, search = {'filename','number','created_at'}, lambda r: r['filename'] + r['task_title']
        row = lambda r: [r['task_code'], r['filename'], r['number'], r['created_at'], r['expires_at'], r['task_title']]
    else:
        require(False, '找不到匯出資料。', 404)
    require(not sort or sort in keys, '排序欄位不符。')
    records = [r for r in records if not query or query in search(r).lower()]
    records = ordered(records, sort or fallback, descending, fallback)
    return export_rows([header, *[row(r) for r in records]], format, title)
