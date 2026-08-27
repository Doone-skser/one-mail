#!/usr/bin/env python3
"""OneMail - 邮件取件小服务 (只读访问 MIAB Maildir)"""
import base64
import html as html_mod
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from flask import (Flask, abort, g, redirect, render_template, request,
                   Response, session, url_for)

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'onemail.db')
MAILBOX_ROOT = '/home/user-data/mail/mailboxes'
MIAB_API = 'https://127.0.0.1/admin/mail/users?format=json'
TZ = ZoneInfo('Asia/Shanghai')
PER_PAGE = 20
DEV_HOST = '127.0.0.1'   # 本地调试用;生产监听地址在 deploy/onemail.service 里
DEV_PORT = 8200

# 失效提示页(过期/撤销)上需要打码显示的邮箱集合,链接有效时始终显示完整地址。
# 用法:把要保护的完整邮箱地址加进集合即可,如 "masked@example.com"
# 会显示为 "ma**@example.com"(本地部分前 2 字符 + **);管理端永远显示完整地址。
MASKED_MAILBOXES = {"masked@example.com"}


def mask_email(addr):
    """失效提示页(过期/撤销)上打码显示邮箱:本地部分 ≤2 字符显示首字符+*,
    >2 字符显示前 2 字符+**;域名原样保留。不在集合内原样返回。
    注意:链接有效时的收件箱/详情页显示完整地址,打码只在失效页生效。"""
    if not addr or addr not in MASKED_MAILBOXES:
        return addr
    local, _, domain = addr.partition('@')
    if not local:
        return addr
    if len(local) <= 2:
        return '%s*@%s' % (local[0], domain)
    return '%s**@%s' % (local[:2], domain)

app = Flask(__name__)

_keyfile = os.path.join(BASE, 'secret_key')
if os.path.exists(_keyfile):
    with open(_keyfile, 'r') as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_keyfile, 'w') as f:
        f.write(app.secret_key)
    os.chmod(_keyfile, 0o600)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# 安全事件日志(fail2ban 读取):登录失败/锁定/无效 token
_sec_log = logging.getLogger('onemail.security')
_sec_log.setLevel(logging.INFO)
_sec_handler = logging.FileHandler(os.path.join(BASE, 'security.log'))
_sec_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s',
                                            '%Y-%m-%dT%H:%M:%S'))
_sec_log.addHandler(_sec_handler)
_sec_log.propagate = False

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ---------- 数据库 ----------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY,
            token TEXT UNIQUE,
            email TEXT,
            created_at REAL,
            active INTEGER
        );
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY,
            link_id INTEGER,
            ts REAL,
            ip TEXT,
            user_agent TEXT
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY,
            kind TEXT,
            key TEXT,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            email TEXT,
            pw TEXT,
            expires REAL
        );
    ''')
    # 迁移:为 links 增加 expires_at(NULL = 永久),已有行默认 NULL
    cols = [r[1] for r in db.execute('PRAGMA table_info(links)')]
    if 'expires_at' not in cols:
        db.execute('ALTER TABLE links ADD COLUMN expires_at REAL')
    db.commit()
    db.close()


init_db()


# ---------- MIAB API ----------

def miab_query(email, password):
    """返回 (status, data)。200 表示合法管理员。"""
    req = urllib.request.Request(MIAB_API)
    cred = base64.b64encode(('%s:%s' % (email, password)).encode()).decode()
    req.add_header('Authorization', 'Basic ' + cred)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


_users_cache = {'ts': 0, 'data': None}
_users_lock = threading.Lock()


def miab_users(email, password):
    """拉取邮箱列表,缓存 60 秒。"""
    with _users_lock:
        if _users_cache['data'] is not None and time.time() - _users_cache['ts'] < 60:
            return _users_cache['data']
    status, data = miab_query(email, password)
    if status != 200:
        return None
    boxes = []
    for domain in data:
        for u in domain.get('users', []):
            boxes.append(u.get('email', ''))
    boxes.sort()
    with _users_lock:
        _users_cache['ts'] = time.time()
        _users_cache['data'] = boxes
    return boxes


# ---------- Maildir 只读解析 ----------

def maildir_path(email_addr):
    local, _, domain = email_addr.partition('@')
    if not local or not domain:
        return None
    base = os.path.normpath(os.path.join(MAILBOX_ROOT, domain, local))
    if not base.startswith(os.path.normpath(MAILBOX_ROOT) + os.sep):
        return None
    if os.path.isdir(os.path.join(base, 'cur')):
        return base
    if os.path.isdir(os.path.join(base, 'Maildir', 'cur')):
        return os.path.join(base, 'Maildir')
    return None


def list_messages(maildir):
    """返回 [(key, mtime)],按 mtime 倒序(最新在前)。"""
    entries = []
    for sub in ('cur', 'new'):
        d = os.path.join(maildir, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name.startswith('.'):
                continue
            try:
                mt = os.path.getmtime(os.path.join(d, name))
            except OSError:
                continue
            entries.append((name, mt))
    entries.sort(key=lambda e: e[1], reverse=True)
    return entries


def open_message(maildir, key):
    """按 key(文件名)打开邮件,只允许 cur/new 下的普通文件。"""
    if not key or '/' in key or '\\' in key or key.startswith('.'):
        return None
    for sub in ('cur', 'new'):
        p = os.path.join(maildir, sub, key)
        if os.path.isfile(p):
            with open(p, 'rb') as f:
                return BytesParser(policy=policy.default).parse(f)
    return None


def hdr(msg, name):
    """解码邮件头(RFC2047 / GBK / UTF-8 容错)。"""
    v = msg.get(name)
    if v is None:
        return ''
    try:
        s = str(make_header(decode_header(str(v))))
    except Exception:
        s = str(v)
    return s.encode('utf-8', 'replace').decode('utf-8')


def msg_date(msg, fallback_ts=None):
    try:
        dt = parsedate_to_datetime(str(msg.get('Date', '')))
        if dt is None:
            raise ValueError
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo('UTC'))
        return dt.astimezone(TZ)
    except Exception:
        if fallback_ts:
            return datetime.fromtimestamp(fallback_ts, TZ)
        return datetime.now(TZ)


def decode_payload(part):
    data = part.get_payload(decode=True) or b''
    cs = part.get_content_charset()
    tried = []
    for enc in (cs, 'utf-8', 'gbk'):
        if not enc or enc.lower() in tried:
            continue
        tried.append(enc.lower())
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', 'replace')


def extract_parts(msg):
    """返回 (text_body, html_body, attachments)。
    attachments: [(index, filename, size)] — index 为附件在列表中的序号。"""
    text_body = None
    html_body = None
    atts = []

    def is_att(part):
        disp = part.get_content_disposition()
        return disp == 'attachment' or (disp is None and part.get_filename())

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if is_att(part):
                atts.append(part)
            elif ctype == 'text/plain' and text_body is None:
                text_body = decode_payload(part)
            elif ctype == 'text/html' and html_body is None:
                html_body = decode_payload(part)
    else:
        ctype = msg.get_content_type()
        if ctype == 'text/html':
            html_body = decode_payload(msg)
        else:
            text_body = decode_payload(msg)

    att_info = []
    for i, p in enumerate(atts):
        fn = p.get_filename() or '附件%d' % (i + 1)
        try:
            fn = str(make_header(decode_header(fn)))
        except Exception:
            pass
        size = len(p.get_payload(decode=True) or b'')
        att_info.append((i, fn, size))
    return text_body, html_body, att_info


# ---------- HTML 邮件远端资源剥离 ----------

# 1px 透明 GIF,剥掉远端图片后占位用(保留布局尺寸)
_PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA='
# src/srcset/background 属性里的远端 URL
_REMOTE_ATTR_RE = re.compile(
    r'(\s(?:src|srcset|background)\s*=\s*)("|\')(https?://.*?)\2', re.I | re.S)
# style 属性里的 url(http...)
_REMOTE_CSS_URL_RE = re.compile(r'url\(\s*(["\']?)https?://.*?\1\s*\)', re.I)
# 占位图样式,追加在正文末尾让被剥的图有个浅灰底
_PLACEHOLDER_STYLE = '<style>img[data-remote]{background-color:#f3f4f6}</style>'


def strip_remote_resources(html_text):
    """剥掉 HTML 正文里的远端资源:img/source 的 src、srcset、background 属性
    和 style 里的 url(http...) 全部替换为 1px 透明占位图(图片打上
    data-remote 标记,末尾追加浅灰底样式),返回处理后的 HTML。"""
    if not html_text:
        return html_text

    def attr_sub(m):
        prefix, q, _url = m.groups()
        if 'srcset' in prefix.lower():
            return prefix + q + q  # srcset 直接清空
        return '%s%s%s%s data-remote="1"' % (prefix, q, _PLACEHOLDER, q)

    s = _REMOTE_ATTR_RE.sub(attr_sub, html_text)
    s = _REMOTE_CSS_URL_RE.sub('url("%s")' % _PLACEHOLDER, s)
    return s + _PLACEHOLDER_STYLE


# ---------- 验证码提取 ----------

# 中文关键词 + 英文关键词(英文加词边界,避免命中 decode/spin 等)
# 只认关键词邻近命中,不做无关键词的数字兜底(兜底会误抓地址编号/型号数字等)
_CODE_KW = (r'(?:验证码|校验码|动态码|动态密码|安全码|代码|登录代码|口令'
            r'|verification\s*code|security\s*code|confirmation\s*code'
            r'|authentication\s*code|one[-\s]*time\s*(?:code|password)'
            r'|passcode|\botp\b|\bcode\b|\bpin\b)')
_CODE_KW_RE = re.compile(_CODE_KW, re.I)
# 关键词在前,数字在后(允许冒号/空格/is/为/是 等连接,间隙不超过 20 个非数字字符;
# (?!\d) 保证不会从 9 位以上的长数字段里截一段出来)
_KW_BEFORE_RE = re.compile(_CODE_KW + r'[^\d]{0,20}?(\d{4,8})(?!\d)', re.I)
# 数字在前,关键词在后(前置边界同样防长数字段)
_KW_AFTER_RE = re.compile(r'(?<![\dA-Za-z])(\d{4,8})[^\d]{0,20}?' + _CODE_KW, re.I)
_TAG_RE = re.compile(r'<[^>]+>')
_STYLE_SCRIPT_RE = re.compile(r'<(style|script|head)[^>]*>.*?</\1>', re.I | re.S)
_HEX_COLOR_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b')


def strip_html(html_text):
    """剥掉 HTML 标签,得到纯文本(用于从 HTML 邮件中提取验证码)。
    先剔除 style/script/head 块,再剥标签,最后去掉 #000000 之类的颜色值,
    避免 CSS 里的十六进制颜色被误识别为验证码。"""
    if not html_text:
        return ''
    s = _STYLE_SCRIPT_RE.sub(' ', html_text)
    s = _TAG_RE.sub(' ', s)
    s = html_mod.unescape(s)
    s = _HEX_COLOR_RE.sub(' ', s)
    return s


def _looks_like_year(d):
    return len(d) == 4 and d[:2] in ('19', '20')


def _kw_search(src):
    """关键词邻近匹配,跳过年份(19xx/20xx,如 copyright 年份)。"""
    for rx in (_KW_BEFORE_RE, _KW_AFTER_RE):
        for m in rx.finditer(src):
            if not _looks_like_year(m.group(1)):
                return m.group(1)
    return None


_WS_RE = re.compile(r'\s+')


def extract_code(subject, text):
    """从主题+纯文本正文中提取验证码,提取不到返回 None。
    只认关键词邻近的 4~8 位数字(主题先于正文);关键词附近是年份的不算。
    匹配前先把连续空白压成单空格——HTML 邮件剥标签后关键词和数字之间
    常隔着大量换行/缩进,不压缩会漏真验证码。"""
    subject = _WS_RE.sub(' ', subject or '')
    text = _WS_RE.sub(' ', text or '')
    for src in (subject, text):
        hit = _kw_search(src)
        if hit:
            return hit
    return None


def msg_plain_text(msg, text_body, html_body):
    """用于验证码提取的纯文本:优先 text/plain,否则剥 HTML。"""
    if text_body:
        return text_body
    return strip_html(html_body)


# ---------- 工具 ----------

def client_ip():
    return request.headers.get('X-Real-IP') or request.remote_addr or ''


def fmt_size(n):
    if n < 1024:
        return '%d B' % n
    if n < 1024 * 1024:
        return '%.1f KB' % (n / 1024)
    return '%.1f MB' % (n / 1024 / 1024)


app.jinja_env.filters['fmt_size'] = fmt_size


def get_link(token):
    return get_db().execute(
        'SELECT * FROM links WHERE token = ?', (token,)).fetchone()


# ---------- 链接有效期 ----------

TTL_CHOICES = {'1': 1, '7': 7, '30': 30, '0': None}  # 天数; None = 永久


def calc_expires(ttl):
    """ttl 表单值 -> expires_at 时间戳(None = 永久)。非法值抛 ValueError。"""
    if ttl not in TTL_CHOICES:
        raise ValueError('bad ttl')
    days = TTL_CHOICES[ttl]
    return None if days is None else time.time() + days * 86400


def link_expired(link, now=None):
    exp = link['expires_at']
    return exp is not None and exp < (now if now is not None else time.time())


def remaining_str(link, now):
    exp = link['expires_at']
    if exp is None:
        return '永久'
    if exp < now:
        return '已过期'
    delta = int(exp - now)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return '剩 %d 天 %d 小时' % (days, hours)
    if hours:
        return '剩 %d 小时 %d 分' % (hours, minutes)
    return '剩 %d 分钟' % max(minutes, 1)


def link_view(row, host, now, visits=None, last_visit=None):
    """把 links 行加工成模板用的 dict(状态徽章/剩余时间/URL 等)。"""
    d = dict(row)
    d['url'] = 'https://%s/m/%s' % (host, row['token'])
    d['created_str'] = datetime.fromtimestamp(
        row['created_at'], TZ).strftime('%Y-%m-%d %H:%M')
    d['visits'] = visits if visits is not None else 0
    d['last_visit_str'] = (datetime.fromtimestamp(last_visit, TZ).strftime('%Y-%m-%d %H:%M')
                           if last_visit else '—')
    d['expired'] = link_expired(row, now)
    if not row['active']:
        d['status'], d['status_cls'] = '已撤销', 'badge-off'
    elif d['expired']:
        d['status'], d['status_cls'] = '已过期', 'badge-expired'
    else:
        d['status'], d['status_cls'] = '有效', 'badge-on'
    d['remaining'] = remaining_str(row, now)
    return d


def admin_required():
    if not session.get('admin') or get_admin_creds() is None:
        return redirect(url_for('admin_login'))
    return None


# ---------- 安全:限速 / 会话存储 / CSRF / 响应头 ----------

LOGIN_FAIL_WINDOW = 600    # 10 分钟内
LOGIN_FAIL_MAX = 5         # 失败 5 次
LOGIN_LOCK_SEC = 900       # 锁 15 分钟
TOKEN404_WINDOW = 60       # 1 分钟内
TOKEN404_MAX = 20          # 无效 token 404 超过 20 次 → 429


def _prune_attempts(db):
    db.execute('DELETE FROM attempts WHERE ts < ?', (time.time() - 3600,))


def record_attempt(kind, key):
    db = get_db()
    _prune_attempts(db)
    db.execute('INSERT INTO attempts (kind, key, ts) VALUES (?,?,?)',
               (kind, key, time.time()))
    db.commit()


def attempt_count(kind, key, window):
    row = get_db().execute(
        'SELECT COUNT(*) FROM attempts WHERE kind = ? AND key = ? AND ts > ?',
        (kind, key, time.time() - window)).fetchone()
    return row[0]


def login_locked(key):
    """返回 True 表示该 IP+账号 处于锁定期。"""
    db = get_db()
    _prune_attempts(db)
    row = db.execute(
        "SELECT COUNT(*) FROM attempts WHERE kind = 'loginlock' AND key = ?"
        ' AND ts > ?', (key, time.time() - LOGIN_LOCK_SEC)).fetchone()
    if row[0]:
        return True
    if attempt_count('loginfail', key, LOGIN_FAIL_WINDOW) >= LOGIN_FAIL_MAX:
        db.execute("INSERT INTO attempts (kind, key, ts) VALUES ('loginlock', ?, ?)",
                   (key, time.time()))
        db.commit()
        return True
    return False


def clear_login_fails(key):
    db = get_db()
    db.execute("DELETE FROM attempts WHERE kind IN ('loginfail','loginlock')"
               ' AND key = ?', (key,))
    db.commit()


def save_admin_session(email, password):
    """凭据存服务端 sessions 表,cookie 里只放随机 sid。返回 sid。"""
    db = get_db()
    db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
    sid = secrets.token_urlsafe(24)
    db.execute('INSERT INTO sessions (sid, email, pw, expires) VALUES (?,?,?,?)',
               (sid, email, password,
                time.time() + app.config['PERMANENT_SESSION_LIFETIME'].total_seconds()))
    db.commit()
    return sid


def get_admin_creds():
    """从服务端会话表取凭据,过期/不存在返回 None。"""
    sid = session.get('sid')
    if not sid:
        return None
    row = get_db().execute(
        'SELECT email, pw, expires FROM sessions WHERE sid = ?', (sid,)).fetchone()
    if row is None or row['expires'] < time.time():
        return None
    return row['email'], row['pw']


def drop_admin_session():
    sid = session.get('sid')
    if sid:
        db = get_db()
        db.execute('DELETE FROM sessions WHERE sid = ?', (sid,))
        db.commit()


@app.before_request
def csrf_protect():
    """管理端 POST(登录除外)校验 CSRF token;已登录 GET 确保 token 存在。"""
    if not request.path.startswith('/admin/'):
        return None
    if request.path == '/admin/login':
        return None
    if not session.get('admin'):
        return None
    if request.method == 'POST':
        if request.form.get('csrf') != session.get('csrf'):
            abort(403)
    elif 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(16)
    return None


@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "frame-ancestors 'none'; base-uri 'none'")
    if request.headers.get('X-Forwarded-Proto') == 'https':
        resp.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return resp


# ---------- 客户端取件 ----------

@app.route('/')
def index():
    return redirect(url_for('admin_login'))


# 验证码邮件缓存:(maildir, key) -> (mtime, entry|None),entry 为展示用 dict。
# 轮询接口每 5 秒跑一次,靠 mtime 命中缓存避免反复解析全部邮件。
_code_cache = {}


def _entry_for(maildir, key, mt):
    ck = (maildir, key)
    ent = _code_cache.get(ck)
    if ent is not None and ent[0] == mt:
        return ent[1]
    entry = None
    msg = open_message(maildir, key)
    if msg is not None:
        text_body, html_body, _atts = extract_parts(msg)
        code = extract_code(hdr(msg, 'Subject'),
                            msg_plain_text(msg, text_body, html_body))
        if code is not None:
            entry = {
                'key': key,
                'from': hdr(msg, 'From'),
                'subject': hdr(msg, 'Subject') or '(无主题)',
                'code': code,
                'date': msg_date(msg, mt).strftime('%Y-%m-%d %H:%M'),
            }
    if len(_code_cache) > 20000:
        _code_cache.clear()
    _code_cache[ck] = (mt, entry)
    return entry


def scan_code_mails(maildir):
    """扫描邮箱,返回验证码邮件列表(mtime 倒序)。"""
    code_mails = []
    for key, mt in list_messages(maildir):
        entry = _entry_for(maildir, key, mt)
        if entry is not None:
            code_mails.append(entry)
    return code_mails


LOG_THROTTLE_SEC = 60  # 同一链接 60 秒内的重复列表页访问只记第一条


def client_invalid(link):
    """客户端页面类路由的失效处理:渲染正式提示页(HTTP 404)。
    区分 token 不存在 / 已撤销 / 已过期三种情况。
    token 不存在时对来源 IP 计数限速,1 分钟内超 20 次直接 429(防枚举)。"""
    if link is None:
        ip = client_ip()
        record_attempt('t404', ip)
        n = attempt_count('t404', ip, TOKEN404_WINDOW)
        _sec_log.info('TOKEN404 ip=%s count=%d', ip, n)
        if n > TOKEN404_MAX:
            _sec_log.info('TOKEN429 ip=%s', ip)
            return 'Too Many Requests', 429
        kind, email = 'notfound', None
    elif not link['active']:
        kind, email = 'revoked', mask_email(link['email'])
    else:
        kind, email = 'expired', mask_email(link['email'])
    return render_template('invalid.html', kind=kind, email=email), 404


@app.route('/m/<token>')
def inbox(token):
    link = get_link(token)
    if link is None or not link['active'] or link_expired(link):
        return client_invalid(link)
    maildir = maildir_path(link['email'])
    if maildir is None:
        abort(404)

    now = time.time()
    db = get_db()
    last = db.execute(
        'SELECT MAX(ts) FROM access_log WHERE link_id = ?', (link['id'],)
    ).fetchone()[0]
    if last is None or now - last >= LOG_THROTTLE_SEC:
        db.execute(
            'INSERT INTO access_log (link_id, ts, ip, user_agent) VALUES (?,?,?,?)',
            (link['id'], now, client_ip(),
             request.headers.get('User-Agent', '')[:500]))
        db.commit()

    view = 'all' if request.args.get('view') == 'all' else 'code'
    if view == 'all':
        # 全部邮件视图:不过滤;验证码仍尝试提取(走缓存),有就展示
        entries = list_messages(maildir)
        total = len(entries)
        latest = entries[0][0] if entries else None
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            page = 1
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        mails = []
        for key, mt in entries[(page - 1) * PER_PAGE: page * PER_PAGE]:
            cached = _entry_for(maildir, key, mt)
            msg = open_message(maildir, key)
            if msg is None:
                continue
            mails.append({
                'key': key,
                'from': hdr(msg, 'From'),
                'subject': hdr(msg, 'Subject') or '(无主题)',
                'code': cached['code'] if cached else None,
                'date': msg_date(msg, mt).strftime('%Y-%m-%d %H:%M'),
            })
    else:
        code_mails = scan_code_mails(maildir)
        total = len(code_mails)
        latest = code_mails[0]['key'] if code_mails else None
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            page = 1
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        mails = code_mails[(page - 1) * PER_PAGE: page * PER_PAGE]

    return render_template('inbox.html', email=link['email'], mails=mails,
                           page=page, pages=pages, total=total, token=token,
                           latest=latest, view=view)


@app.route('/m/<token>/check')
def inbox_check(token):
    """轻量轮询接口:返回邮件总数和最新一封的标识,不写访问日志。
    默认验证码视图;?view=all 返回整个收件箱的统计。"""
    link = get_link(token)
    if link is None or not link['active'] or link_expired(link):
        return {'error': 'invalid'}, 404
    maildir = maildir_path(link['email'])
    if maildir is None:
        return {'error': 'invalid'}, 404
    if request.args.get('view') == 'all':
        entries = list_messages(maildir)
        return {'count': len(entries),
                'latest': entries[0][0] if entries else None}
    code_mails = scan_code_mails(maildir)
    return {'count': len(code_mails),
            'latest': code_mails[0]['key'] if code_mails else None}


@app.route('/m/<token>/msg/<path:key>')
def message_view(token, key):
    link = get_link(token)
    if link is None or not link['active'] or link_expired(link):
        return client_invalid(link)
    maildir = maildir_path(link['email'])
    if maildir is None:
        abort(404)
    msg = open_message(maildir, key)
    if msg is None:
        abort(404)
    text_body, html_body, atts = extract_parts(msg)
    code = extract_code(hdr(msg, 'Subject'),
                        msg_plain_text(msg, text_body, html_body))
    # 远端资源默认不加载;?remote=1 时渲染原始 HTML
    remote = request.args.get('remote') == '1'
    if html_body and not remote:
        html_body = strip_remote_resources(html_body)
    # 链接有效时完整显示邮箱地址(打码只在失效提示页生效,见 client_invalid)
    return render_template(
        'message.html', email=link['email'], token=token, key=key,
        subject=hdr(msg, 'Subject') or '(无主题)',
        from_addr=hdr(msg, 'From'), to_addr=hdr(msg, 'To'),
        date=msg_date(msg).strftime('%Y-%m-%d %H:%M:%S'),
        code=code, remote=remote,
        text_body=text_body, html_body=html_body, atts=atts)


@app.route('/m/<token>/msg/<path:key>/att/<int:idx>')
def attachment(token, key, idx):
    link = get_link(token)
    if link is None or not link['active'] or link_expired(link):
        return client_invalid(link)
    maildir = maildir_path(link['email'])
    if maildir is None:
        abort(404)
    msg = open_message(maildir, key)
    if msg is None:
        abort(404)
    atts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = part.get_content_disposition()
            if disp == 'attachment' or (disp is None and part.get_filename()):
                atts.append(part)
    if idx < 0 or idx >= len(atts):
        abort(404)
    part = atts[idx]
    fn = part.get_filename() or 'attachment'
    data = part.get_payload(decode=True) or b''
    return Response(data, mimetype=part.get_content_type(), headers={
        'Content-Disposition': "attachment; filename*=utf-8''%s"
        % urllib.parse.quote(fn)})


# ---------- 管理端 ----------

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        ip = client_ip()
        key = '%s|%s' % (ip, email)
        if login_locked(key):
            _sec_log.info('LOGIN_LOCKED ip=%s email=%s', ip, email)
            error = '登录失败:邮箱或密码错误,或尝试次数过多,请 15 分钟后再试'
        else:
            status, _ = miab_query(email, password)
            if status == 200:
                clear_login_fails(key)
                drop_admin_session()
                session.clear()
                session['sid'] = save_admin_session(email, password)
                session['admin'] = email
                session['csrf'] = secrets.token_urlsafe(16)
                session.permanent = True
                return redirect(url_for('admin_home'))
            record_attempt('loginfail', key)
            _sec_log.info('LOGIN_FAIL ip=%s email=%s', ip, email)
            error = '登录失败:邮箱或密码错误,或该账号不是管理员'
    return render_template('login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    drop_admin_session()
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/')
def admin_home():
    r = admin_required()
    if r:
        return r
    boxes = miab_users(*get_admin_creds())
    if boxes is None:
        session.clear()
        return redirect(url_for('admin_login'))
    all_boxes = boxes
    db = get_db()
    rows = db.execute('''
        SELECT l.*, COUNT(a.id) AS visits, MAX(a.ts) AS last_visit
        FROM links l LEFT JOIN access_log a ON a.link_id = l.id
        GROUP BY l.id ORDER BY l.id DESC
    ''').fetchall()
    now = time.time()
    links_by_email = {}
    for row in rows:
        links_by_email.setdefault(row['email'], []).append(
            link_view(row, request.host, now, row['visits'], row['last_visit']))
    new_url = session.pop('new_url', None)
    flash_msg = session.pop('flash_msg', None)
    # 首页只显示已创建过取件链接的邮箱;all_boxes 供"新增链接"下拉选择
    boxes = [b for b in boxes if b in links_by_email]
    revoked_count = sum(
        1 for ls in links_by_email.values() for l in ls if not l['active'])
    return render_template('admin.html', boxes=boxes, all_boxes=all_boxes,
                           new_url=new_url, flash_msg=flash_msg,
                           revoked_count=revoked_count,
                           links_by_email=links_by_email)


def _new_token(db):
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        if not db.execute('SELECT 1 FROM links WHERE token = ?',
                          (token,)).fetchone():
            return token
    abort(500)


def _safe_next():
    nxt = request.form.get('next', '')
    return nxt if nxt.startswith('/admin') else url_for('admin_home')


@app.route('/admin/generate', methods=['POST'])
def admin_generate():
    r = admin_required()
    if r:
        return r
    email = request.form.get('email', '').strip()
    if not email or '@' not in email:
        abort(400)
    try:
        expires = calc_expires(request.form.get('ttl', '0'))
    except ValueError:
        abort(400)
    db = get_db()
    token = _new_token(db)
    db.execute(
        'INSERT INTO links (token, email, created_at, active, expires_at)'
        ' VALUES (?,?,?,1,?)',
        (token, email, time.time(), expires))
    db.commit()
    session['new_url'] = 'https://%s/m/%s' % (request.host, token)
    return redirect(_safe_next())


@app.route('/admin/links/<int:lid>/reset', methods=['POST'])
def admin_reset(lid):
    """重置:换新 token(旧 URL 立即失效),有效期按所选时长从当前重新计时,
    并恢复为启用状态。"""
    r = admin_required()
    if r:
        return r
    db = get_db()
    link = db.execute('SELECT * FROM links WHERE id = ?', (lid,)).fetchone()
    if link is None:
        abort(404)
    try:
        expires = calc_expires(request.form.get('ttl', '0'))
    except ValueError:
        abort(400)
    token = _new_token(db)
    db.execute(
        'UPDATE links SET token = ?, created_at = ?, expires_at = ?, active = 1'
        ' WHERE id = ?',
        (token, time.time(), expires, lid))
    db.commit()
    session['new_url'] = 'https://%s/m/%s' % (request.host, token)
    return redirect(_safe_next())


@app.route('/admin/links')
def admin_links():
    r = admin_required()
    if r:
        return r
    db = get_db()
    rows = db.execute('''
        SELECT l.*, COUNT(a.id) AS visits, MAX(a.ts) AS last_visit
        FROM links l LEFT JOIN access_log a ON a.link_id = l.id
        GROUP BY l.id ORDER BY l.id DESC
    ''').fetchall()
    now = time.time()
    links = [link_view(row, request.host, now, row['visits'], row['last_visit'])
             for row in rows]
    new_url = session.pop('new_url', None)
    return render_template('links.html', links=links, new_url=new_url)


@app.route('/admin/links/<int:lid>/toggle', methods=['POST'])
def admin_toggle(lid):
    r = admin_required()
    if r:
        return r
    db = get_db()
    link = db.execute('SELECT * FROM links WHERE id = ?', (lid,)).fetchone()
    if link is None:
        abort(404)
    if link_expired(link):
        # 已过期的链接不能再启用/撤销,只能重置
        return redirect(_safe_next())
    db.execute('UPDATE links SET active = 1 - active WHERE id = ?', (lid,))
    db.commit()
    return redirect(_safe_next())


@app.route('/admin/links/<int:lid>/delete', methods=['POST'])
def admin_delete(lid):
    """彻底删除链接及其访问记录,删除后该 URL 落到"链接不存在"页。"""
    r = admin_required()
    if r:
        return r
    db = get_db()
    db.execute('DELETE FROM access_log WHERE link_id = ?', (lid,))
    db.execute('DELETE FROM links WHERE id = ?', (lid,))
    db.commit()
    session['flash_msg'] = '链接 #%d 已彻底删除' % lid
    return redirect(_safe_next())


@app.route('/admin/links/clear-revoked', methods=['POST'])
def admin_clear_revoked():
    """一键清除所有已撤销链接及其访问记录。"""
    r = admin_required()
    if r:
        return r
    db = get_db()
    ids = [row['id'] for row in db.execute(
        'SELECT id FROM links WHERE active = 0').fetchall()]
    if ids:
        marks = ','.join('?' * len(ids))
        db.execute('DELETE FROM access_log WHERE link_id IN (%s)' % marks, ids)
        db.execute('DELETE FROM links WHERE id IN (%s)' % marks, ids)
        db.commit()
    session['flash_msg'] = '已清除 %d 条已撤销链接' % len(ids)
    return redirect(_safe_next())


@app.route('/admin/links/<int:lid>')
def admin_link_detail(lid):
    r = admin_required()
    if r:
        return r
    db = get_db()
    link = db.execute('SELECT * FROM links WHERE id = ?', (lid,)).fetchone()
    if link is None:
        abort(404)
    logs = db.execute(
        'SELECT * FROM access_log WHERE link_id = ? ORDER BY id DESC',
        (lid,)).fetchall()
    log_list = []
    for row in logs:
        log_list.append({
            'ts': datetime.fromtimestamp(row['ts'], TZ).strftime('%Y-%m-%d %H:%M:%S'),
            'ip': row['ip'],
            'ua': row['user_agent'],
        })
    lv = link_view(link, request.host, time.time(),
                   visits=len(log_list),
                   last_visit=logs[0]['ts'] if logs else None)
    return render_template('link_detail.html', link=lv, logs=log_list)


if __name__ == '__main__':
    # 仅供本地调试;生产用 gunicorn(见 deploy/onemail.service)
    app.run(host=DEV_HOST, port=DEV_PORT)
