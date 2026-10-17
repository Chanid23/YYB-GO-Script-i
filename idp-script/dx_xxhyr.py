#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 电信星享会员日
# cron: 23 8 17-19 10 *
"""
电信星享会员日 dx_xxhyr v1.0.0（2026-10-17 至 2026-10-19，北京时间）
活动地址：https://apps.telefen.com/mallactive/starday

环境变量：
必填 TELEFEN_AUTH：备注#手机号#服务密码#clientType#androidId
获取地址：https://dx.lcmovie.qzz.io/

功能：四项每日任务、已开通云智奖励登记、免费开星盒、奖品和资格券提醒。

依赖：requests、pycryptodome（或 cryptography）；青龙 notify.py。
青龙定时：23 8 17-19 10 *；活动期外自动退出。

"""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET
import requests

CST = timezone(timedelta(hours=8))
BASE = "https://apps.telefen.com"
LOGIN_URL = "https://fnostelecomlogin.iepose.cn/api/step1_login"
TICKET_URL = "https://appgologin.189.cn:9031/map/clientXML"
MERCHANT_PROMO = "e4aaf9573aa5dff9"
TICKET_TARGET_URL = BASE + "/home/tparse?ticket=$ticket$&promoid=" + MERCHANT_PROMO + "&shareid="
ACTIVITY_URL = BASE + "/mallactive/starday"
GAME_ID = "JGSUDOKU-GJAu57y4"
COUPON_ID = "NCA53EE462DA8F3769E5D0EF9E8"
YUNZHI_CODE = "202610171"
START_DAY, END_DAY = "2026-10-17", "2026-10-19"
DES_KEY = b"1234567`90koiuyhgtfrdews"
TASKS = {
    "VIEW_PAGE_STARDAY": "浏览星享会员日",
    "VIEW_PAGE_IAGE": "浏览网龄回馈",
    "VIEW_PAGE_HAOWU": "浏览积分好物",
    "YUNZHI_LOGIN": "访问云智网页端",
}

RECORD_PATTERN = re.compile(
    r"(?P<remark>[^#\r\n]+)#(?P<phone>1[3-9]\d{9})#(?P<password>[^#\r\n]+)#"
    r"(?P<client_type>#[^#\r\n]+#[^#\r\n]+#[^#\r\n]+#)#"
    r"(?P<encoded_id>[2-9:;c-h]{16})"
)


class AuthenticationError(Exception):
    """Only a fixed stage/type or a short validated server code is exposed."""

    def __init__(self, stage, code):
        self.stage = stage if stage in {"配置", "加密", "登录", "取票"} else "认证"
        value = str(code)
        self.code = value if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) else "UNKNOWN"
        super().__init__(f"{self.stage}失败 [{self.code}]")


@dataclass(frozen=True)
class Account:
    remark: str = field(repr=False)
    phone: str = field(repr=False)
    password: str = field(repr=False)
    client_type: str = field(repr=False)
    encoded_id: str = field(repr=False)

    @property
    def label(self):
        value = self.remark.replace(self.phone, "账号").replace(self.password, "[隐藏]")
        return re.sub(r"[\r\n\x00-\x1f]", "", value)[:32] or "账户"


@dataclass(frozen=True)
class TicketCredentials:
    ticket: str = field(repr=False)
    app_login: dict = field(repr=False)
    user_agent: str = field(repr=False)
    source: str = "fresh"


def parse_accounts(text):
    """Accept newline records and QingLong '&'-joined records, not split('&')."""
    result, phones = [], set()
    for number, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        position = 0
        while position < len(line):
            match = RECORD_PATTERN.match(line, position)
            if not match:
                raise AuthenticationError("配置", f"LINE_{number}_FORMAT")
            values = match.groupdict()
            value = values["encoded_id"]
            raw = "".join(chr(ord(c) - 2) for c in value)
            if not re.fullmatch(r"[0-9a-f]{16}", raw) or encode(raw) != value:
                raise AuthenticationError("配置", f"LINE_{number}_ANDROID_ID")
            if values["phone"] in phones:
                raise AuthenticationError("配置", f"LINE_{number}_DUPLICATE")
            phones.add(values["phone"])
            result.append(Account(**values))
            position = match.end()
            while position < len(line) and line[position].isspace():
                position += 1
            if position == len(line):
                break
            if line[position] != "&":
                raise AuthenticationError("配置", f"LINE_{number}_TRAILING")
            position += 1
            while position < len(line) and line[position].isspace():
                position += 1
            if position == len(line):
                raise AuthenticationError("配置", f"LINE_{number}_EMPTY")
    if not result:
        raise AuthenticationError("配置", "TELEFEN_AUTH_MISSING")
    return result


def encode(value):
    return "".join(chr(ord(c) + 2) for c in str(value))


def des(value, decrypt=False):
    """Protocol 3DES-CBC, zero IV, PKCS7, hex wire encoding (same as dxgy)."""
    try:
        from Crypto.Cipher import DES3
        from Crypto.Util.Padding import pad, unpad
        cipher = DES3.new(DES_KEY, DES3.MODE_CBC, b"\0" * 8)
        return (
            unpad(cipher.decrypt(bytes.fromhex(value)), 8).decode("utf-8")
            if decrypt else cipher.encrypt(pad(value.encode("utf-8"), 8)).hex()
        )
    except ImportError:
        try:
            try:
                from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
            except ImportError:
                from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
            from cryptography.hazmat.primitives.ciphers import Cipher, modes
            from cryptography.hazmat.primitives import padding
        except ImportError:
            raise AuthenticationError("加密", "DEPENDENCY_MISSING") from None
        cipher = Cipher(TripleDES(DES_KEY), modes.CBC(b"\0" * 8))
        if decrypt:
            worker = cipher.decryptor()
            padded = worker.update(bytes.fromhex(value)) + worker.finalize()
            unpadder = padding.PKCS7(64).unpadder()
            return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        padder = padding.PKCS7(64).padder()
        padded = padder.update(value.encode("utf-8")) + padder.finalize()
        worker = cipher.encryptor()
        return (worker.update(padded) + worker.finalize()).hex()


def build_user_agent(account):
    parts = account.client_type.split("#")
    rnd = lambda n: "".join(secrets.choice("0123456789abcdef") for _ in range(n))
    b64 = lambda text: base64.b64encode(text.encode()).decode()
    return f"CtClient;{parts[1]};Android;13;{parts[3]};{b64(rnd(12))}!#!{b64(rnd(8))}"


def build_ticket_xml(account, login, now=None):
    """Keep literal '$ticket$' inside the encrypted URL, just as dxgy did."""
    doc = ET.Element("Request")
    header = ET.SubElement(doc, "HeaderInfos")
    fields = {
        "Code": "getSingle", "Timestamp": (now or datetime.now(CST)).strftime("%Y%m%d%H%M%S"),
        "BroadAccount": "", "BroadToken": "", "ClientType": account.client_type,
        "ShopId": "20002", "Source": "110003", "SourcePassword": "Sid98s",
        "Token": login["token"], "UserLoginName": encode(account.phone),
    }
    for key, value in fields.items():
        ET.SubElement(header, key).text = str(value)
    content = ET.SubElement(doc, "Content")
    ET.SubElement(content, "Attach").text = "test"
    field_data = ET.SubElement(content, "FieldData")
    ET.SubElement(field_data, "TargetId").text = des(str(login["userId"]))
    ET.SubElement(field_data, "Url").text = des(TICKET_TARGET_URL)
    return ET.tostring(doc, encoding="utf-8")


def auth_request(session, stage, method, url, **kwargs):
    """No retries, no redirect-following, default certificate verification."""
    kwargs.setdefault("timeout", (5, 35) if stage == "登录" else (5, 20))
    kwargs["allow_redirects"] = False
    try:
        response = session.request(method, url, **kwargs)
    except requests.exceptions.SSLError:
        raise AuthenticationError(stage, "TLS_CERTIFICATE") from None
    except requests.exceptions.Timeout:
        raise AuthenticationError(stage, "TIMEOUT") from None
    except requests.RequestException:
        raise AuthenticationError(stage, "NETWORK") from None
    if response.status_code != 200:
        raise AuthenticationError(stage, "HTTP_" + str(response.status_code))
    return response


def _normalize_login(value):
    if not isinstance(value, dict):
        return None
    token, user_id = value.get("token"), value.get("userId")
    if not isinstance(token, str) or not token or not user_id:
        return None
    if len(token) > 8192 or len(str(user_id)) > 256:
        return None
    return {"token": token, "userId": str(user_id)}


def exchange_ticket(account, login, user_agent, session_factory=requests.Session):
    body = build_ticket_xml(account, login)
    with session_factory() as session:
        response = auth_request(session, "取票", "POST", TICKET_URL, data=body,
            headers={"Content-Type": "application/xml;charset=UTF-8", "User-Agent": user_agent})
    try:
        if len(response.content) > 1024 * 1024:
            raise AuthenticationError("取票", "RESPONSE_TOO_LARGE")
        xml = ET.fromstring(response.content)
    except (ET.ParseError, ValueError):
        raise AuthenticationError("取票", "INVALID_XML") from None
    node = next((n for n in xml.iter() if n.tag.rsplit("}", 1)[-1].lower() == "ticket" and n.text), None)
    if node is None:
        code_node = next((n for n in xml.iter() if n.tag.rsplit("}", 1)[-1].lower() in {"resultcode", "code"} and n.text), None)
        code = code_node.text.strip() if code_node is not None else "TICKET_MISSING"
        raise AuthenticationError("取票", code)
    try:
        ticket = des(node.text.strip(), decrypt=True)
    except (ValueError, UnicodeError):
        raise AuthenticationError("取票", "INVALID_TICKET") from None
    if not ticket or len(ticket) > 8192 or re.search(r"[\s\x00-\x1f]", ticket):
        raise AuthenticationError("取票", "INVALID_TICKET")
    return ticket


def login_ticket(account, cached_login=None, user_agent=None, session_factory=requests.Session):
    """Try cached APP auth once, then at most one service-password login.

    Return credentials in memory only. The caller must validate the new activity
    session and account before any lottery action; a returned ticket alone is not
    proof of activity login. Do not print the return value's fields.
    """
    ua = user_agent or build_user_agent(account)
    cached = _normalize_login(cached_login)
    if cached:
        try:
            ticket = exchange_ticket(account, cached, ua, session_factory)
            return TicketCredentials(ticket, cached, ua, "cached")
        except AuthenticationError as exc:
            if exc.stage == "加密" or exc.code in {"NETWORK", "TIMEOUT", "TLS_CERTIFICATE"} or exc.code.startswith("HTTP_5"):
                raise
    # Fail missing crypto dependencies before making the one password request.
    des("dependency-check")
    with session_factory() as session:
        response = auth_request(session, "登录", "POST", LOGIN_URL, json={
            "phone": account.phone, "password": account.password, "remark": account.remark,
            "clientType": account.client_type, "androidId": account.encoded_id,
            "androidVersion": "13", "userAgent": ua,
        })
    try:
        data = response.json()
    except ValueError:
        raise AuthenticationError("登录", "INVALID_JSON") from None
    if not isinstance(data, dict) or data.get("success") is not True:
        if isinstance(data, dict) and data.get("needVerifyCode"):
            raise AuthenticationError("登录", "MANUAL_VERIFICATION")
        raise AuthenticationError("登录", data.get("resultCode") or "FAILED" if isinstance(data, dict) else "INVALID_SHAPE")
    login = _normalize_login(data)
    if not login:
        raise AuthenticationError("登录", "TOKEN_FIELDS_MISSING")
    ticket = exchange_ticket(account, login, ua, session_factory)
    return TicketCredentials(ticket, login, ua, "fresh")


class ActivityError(Exception):
    pass


class BusinessError(ActivityError):
    def __init__(self, code, message):
        code = str(code)
        self.code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", code) else "UNKNOWN"
        self.message = clean(message)
        super().__init__(f"业务返回 {self.code}：{self.message}")


def now():
    return datetime.now(CST)


def clean(value, account=None):
    text = str(value or "")
    if account:
        for secret in (account.phone, account.password, account.client_type, account.encoded_id):
            text = text.replace(secret, "[隐藏]")
    text = re.sub(r"https?://[^\s<>]+", "[活动链接]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
    text = re.sub(r"(?i)(?:ticket|token|cookie|password|authorization)\s*[:=]\s*\S+", "[鉴权信息]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[长标识]", text)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:180]


def log(message, account=None):
    label = clean(account.remark, account) if account else "星享会员日"
    print(f"[{now():%H:%M:%S}] [{label or '账户'}] {clean(message, account)}", flush=True)


def integer(value, label="次数", maximum=100):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)) or int(value) > maximum:
        raise ActivityError(label + "字段异常，已停止操作")
    return int(value)


def write_json(path, data):
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        salt = self.root / ".salt"
        try:
            with os.fdopen(os.open(salt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.salt = salt.read_bytes()
        if len(self.salt) != 32:
            raise ActivityError("状态目录密钥损坏，请保留原文件排查")

    def key(self, account):
        return hmac.new(self.salt, account.phone.encode(), hashlib.sha256).hexdigest()

    def fingerprint(self, account):
        text = "\0".join((account.phone, account.password, account.client_type, account.encoded_id))
        return hmac.new(self.salt, text.encode(), hashlib.sha256).hexdigest()

    def path(self, account, kind):
        return self.root / (self.key(account) + "." + kind + ".json")

    def read(self, account, kind):
        path = self.path(account, kind)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise ActivityError("账户缓存损坏，已保留原文件并暂停账户") from None

    def save(self, account, kind, data):
        write_json(self.path(account, kind), data)

    @contextmanager
    def lock(self):
        with (self.root / "run.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ActivityError("已有任务运行，本次退出以避免重复抽奖") from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def import_cache(account):
    """Read compatible sibling caches; never modify another activity's state."""
    parents = [Path(__file__).resolve().parent, Path('/ql/data/scripts'),
               Path('/ql/data/scripts/lcmovie_YYB-GO-Script-i_main/idp-script')]
    for parent in dict.fromkeys(parents):
        root = parent / '.telefen_iage_202610'
        try:
            salt = (root / '.salt').read_bytes()
            if len(salt) != 32:
                continue
            key = hmac.new(salt, account.phone.encode(), hashlib.sha256).hexdigest()
            data = json.loads((root / (key + '.auth.json')).read_text('utf-8'))
            raw = '\0'.join((account.phone, account.password, account.client_type, account.encoded_id))
            fp = hmac.new(salt, raw.encode(), hashlib.sha256).hexdigest()
            if data.get('fingerprint') == fp and (data.get('cookie') or _normalize_login(data.get('app_login'))):
                return data
        except (OSError, ValueError, AttributeError):
            pass
    for parent in dict.fromkeys(parents):
        try:
            data = json.loads((parent / '.telecom_wheel_state/app_tokens.json').read_text('utf-8'))
            login = _normalize_login(data.get(account.phone))
            if login:
                return {'app_login': login}
        except (OSError, ValueError, AttributeError):
            pass
    return {}


def masked_match(observed, expected):
    text = str(observed or "")
    return len(text) == 11 and text.count('*') <= 4 and all(a == '*' or a == b for a, b in zip(text, expected))


class Client:
    def __init__(self, account, store):
        self.account, self.store = account, store
        self.cache = store.read(account, 'auth')
        if self.cache.get('fingerprint') != store.fingerprint(account):
            self.cache = {}
        self.cache = self.cache or import_cache(account)
        self.ua = self.cache.get('user_agent') or build_user_agent(account)
        self.channel = self.cache.get('channel') or 'HGOKHD'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.ua, 'Accept': 'application/json', 'Referer': ACTIVITY_URL})
        self.info = {}
        self.day_code = now().strftime('%Y%m%d')

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', (5, 25))
        kwargs['allow_redirects'] = False
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException:
            raise ActivityError('网络连接失败或超时，未自动重试') from None

    def call(self, method, endpoint, params=None, payload=None):
        allowed = {
            ('GET', '/nstars/api/account/userinfo'),
            ('GET', '/mallactive/api/account/must'),
            ('GET', '/mallactive/api/v917/activity/home'),
            ('POST', '/mallactive/api/v917/complete'),
            ('POST', '/mallactive/api/v917/yzOrderIsBuy'),
            ('GET', '/mallactive/api/v917/getSkillId'),
            ('GET', '/mallactive/api/game/freeCount'),
            ('GET', '/mallactive/api/game/gameInfo'),
            ('GET', '/mallactive/api/game/lottery'),
            ('GET', '/mallactive/api/coupon/usedcoupon'),
        }
        if (method, endpoint) not in allowed:
            raise ActivityError('接口不在本活动允许范围')
        self.session.headers['appCode'] = self.channel
        response = self.request(method, BASE + endpoint, params=params, json=payload)
        if response.status_code != 200:
            raise ActivityError(f'活动接口 HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError:
            raise ActivityError('活动接口返回非 JSON，可能会话失效或站点防护') from None
        if not isinstance(data, dict) or str(data.get('errCode')) != '0000':
            raise BusinessError(data.get('errCode'), data.get('errMsg')) if isinstance(data, dict) else ActivityError('业务响应结构异常')
        return data.get('data')

    def validate(self):
        user = self.call('GET', '/nstars/api/account/userinfo', params={'noload': 'true'})
        if not isinstance(user, dict) or not masked_match((user.get('userInfo') or {}).get('loginName'), self.account.phone):
            raise ActivityError('商城账户核验失败，未执行任务')
        info = self.call('GET', '/mallactive/api/account/must')
        if not isinstance(info, dict) or not (masked_match(info.get('deviceNo'), self.account.phone) or masked_match(info.get('phone'), self.account.phone)):
            raise ActivityError('活动账户核验失败，未执行任务')
        self.info = info

    def save_auth(self, login=None):
        cookie = next((c.value for c in self.session.cookies if c.name == 'newmallsession' and c.domain.lstrip('.') == 'apps.telefen.com'), None)
        if not cookie:
            raise ActivityError('商城未下发活动会话')
        data = {'fingerprint': self.store.fingerprint(self.account), 'cookie': cookie,
                'channel': self.channel, 'user_agent': self.ua,
                'app_login': login or self.cache.get('app_login'), 'updated': now().isoformat()}
        self.store.save(self.account, 'auth', data)

    def authenticate(self):
        if self.cache.get('cookie'):
            self.session.cookies.set('newmallsession', self.cache['cookie'], domain='apps.telefen.com', path='/', secure=True)
            try:
                self.validate()
                self.save_auth()
                log('🔐 会话缓存有效，账户核验通过', self.account)
                return
            except ActivityError:
                self.session.cookies.clear()
        cached = _normalize_login(self.cache.get('app_login'))
        gate = self.store.read(self.account, 'login_gate')
        blocked_login = gate.get('day') == now().date().isoformat()
        if not cached and blocked_login:
            raise ActivityError('今日登录已失败，请人工核查账号或验证码，暂停重复密码登录')
        try:
            if blocked_login:
                credentials = TicketCredentials(exchange_ticket(self.account, cached, self.ua), cached, self.ua, 'cached')
            else:
                credentials = login_ticket(self.account, cached_login=cached, user_agent=self.ua)
        except AuthenticationError as exc:
            if exc.stage == '登录':
                self.store.save(self.account, 'login_gate', {'day': now().date().isoformat(), 'code': exc.code})
            raise
        self.session.cookies.clear()
        url = BASE + '/home/tparse?' + urlencode({'ticket': credentials.ticket, 'promoid': MERCHANT_PROMO, 'shareid': ''})
        for _ in range(6):
            uri = urlsplit(url)
            if uri.scheme != 'https' or uri.hostname != 'apps.telefen.com':
                raise ActivityError('商城会话跳转域名异常')
            channel = parse_qs(uri.query).get('channel', [self.channel])[0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', channel):
                raise ActivityError('商城渠道格式异常')
            self.channel = channel
            response = self.request('GET', url)
            if response.status_code not in (301, 302, 303, 307, 308):
                if response.status_code != 200:
                    raise ActivityError(f'商城登录跳转 HTTP {response.status_code}')
                break
            url = urljoin(url, response.headers.get('Location', ''))
        self.validate()
        self.save_auth(credentials.app_login)
        log('🔐 APP Token 换票成功，活动账户核验通过', self.account)

    def home(self, code):
        data = self.call('GET', '/mallactive/api/v917/activity/home', params={'noload': 'true', 'activeCode': code})
        if not isinstance(data, dict) or str(data.get('activeCode')) != code or data.get('gameId') != GAME_ID or not isinstance(data.get('taskList'), list):
            raise ActivityError('活动编码、抽奖编号或任务列表不匹配')
        return data

    def count(self):
        return integer(self.call('GET', '/mallactive/api/game/freeCount', params=self.game_params()))

    def game_params(self):
        return {'noload': 'true', 'appId': GAME_ID, 'mchId': '0', 'channel': self.channel}

    def complete(self, task, code):
        if task not in TASKS and task != 'YUNZHI_OPEN':
            raise ActivityError('未识别的任务')
        self.assert_day(code if task != 'YUNZHI_OPEN' else self.day_code)
        channel_id = {'SWX': 37, 'HGOWAP': 20, 'HGOKHD': 21, 'TYYX': 50}.get(self.channel.upper(), 37)
        return self.call('POST', '/mallactive/api/v917/complete', payload={'activityId': code, 'taskType': task, 'channelId': channel_id})

    def assert_day(self, code):
        if code != self.day_code or now().strftime('%Y%m%d') != code or not START_DAY <= now().date().isoformat() <= END_DAY:
            raise ActivityError('日期已切换或活动已结束，请按当天任务重新运行')

    def lottery(self):
        try:
            return self.call('GET', '/mallactive/api/game/lottery', params={**self.game_params(), 'jgg': 'true'})
        except BusinessError as exc:
            # The official lotteryJGG client recognizes these as an ordinary loss.
            if exc.code in {'0009', '0099'} and '未中奖' in exc.message:
                return {'rewardId': 0, 'productName': '谢谢参与'}
            raise

    def verify_free_game(self):
        data = self.call('GET', '/mallactive/api/game/gameInfo', params=self.game_params())
        try:
            game = data['gameData']['gameInfo']
            start = datetime.strptime(game['startDate'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
            end = datetime.strptime(game['expireDay'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
            valid = (str(game['activityId']) == '52485' and str(game['isJfPay']) == '0'
                     and str(game['price']) == '0' and str(game['status']) == '1' and start <= now() < end)
        except (TypeError, KeyError, ValueError):
            valid = False
        if not valid:
            raise ActivityError('免费抽奖规则、活动编号或有效期不匹配，已停抽')

    def close(self):
        self.session.close()


def task_map(home):
    return {row['taskType']: row for row in home.get('taskList', []) if isinstance(row, dict) and isinstance(row.get('taskType'), str)}


def done(row):
    return isinstance(row, dict) and row.get('isFinished') in (True, 1, '1')


def perform_tasks(client, result, query_only):
    code = now().strftime('%Y%m%d')
    home = client.home(code)
    rows = task_map(home)
    for task, label in TASKS.items():
        if task not in rows:
            result['warnings'].append(label + '：服务端未提供')
            continue
        if done(rows[task]):
            log('✓ ' + label + '：今日已完成', client.account)
            continue
        if query_only:
            log('○ ' + label + '：待完成', client.account)
            continue
        try:
            client.complete(task, code)
        except ActivityError as exc:
            log('⚠ ' + label + '：' + str(exc), client.account)
        # Reconcile the task list, including a timeout or business error.
        rows = task_map(client.home(code))
        if done(rows.get(task)):
            log('✓ ' + label + '：完成并回读确认', client.account)
        else:
            result['warnings'].append(label + '：未确认完成')
            log('⚠ ' + label + '：未确认完成，本轮不重复提交', client.account)
        time.sleep(1)
    result['tasks'] = sum(done(rows.get(k)) for k in TASKS)
    yz_home = client.home(YUNZHI_CODE)
    yz_row = task_map(yz_home).get('YUNZHI_OPEN')
    restricted = str(client.info.get('provinceId', '')).zfill(2) in {'01', '03', '05', '25'}
    if done(yz_row):
        result['yunzhi'] = '已完成'
    elif restricted:
        result['yunzhi'] = '本省不展示开通任务'
    elif yz_row is None:
        result['yunzhi'] = '服务端未提供'
    else:
        status = client.call('POST', '/mallactive/api/v917/yzOrderIsBuy', payload={'noload': True})
        if str(status) == '2':
            if not query_only:
                client.complete('YUNZHI_OPEN', YUNZHI_CODE)
                yz_row = task_map(client.home(YUNZHI_CODE)).get('YUNZHI_OPEN')
            result['yunzhi'] = '已完成' if done(yz_row) else '已开通，奖励待登记'
        else:
            result['yunzhi'] = '未开通，跳过'
    log('☁ 云智状态：' + result['yunzhi'], client.account)


def draw_free(client, store, result, query_only):
    client.verify_free_game()
    remaining = client.count()
    result['remaining'] = remaining
    log(f'🎁 可用免费次数：{remaining}', client.account)
    if query_only:
        return
    day = now().date().isoformat()
    state = store.read(client.account, 'draws')
    if state.get('pending'):
        result['warnings'].append('上次抽奖结果待核对，已停抽；请在活动页我的奖品核实')
        return
    daily = state.setdefault('days', {}).setdefault(day, {'attempts': 0, 'prizes': []})
    attempts = integer(daily.get('attempts'), '已提交次数', maximum=10)
    for _ in range(min(remaining, 10 - attempts)):
        client.assert_day(day.replace('-', ''))
        before = client.count()
        if before < 1:
            remaining = before
            break
        state['pending'] = {'day': day, 'before': before, 'at': now().isoformat()}
        daily['attempts'] += 1
        store.save(client.account, 'draws', state)
        try:
            data = client.lottery()
            if not isinstance(data, dict) or not isinstance(data.get('productName'), str) or not data.get('productName') or data.get('rewardId') is None:
                raise ActivityError('抽奖响应缺少奖品信息')
            prize = clean(data['productName'], client.account)
            # Save the received prize before any subsequent network request.
            daily['prizes'].append(prize)
            state['pending']['prize'] = prize
            store.save(client.account, 'draws', state)
            result['prizes'].append(prize)
            mark = '🎲' if prize in {'谢谢参与', '未中奖'} else '🎉'
            log(f'{mark} 第 {len(result["prizes"])} 次：{prize}', client.account)
            remaining = client.count()
            if remaining != before - 1:
                raise ActivityError('抽奖次数未按预期减少，已保留待核对状态')
            state['pending'] = None
            store.save(client.account, 'draws', state)
        except ActivityError as exc:
            result['warnings'].append('抽奖已暂停：' + str(exc) + '；请核对我的奖品')
            log('⚠ 抽奖结果或次数待核对，保留记录并停止重复提交', client.account)
            try:
                remaining = client.count()
            except ActivityError:
                remaining = None
            break
        if remaining == 0:
            break
        time.sleep(2)
    result['remaining'] = remaining
    if remaining and daily['attempts'] >= 10:
        result['warnings'].append('已达每日10次保护上限，剩余次数请人工核实')
    log(f'🎁 本轮抽奖 {len(result["prizes"])} 次，剩余：{remaining if remaining is not None else "待核对"}', client.account)


def benefits(client, result):
    coupons = client.call('GET', '/mallactive/api/coupon/usedcoupon', params={'couponIds': COUPON_ID, 'noload': 'true'})
    if not isinstance(coupons, list):
        raise ActivityError('资格券状态结构异常')
    for coupon in coupons:
        if coupon.get('couponId') != COUPON_ID:
            continue
        if integer(coupon.get('userReceivedCount', 0), '资格券数量', 1000) > 0:
            unused = integer(coupon.get('userUnusedCount', 0), '未使用券数量', 1000)
            result['coupon'] = '已获得1积分品质好物10选1' + ('，请及时手动兑换' if unused else '，已使用或当前无未用券')
            log('🏆 ' + result['coupon'], client.account)
    skill = client.call('GET', '/mallactive/api/v917/getSkillId', params={'noload': 'true', 'type': '0'})
    if skill:
        result['discount'] = '已有折扣资格，可在活动页查看好物；兑换需手动操作'
    else:
        result['discount'] = '未查询到翻牌折扣资格，可前往活动页查看'


def load_notify():
    for path in (Path('/ql/data/scripts/notify.py'), Path('/ql/scripts/notify.py'), Path(__file__).resolve().parent / 'notify.py'):
        if path.is_file():
            try:
                spec = importlib.util.spec_from_file_location('xxhyr_ql_notify', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if callable(getattr(module, 'send', None)):
                    return module.send
            except Exception:
                continue
    return None


def summary(results, elapsed, query_only):
    ok = sum(r['status'] == '正常' for r in results)
    lines = ['✨ 电信星享会员日' + (' · 仅查询' if query_only else ''),
             f'📅 {now():%Y-%m-%d %H:%M}（北京）',
             f'👥 账户 {len(results)}｜正常 {ok}｜需留意 {len(results)-ok}',
             f'🎲 本轮抽奖 {sum(len(r["prizes"]) for r in results)} 次｜耗时 {elapsed:.1f} 秒']
    for i, r in enumerate(results, 1):
        lines.extend(['', '━━━━━━━━━━━━━━', f'【{i}】{r["label"]} · {r["status"]}',
                      f'📋 每日任务：{r["tasks"]}/4', '☁ 云智：' + r['yunzhi'],
                      '🎁 剩余免费次数：' + str(r['remaining'] if r['remaining'] is not None else '未查询')])
        if r['prizes']:
            for prize in dict.fromkeys(r['prizes']):
                mark = '🎲' if prize in {'谢谢参与', '未中奖'} else '🎉'
                lines.append(mark + ' ' + prize + ' × ' + str(r['prizes'].count(prize)))
        else:
            lines.append('🎲 本轮无新增抽奖结果')
        if r.get('coupon'):
            lines.append('🏆 ' + r['coupon'])
        if r.get('discount'):
            lines.append('🃏 ' + r['discount'])
        lines.extend('⚠ ' + message for message in r['warnings'])
    lines.extend(['', '📌 奖品请在活动页“我的奖品”查看，资格券请在有效期内手动兑换。'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='电信星享会员日：每日任务、免费开星盒及青龙通知')
    parser.add_argument('--query-only', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args(argv)
    started, results = time.monotonic(), []
    if not START_DAY <= now().date().isoformat() <= END_DAY:
        log('活动时间为2026-10-17至10-19，本次已退出')
        return 0
    try:
        accounts = parse_accounts(os.environ.get('TELEFEN_AUTH', ''))
        if args.limit < 0:
            raise ActivityError('--limit 必须大于等于0')
        if args.limit:
            accounts = accounts[:args.limit]
        store = Store(os.environ.get('DX_XXHYR_STATE_DIR') or Path(__file__).resolve().parent / '.dx_xxhyr_state')
        with store.lock():
            log(f'✨ 活动已启动｜账户 {len(accounts)}｜模式：' + ('仅查询' if args.query_only else '每日任务与免费抽奖'))
            for i, account in enumerate(accounts, 1):
                result = {'label': clean(account.remark, account) or f'账户{i}', 'status': '正常',
                          'tasks': 0, 'yunzhi': '未查询', 'prizes': [], 'remaining': None, 'warnings': []}
                client = None
                print('\n' + '═' * 40, flush=True)
                log(f'👤 账户 {i}/{len(accounts)} 开始', account)
                try:
                    client = Client(account, store)
                    client.authenticate()
                    log('⭐ 星级：' + clean(client.info.get('starName') or client.info.get('starLevel')), account)
                    perform_tasks(client, result, args.query_only)
                    draw_free(client, store, result, args.query_only)
                    benefits(client, result)
                    if not args.query_only and result['tasks'] < 4:
                        result['warnings'].append('每日任务未全部确认完成')
                except (AuthenticationError, ActivityError) as exc:
                    result['warnings'].append(clean(str(exc), account))
                except Exception as exc:
                    result['warnings'].append('处理异常：' + type(exc).__name__ + '，详情未输出以保护账户参数')
                finally:
                    if client:
                        client.close()
                if result['warnings']:
                    result['status'] = '需留意'
                results.append(result)
                log('✅ 账户处理结束：' + result['status'], account)
            body = summary(results, time.monotonic() - started, args.query_only)
            print('\n' + '═' * 40 + '\n📣 通知汇总\n' + body + '\n' + '═' * 40, flush=True)
            if not args.no_notify:
                sender = load_notify()
                if sender:
                    try:
                        sender('电信星享会员日 · 运行报告', body)
                        log('📨 已调用青龙通知；送达情况以推送渠道和接收端为准')
                    except Exception:
                        log('⚠ 青龙通知发送失败，完整汇总已打印，业务记录已保留')
                else:
                    log('⚠ 未找到青龙 notify.py，完整汇总已打印')
        return 1 if any(r['warnings'] for r in results) else 0
    except (AuthenticationError, ActivityError) as exc:
        log('⚠ ' + str(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
