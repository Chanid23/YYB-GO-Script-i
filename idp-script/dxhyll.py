#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 电信好运连连
# cron: 37 6 * * *
"""
name: 电信好运连连
cron: 37 6 * * *

环境变量：
必填 TELEFEN_AUTH：备注#手机号#服务密码#clientType#androidId
获取地址：https://dx.lcmovie.qzz.io/

→ 每账号每日3次抽奖（每次10金豆），奖品打印日志并推送汇总通知。
Alpine/musl 无 playwright 轮子，故用原生 CDP 协议直驱系统 chromium。

依赖：requests、websocket-client、pycryptodome、系统 chromium。
     均支持自动安装（多国内镜像依次回退）：缺 chromium 时自动 apk/apt 安装
     （清华/阿里云/中科大镜像，需 root），缺 Python 包时自动 pip 安装。

通知：使用青龙内置 notify.py；通知失败不影响任务结果。

作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests


CST = timezone(timedelta(hours=8))
ACTIVITY_CODE = "ACTCODE2026092442AURL8B"      # URL 上的 activityCode/yxai
ACTIVITY_ID = "ACTIVITY20261013CL4WDPHY"       # 业务接口的 activityId
START_DAY = "2026-10-15"
END_DAY = "2026-11-15"
DAILY_LIMIT = 3
DRAW_COST = 10
BASE = "https://wapmas.189.cn"
LOGIN_URL = "https://fnostelecomlogin.iepose.cn/api/step1_login"
TICKET_URL = "https://appgologin.189.cn:9031/map/clientXML"
ACTIVITY_URL = (
    "https://wapmas.189.cn/mas-pub-ui/spm/newActivity/" + ACTIVITY_CODE
    + "?activityCode=" + ACTIVITY_CODE + "&yxai=" + ACTIVITY_CODE
)
TICKET_TARGET_URL = ACTIVITY_URL + "&ticket=$ticket$&isshare=1"
DES_KEY = b"1234567`90koiuyhgtfrdews"
RECORD_PATTERN = re.compile(
    r"(?P<remark>[^#\r\n]+)#(?P<phone>1[3-9]\d{9})#(?P<password>[^#\r\n]+)#"
    r"(?P<client_type>#[^#\r\n]+#[^#\r\n]+#[^#\r\n]+#)#"
    r"(?P<encoded_id>[2-9:;c-h]{16})"
)


# ---------------------------------------------------------------- 异常
class WheelError(Exception):
    pass


class BusinessError(WheelError):
    pass


class AuthenticationError(Exception):
    """Only a fixed stage/type or a short validated server code is exposed."""

    def __init__(self, stage, code):
        self.stage = stage if stage in {"配置", "加密", "登录", "取票"} else "认证"
        value = str(code)
        self.code = value if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) else "UNKNOWN"
        super().__init__(f"{self.stage}失败 [{self.code}]")


# ---------------------------------------------------------------- 工具
def today():
    return datetime.now(CST).date().isoformat()


def clean(value, account=None):
    text = str(value or "")
    if account is not None:
        for secret in (account.phone, account.password, account.encoded_id, account.client_type):
            if secret:
                text = text.replace(secret, "[隐藏]")
    text = re.sub(r"https?://[^\s<>]+", "[链接]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
    text = re.sub(r"(?i)(?:ticket|token|cookie|password|authorization)\s*[:=]\s*\S+", "[鉴权信息]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[长标识]", text)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:180]


class Log:
    def __init__(self, account=None):
        self.account = account

    def __call__(self, message):
        label = clean(self.account.remark, self.account) if self.account else "任务"
        print(f"[{datetime.now(CST):%H:%M:%S}] [{label or '账号'}] {clean(message, self.account)}", flush=True)


def integer(value, name, maximum=10**12):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise WheelError(name + "字段不是有效非负整数")
    result = int(value)
    if result > maximum:
        raise WheelError(name + "字段超出合理范围")
    return result


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def state_lock(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    stream = open(root / "run.lock", "a+b")
    acquired = False
    try:
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
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise WheelError("已有实例运行；本次退出，避免重复抽奖") from None
        yield root
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class DailyState:
    """同日防重跑：非幂等抽奖前先落盘 pending，结果未知则停抽。"""

    def __init__(self, directory, account, day):
        self.account, self.day = account, day
        root = Path(directory)
        salt_path = root / "salt.json"
        try:
            if not salt_path.exists():
                atomic_json(salt_path, {"salt": secrets.token_hex(32)})
            salt = json.loads(salt_path.read_text("utf-8"))["salt"]
            if not re.fullmatch(r"[a-f0-9]{64}", salt):
                raise ValueError()
            key = hashlib.sha256((salt + ACTIVITY_ID + account.phone).encode()).hexdigest()
            self.path = root / (key + ".json")
            data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else None
            if data is not None:
                if (not isinstance(data, dict) or data.get("version") != 1
                        or data.get("activity_id") != ACTIVITY_ID
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data.get("day", "")))
                        or not isinstance(data.get("attempts"), list)
                        or len(data["attempts"]) > DAILY_LIMIT
                        or not isinstance(data.get("halt"), str)):
                    raise ValueError()
                if any(not isinstance(a, dict) or a.get("status") not in
                       ("pending", "success", "unknown", "rejected") for a in data["attempts"]):
                    raise ValueError()
                if data["day"] > day:
                    raise WheelError("本地状态日期晚于当前日期；请核对系统时间")
            self.data = data if data and data["day"] == day else {
                "version": 1, "activity_id": ACTIVITY_ID, "day": day, "attempts": [], "halt": ""}
        except (ValueError, KeyError, TypeError, OSError):
            raise WheelError("本地状态损坏或不可读；保留原文件并停止，避免重复扣豆") from None

    @property
    def used(self):
        return len(self.data["attempts"])

    @property
    def blocked(self):
        return bool(self.data["halt"] or any(a["status"] in ("pending", "unknown")
                                            for a in self.data["attempts"]))

    def save(self):
        atomic_json(self.path, self.data)

    def reserve(self):
        if self.used >= DAILY_LIMIT or self.blocked:
            raise WheelError("当日抽奖预算耗尽或存在待核验请求")
        self.data["attempts"].append({"status": "pending"})
        self.save()  # Must durably record before the non-idempotent GET is sent.

    def finish(self, status, prize=""):
        self.data["attempts"][-1] = {"status": status, "prize": clean(prize, self.account)}
        if status != "success":
            self.data["halt"] = status
        self.save()


# ---------------------------------------------------------------- 认证（APP 登录 + 换票）
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
    except ImportError:
        if _pip_install("pycryptodome"):
            from Crypto.Cipher import DES3
            from Crypto.Util.Padding import pad, unpad
        else:
            try:
                try:
                    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
                except ImportError:
                    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
                from cryptography.hazmat.primitives.ciphers import Cipher, modes
                from cryptography.hazmat.primitives.padding import padding
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
    cipher = DES3.new(DES_KEY, DES3.MODE_CBC, b"\0" * 8)
    return (
        unpad(cipher.decrypt(bytes.fromhex(value)), 8).decode("utf-8")
        if decrypt else cipher.encrypt(pad(value.encode("utf-8"), 8)).hex()
    )


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
            if exc.stage == "加密":
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


class TokenCache:
    """APP 登录 token 缓存：避免每次运行都走密码登录（登录桥有频率风控）。

    token 仅缓存在 state 目录（权限600）；换票 ticket 本身一次性，不缓存。
    """

    def __init__(self, directory):
        self.path = Path(directory) / "app_tokens.json"
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
        except Exception:
            data = {}
        self.data = data

    def get(self, account):
        entry = self.data.get(account.phone)
        if not isinstance(entry, dict):
            return None
        return _normalize_login(entry)

    def put(self, account, login):
        if not login:
            return
        self.data[account.phone] = {"token": login["token"], "userId": login["userId"]}
        atomic_json(self.path, self.data)


def obtain_ticket(account, cache=None):
    """登录 + getSingle 换票；返回 (ticket, user_agent)。优先用缓存 token 换票。"""
    cached = cache.get(account) if cache is not None else None
    credentials = login_ticket(account, cached_login=cached)
    if credentials.source == "fresh" and cache is not None:
        cache.put(account, credentials.app_login)
    return credentials.ticket, credentials.user_agent


def activity_url(ticket):
    return ACTIVITY_URL + "&ticket=" + urllib.parse.quote(ticket, safe="") + "&isshare=1"


# ---------------------------------------------------------------- 页内 XHR 客户端
# Use the page's XMLHttpRequest, not APIRequestContext/requests: the official
# protection script wraps XHR and produces fresh per-request protection data.
XHR = r"""({path, params, ticket, activityCode, environment}) => new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    const url = '/mas-pub-web' + path + '?' + new URLSearchParams(params).toString();
    xhr.open('GET', url, true);
    xhr.withCredentials = true;
    xhr.timeout = 30000;
    const headers = {Accept: 'application/json, text/plain, */*', ticket,
        id: '', kdticket: '', activityId: '', activityCode, yxai: activityCode,
        wyDataStr: '', wyopenid: '', wycorpId: '', masEnv: environment};
    if (environment === 'wap') headers.wxFlag = '0';
    const phoneModel = sessionStorage.getItem('phoneModel');
    if (phoneModel && environment !== 'wap') headers.phoneModel = phoneModel;
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        resolve({status: xhr.status, data});
    };
    xhr.onerror = () => resolve({status: 0, data: null});
    xhr.ontimeout = () => resolve({status: 0, data: null});
    xhr.onabort = () => resolve({status: 0, data: null});
    xhr.send();
})"""


CDP_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def bridge_script(phone_model):
    """电信APP Android 原生桥 CtclientJS 的桩 + 预写 phoneModel。

    真实 webview 由原生注入 CtclientJS 并写入 sessionStorage.phoneModel；
    headless 环境缺失会让 Vue 根组件 beforeCreate 直接 ReferenceError，
    #app 永不挂载。此处按 app bundle 实际调用点逐一补 no-op。
    """
    return (
        "(function(){if(window.CtclientJS)return;"
        "var m=" + json.dumps(str(phone_model or "")) + ";"
        "try{sessionStorage.setItem('phoneModel',m)}catch(e){}"
        "function n(){}"
        "window.CtclientJS={goLink:n,share:n,closeWebView:n,makecall:n,"
        "updateHomePageFloor:n,getIsImmersion:n,getStatusBarHeight:n,"
        "getPhoneInformation:function(){try{sessionStorage.setItem('phoneModel',m)}catch(e){}}};"
        "})();"
    )


class CdpConnection:
    """极简 CDP 客户端：单线程同步收发，按 id 匹配响应，事件直接丢弃。"""

    def __init__(self, url):
        try:
            import websocket  # websocket-client
        except ImportError:
            if _pip_install("websocket-client"):
                try:
                    import websocket  # noqa: F811
                except ImportError:
                    raise WheelError("缺少 websocket-client 且自动安装失败；"
                                     "请手动执行 pip3 install websocket-client") from None
            else:
                raise WheelError("缺少 websocket-client 且自动安装失败；"
                                 "请手动执行 pip3 install websocket-client") from None
        self.ws = websocket.create_connection(url, timeout=120, suppress_origin=True)
        self._id = 0
        self.events = []

    def send(self, method, params=None, session_id=None):
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self.ws.send(json.dumps(message))
        while True:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self._id:
                if "error" in raw:
                    raise WheelError(f"CDP调用失败：{method}")
                return raw.get("result", {})
            self.events.append(raw)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _run_quiet(command, timeout, env=None):
    try:
        result = subprocess.run(command, timeout=timeout, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


# 国内多渠道镜像（依次回退）
PIP_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://pypi.mirrors.ustc.edu.cn/simple",
    None,  # 官方源兜底
]
ALPINE_MIRRORS = [
    None,  # 容器已配置的源优先
    "https://mirrors.tuna.tsinghua.edu.cn/alpine",
    "https://mirrors.aliyun.com/alpine",
    "https://mirrors.ustc.edu.cn/alpine",
]
DEBIAN_MIRRORS = [
    None,  # 容器已配置的源优先
    "https://mirrors.aliyun.com/debian",
    "https://mirrors.tuna.tsinghua.edu.cn/debian",
]


def _pip_install(package):
    """缺 Python 依赖时自动 pip 安装，多镜像源依次回退。"""
    for index in PIP_MIRRORS:
        command = [sys.executable, "-m", "pip", "install", package,
                   "--quiet", "--disable-pip-version-check", "--retries", "2", "--timeout", "30"]
        if index:
            command += ["-i", index]
        if _run_quiet(command, timeout=600):
            return True
    return False


def _os_release():
    values = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"')
    except Exception:
        pass
    return values


def _install_chromium():
    """未找到 Chromium 时自动安装：Alpine apk / Debian apt，多国内镜像依次回退（需 root）。"""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise WheelError("未找到 Chromium 且当前非 root，无法自动安装；"
                         "请手动安装 chromium 或设 TELECOM_WHEEL_BROWSER 指定路径")
    print("[环境] 未找到 Chromium，尝试自动安装（约1-5分钟，仅首次）...", flush=True)
    found = ""
    if shutil.which("apk"):
        version = _os_release().get("VERSION_ID", "")
        for mirror in ALPINE_MIRRORS:
            label = "容器已配源" if mirror is None else mirror.split("//", 1)[-1].split("/")[0]
            print(f"[环境] apk 安装 chromium（源：{label}）...", flush=True)
            command = ["apk", "add", "--no-cache"]
            if mirror and re.fullmatch(r"\d+\.\d+", version):
                command += ["--repository", f"{mirror}/v{version}/main",
                            "--repository", f"{mirror}/v{version}/community"]
            command.append("chromium")
            if _run_quiet(command, timeout=1200):
                found = shutil.which("chromium") or shutil.which("chromium-browser") or ""
                if found:
                    break
    if not found and shutil.which("apt-get"):
        release = _os_release()
        codename, distro = release.get("VERSION_CODENAME", ""), release.get("ID", "")
        for mirror in DEBIAN_MIRRORS:
            if mirror and (not codename or distro != "debian"):
                continue  # 仅 Debian 换源；Ubuntu 的 chromium 走 snap，换源无意义
            label = "容器已配源" if mirror is None else mirror.split("//", 1)[-1].split("/")[0]
            print(f"[环境] apt 安装 chromium（源：{label}）...", flush=True)
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            if mirror:
                sources = (f"deb {mirror}/ {codename} main\n"
                           f"deb {mirror}-security/ {codename}-security main\n")
                override = ["-o", f"Dir::Etc::sourcelist=/etc/apt/dxhyll-mirror.list",
                            "-o", "Dir::Etc::sourceparts=-"]
                Path("/etc/apt/dxhyll-mirror.list").write_text(sources)
            else:
                override = []
            ok = (_run_quiet(["apt-get", *override, "update"], timeout=900, env=env)
                  and _run_quiet(["apt-get", *override, "install", "-y", "chromium"],
                                 timeout=1800, env=env))
            try:
                Path("/etc/apt/dxhyll-mirror.list").unlink()
            except Exception:
                pass
            if ok:
                found = shutil.which("chromium") or shutil.which("chromium-browser") or ""
                if found:
                    break
    if not found:
        raise WheelError("自动安装 Chromium 失败（已尝试全部镜像）；请手动安装 chromium"
                         "（apk add chromium / apt install chromium），或设 TELECOM_WHEEL_BROWSER 指定路径")
    print("[环境] Chromium 自动安装成功", flush=True)
    return found


class ChromiumDriver:
    """启动一个 headless Chromium；全部账号共用进程，每账号独立 BrowserContext（Cookie 隔离）。"""

    def __init__(self):
        executable = os.environ.get("TELECOM_WHEEL_BROWSER", "").strip()
        if not executable:
            executable = (shutil.which("chromium") or shutil.which("chromium-browser")
                          or shutil.which("google-chrome") or "")
        if not executable:
            executable = _install_chromium()
        self.profile = tempfile.mkdtemp(prefix="dxhyll-chrome-")
        command = [executable, "--headless", "--no-sandbox", "--disable-gpu",
                   "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                   "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
                   "--mute-audio", "--remote-allow-origins=*",
                   "--remote-debugging-port=0",
                   "--user-data-dir=" + self.profile, "about:blank"]
        try:
            self.proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError:
            shutil.rmtree(self.profile, ignore_errors=True)
            raise WheelError("Chromium启动失败；检查路径与权限") from None
        endpoint, lines = None, queue.Queue()

        def pump():
            for raw in iter(self.proc.stderr.readline, b""):
                lines.put(raw)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.time() + 30
        while time.time() < deadline and endpoint is None:
            try:
                text = lines.get(timeout=1).decode("utf-8", "replace")
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            match = re.search(r"ws://\S+", text)
            if match:
                endpoint = match.group(0)
        if endpoint is None:
            self.close()
            raise WheelError("Chromium 未在 30 秒内暴露调试端口；检查 chromium 是否可运行")
        self.cdp = CdpConnection(endpoint)

    def close(self):
        if getattr(self, "cdp", None) is not None:
            self.cdp.close()
            self.cdp = None
        if getattr(self, "proc", None) is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)


class BrowserClient:
    def __init__(self, driver, account, ticket, user_agent):
        self.driver, self.account, self.ticket = driver, account, ticket
        self.environment = "android" if ";Android;" in user_agent else "wap"
        self.context_id = driver.cdp.send("Target.createBrowserContext").get("browserContextId")
        target = driver.cdp.send("Target.createTarget",
                                 {"url": "about:blank", "browserContextId": self.context_id})
        self.session = driver.cdp.send("Target.attachToTarget",
                                       {"targetId": target.get("targetId"), "flatten": True}).get("sessionId")
        driver.cdp.send("Network.setUserAgentOverride", {"userAgent": user_agent}, session_id=self.session)
        parts = account.client_type.split("#")
        self.model = parts[3] if len(parts) > 3 else ""
        driver.cdp.send("Page.addScriptToEvaluateOnNewDocument",
                        {"source": CDP_STEALTH}, session_id=self.session)
        driver.cdp.send("Page.addScriptToEvaluateOnNewDocument",
                        {"source": bridge_script(self.model)}, session_id=self.session)

    def _evaluate(self, expression, await_promise=False):
        result = self.driver.cdp.send("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise,
            "returnByValue": True, "timeout": 45000}, session_id=self.session)
        if result.get("exceptionDetails"):
            raise WheelError("页内脚本执行异常")
        value = result.get("result", {})
        return value.get("value") if isinstance(value, dict) else None

    def open(self):
        # 瑞数壳页会 document.open/write 重写文档；window 属性跨重写存活，
        # 因此导航后立刻高频注入桥桩（幂等），赶在 app bundle 执行前生效。
        ready = "Boolean(window.config && window.config.BASE_API && document.querySelector('#app'))"
        inject = bridge_script(self.model) + ";" + ready
        try:
            self.driver.cdp.send("Page.navigate", {"url": activity_url(self.ticket)}, session_id=self.session)
        except Exception:
            raise WheelError("活动页导航失败；检查 Chromium、网络及站点防护，未发起抽奖") from None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self._evaluate(inject) is True:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise WheelError("活动页未加载完成；检查 Chromium、网络及站点防护，未发起抽奖")
        self.validate()

    def call(self, path, params=None):
        allowed = {"/spm/restful", "/component/getUserPhone", "/component/queryType",
                   "/lotteryActivity/lottery", "/lotteryActivity/getGoodsByLotteryActId"}
        if path not in allowed:
            raise WheelError("业务路径不在当前活动白名单")
        payload = {"path": path, "params": params or {}, "ticket": self.ticket,
                   "activityCode": ACTIVITY_CODE, "environment": self.environment}
        expression = "(" + XHR + ")(" + json.dumps(payload, ensure_ascii=False) + ")"
        try:
            value = self._evaluate(expression, await_promise=True)
        except Exception:
            raise WheelError("页内请求中断，结果未知") from None
        if not isinstance(value, dict):
            raise WheelError("页内响应结构异常")
        status = value.get("status")
        if status != 200:
            shown = status if isinstance(status, int) else "未知"
            raise WheelError(f"活动接口HTTP {shown}；请求未重试")
        body = value.get("data")
        if not isinstance(body, dict):
            raise WheelError("活动响应不是JSON；可能为防护页面")
        if str(body.get("code")) != "1":
            raise BusinessError("业务未成功：" + clean(body.get("msg", "未知原因"), self.account))
        return body

    def validate(self):
        body = self.call("/spm/restful", {"method": "setRedisPagesJson", "activityCode": ACTIVITY_CODE})
        if str(body.get("isLogin")).lower() != "true":
            raise WheelError("新票据未建立活动登录态；停止该账号，未抽奖")
        if body.get("activityId") != ACTIVITY_ID:
            raise WheelError("活动ID已变化；停止该账号，未抽奖")
        phone = self.call("/component/getUserPhone").get("data")
        value = str(phone or "")
        if not (len(value) == 11 and value.count("*") <= 4
                and all(a == "*" or a == b for a, b in zip(value, self.account.phone))):
            raise WheelError("活动登录号码无法匹配当前账号；未抽奖")

    def remaining(self):
        return integer(self.call("/spm/restful", {"activityId": ACTIVITY_ID,
                       "method": "prizeRemainingTimes"}).get("data"), "剩余次数", DAILY_LIMIT)

    def balance(self):
        return integer(self.call("/component/queryType", {"queryType": "goldenBeans"}).get("data"), "金豆余额")

    def draw(self):
        body = self.call("/lotteryActivity/lottery", {"activityId": ACTIVITY_ID, "source": "JinDouMall"})
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("goodsName"), str) or not data["goodsName"].strip():
            raise WheelError("抽奖响应缺少奖品名称，结果待核验")
        return data["goodsName"]

    def close(self):
        try:
            self.driver.cdp.send("Target.disposeBrowserContext",
                                 {"browserContextId": self.context_id})
        except Exception:
            pass


# ---------------------------------------------------------------- 主流程
def run_draws(client, state, log, query_only=False, day_fn=today, sleep=time.sleep):
    remaining, balance = client.remaining(), client.balance()
    log(f"服务端剩余 {remaining} 次，金豆 {balance}；本地当日已提交 {state.used}/3 次")
    if query_only:
        log("只读查询完成：未抽奖、未扣金豆")
        return True
    if state.blocked:
        log("当日已有结果未知/失败记录，停止补抽；请在活动奖品记录核对，次日自动重新计数")
        return False
    while state.used < DAILY_LIMIT and remaining > 0:
        if day_fn() != state.day:
            log("已跨北京时间日期，结束本次任务")
            return True
        if balance < DRAW_COST:
            log("金豆不足10，停止抽奖")
            return True
        state.reserve()
        try:
            prize = client.draw()
        except BusinessError as error:
            state.finish("rejected")
            log(str(error) + "；本日不自动重试")
            return False
        except Exception:
            state.finish("unknown")
            log("抽奖结果未知，已记录并停抽；不重发请求，奖品请在活动页面核对")
            return False
        state.finish("success", prize)
        log(f"第 {state.used}/3 次抽奖：{prize}")
        if state.used >= DAILY_LIMIT:
            break
        sleep(3)
        if day_fn() != state.day:
            log("已跨北京时间日期，结束本次任务")
            return True
        current = client.remaining()
        if current >= remaining:
            state.data["halt"] = "remaining_not_decreased"
            state.save()
            log("奖品已返回但剩余次数未减少，暂停后续抽奖；不根据猜测继续扣豆")
            return False
        remaining, balance = current, client.balance()
    if remaining == 0:
        log("今日服务端次数已用完")
    log(f"本次任务完成，当日本地累计提交 {state.used}/3 次")
    return True


class LoginGate:
    """密码登录失败熔断：3001（电信侧判定密码错误/风控伪装）当日不再重试，

    避免失败计数累积导致账号锁定；次日自动恢复。缓存 token 换票不受影响。
    """

    def __init__(self, directory):
        self.root = Path(directory)

    def _path(self, account):
        digest = hashlib.sha256(account.phone.encode()).hexdigest()[:16]
        return self.root / ("login_halt_" + digest + ".json")

    def blocked(self, account, day):
        try:
            return json.loads(self._path(account).read_text("utf-8")).get("day") == day
        except Exception:
            return False

    def block(self, account, day):
        atomic_json(self._path(account), {"day": day})


def load_notify():
    """加载青龙内置 notify.py（task 运行时必然在脚本目录可用）。"""
    import importlib.util
    for path in ("/ql/data/scripts/notify.py", "/ql/scripts/notify.py",
                 str(Path(__file__).resolve().parent / "notify.py")):
        try:
            if not os.path.exists(path):
                continue
            spec = importlib.util.spec_from_file_location("ql_notify", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                func = getattr(module, "send", None)
                if callable(func):
                    return func
        except Exception:
            continue
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="电信好运连连：每日最多3次，每次10金豆")
    parser.add_argument("--query-only", action="store_true", help="只登录/查询，不抽奖、不扣金豆")
    parser.add_argument("--check-browser", action="store_true", help="仅检查Chromium可启动，不联网/不读取账号")
    args = parser.parse_args(argv)
    log = Log()
    driver = None
    try:
        if not args.check_browser and not START_DAY <= today() <= END_DAY:
            log(f"活动期为 {START_DAY} 至 {END_DAY}，当前已在范围外；未登录/抽奖")
            return 0
        accounts = [] if args.check_browser else parse_accounts(os.environ.get("TELEFEN_AUTH", ""))
        driver = ChromiumDriver()
        if args.check_browser:
            context = driver.cdp.send("Target.createBrowserContext").get("browserContextId")
            driver.cdp.send("Target.disposeBrowserContext", {"browserContextId": context})
            log("Chromium启动成功（未联网、未读取账号）")
            return 0
        directory = os.environ.get("TELECOM_WHEEL_STATE_DIR") or str(Path(__file__).resolve().parent / ".telecom_wheel_state")
        failures = 0
        results = []
        with state_lock(directory) as root:
            cache = TokenCache(root)
            gate = LoginGate(root)
            log("开始只读查询" if args.query_only else "每账号每天最多3次，每次10金豆，最多消耗30金豆；不会自动兑换奖品")
            for account in accounts:
                account_log, client = Log(account), None
                try:
                    state = DailyState(root, account, today())
                    if not args.query_only and (state.used >= DAILY_LIMIT or state.blocked):
                        account_log("当日已达3次或存在待核验记录；跳过登录及抽奖")
                        failures += int(state.blocked)
                        continue
                    if gate.blocked(account, state.day):
                        account_log("当日密码登录已熔断（此前3001）；跳过，次日自动恢复")
                        results.append("⛔ " + account.label + "：当日登录熔断（3001），次日恢复")
                        failures += 1
                        continue
                    try:
                        ticket, user_agent = obtain_ticket(account, cache)
                    except AuthenticationError as error:
                        if error.stage == "登录" and error.code == "3001":
                            gate.block(account, state.day)
                            account_log(str(error) + "；已熔断当日重试，次日恢复")
                        raise
                    client = BrowserClient(driver, account, ticket, user_agent)
                    client.open()
                    account_log("活动登录及号码核对通过")
                    failures += not run_draws(client, state, account_log, args.query_only)
                    prizes = [a.get("prize", "") for a in state.data["attempts"]
                              if a.get("status") == "success" and a.get("prize")]
                    line = "✅ " + account.label + "：" + ("、".join(prizes) if prizes else "无新抽奖")
                    if state.data.get("halt"):
                        line += "（早停：" + str(state.data["halt"]) + "）"
                    results.append(line)
                except Exception as error:
                    # Never print library exceptions: they may contain URLs/XML/credentials.
                    if isinstance(error, (WheelError, AuthenticationError)):
                        account_log(str(error))
                        message = str(error)
                    else:
                        account_log("账号处理失败（" + type(error).__name__ + "），未自动重试")
                        message = "处理失败（" + type(error).__name__ + "）"
                    results.append("⚠️ " + account.label + "：" + clean(message, account))
                    failures += 1
                finally:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            pass
        log(f"结束：{len(accounts) - failures} 个账号正常，{failures} 个账号需查看日志")
        if not args.query_only and results:
            sender = load_notify()
            if sender is not None:
                try:
                    sender("电信好运连连 " + today(), "\n".join(results))
                    log("汇总通知已发送")
                except Exception:
                    log("通知发送失败（不影响任务结果）")
        return 1 if failures else 0
    except Exception as error:
        log(str(error) if isinstance(error, (WheelError, AuthenticationError)) else "运行失败（" + type(error).__name__ + "）")
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    sys.exit(main())
