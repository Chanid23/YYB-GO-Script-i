#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Author: Alexin

电信AI奇遇（青龙/面板单文件）
cron: 0 */6 * * *
new Env('电信AI奇遇');


环境变量：
必填 TELEFEN_AUTH：备注#手机号#服务密码#clientType#androidId
获取地址：https://dx.lcmovie.qzz.io/

【使用方法】
1. 青龙“依赖管理 → Python3”安装：requests、pycryptodome、certifi（建议 Python 3.10+）。
2. TELEFEN_AUTH 与电信光耀网龄脚本相同；多账号用 & 或换行分隔，clientType 保留双 #，
   androidId 使用网站导出的编码值。
3. 定时任务建议每6小时：0 */6 * * *；默认执行参与并推送通知。
4. QL_EXECUTE=0 或 --dry-run 只查询；QL_NOTIFY=0 显式关闭通知。
5. 更新时用完整文件替换旧脚本，保留状态目录，不要两版同时跑同一批账号。

【脚本功能】
1. AI奇遇活动
    - 两种模式（IMUSIC_MODE 可选，默认第一种）：
      ① 只攒分（save，默认）：只攒分不抽奖，满1000立即兑换10元话费；兑换成功或明确
        缺货(10003)的当月状态跨重启保存（本月不再重复请求兑换，次月自动重试）；
      ② 极速满额（auto）：每期用免费制作次数做满340点；临近本期结束（最后3天）免费
        额度用光且未满340点时，抽奖只为补AI体验券（中奖率约43%）凑满17件，抽中即制作回
        20点、别多抽；余额满1000点立即兑换10元话费，兑换成功后自动转为全抽奖，之后所有
        点数每轮抽光（约3期45天兑到第一个10元）；明确缺货(10003)当月转抽奖、次月再试兑换；
      ③ 全抽奖（draw）：不攒兑换，每轮把可抽余额全部投入抽奖（默认不保留1000点）；
      辅助：observe=只查询不参与；auto模式的当月状态跨重启保存；
    - 期末防清零（各模式均生效）：最后一期（2027-01-12开奖后点数清零）攒不满1000点时
      自动把余额全部投入抽奖，避免开奖后清零；
   - 中奖话费自动领取；活动券码由服务端定期开奖，脚本只提醒；
   - 彩铃自动设置：检测我的作品，把已生成未设置的作品自动设为视频彩铃（每次+20点，
     每期上限100点即5个，已设置作品跳过；未开通视频彩铃的账号由服务端拒绝，自动跳过）；
   - 不自动订购付费业务、不代填收货地址。
2. 安全设计：未知请求结果写入对账日志、跨重启保护、不重复提交；账号逐一隔离处理，
   一个账号失败不影响其他账号；日志自动脱敏手机号。

"""
try:
    import requests, certifi
    from Crypto.Cipher import AES
except ImportError:
    print('缺少Python依赖：请在青龙依赖管理安装 requests、pycryptodome、certifi')

try:
    import sys as _stdout_sys
    if hasattr(_stdout_sys.stdout, "reconfigure"):
        _stdout_sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _stdout_sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
    raise SystemExit(1)

# ===== 活动请求协议 =====
"""Account-isolated activity client. Credentials stay in memory; no network on import."""
import base64
import hashlib
import json
import os
import re
import secrets
import string
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urlencode, parse_qsl, urlunsplit

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

DEFAULT_CHANNEL = "156000009079"
BASE_URL = "https://ai.imusic.cn"


class ImusicError(RuntimeError):
    pass


_IMUSIC_SAFE_APIS = frozenset({
    "act/LaborApi/getOperationCurrentIssueInfo",
    "act/LaborApi/getOperationTotalScoreOrRemainingScore",
    "act/LaborApi/getOperationMyTickets",
    "act/LaborApi/getOperationPrizeRecordList",
    "act/LaborApi/getOperationRecordList",
    "act/LaborApi/extractPhoneBill",
    "act/LaborApi/operationIntegralRedeemPrize",
    "act/LaborApi/operationIntegralLottery",
    "ismp/IsmpApi/queryAiMakePkgInfo",
    "diy/DiyVideoApi/querySingleDiyResult",
    "diy/DiyVideoApi/queryActRecommendTemplateList",
    "diy/DiyVideoApi/queryFailOrCreativeDiy",
    "diy/DiyVideoApi/getEncryptDecryptMobile",
    "/hapi/diy_ugc/au/get_ugc_list",
    "/hapi/diy_ugc/au/order_or_update",
    "/hapi/diy_video/au/template_make_add_v2",
    "/hapi/diy_video/imu/query_template_info",
    "/vapi/vue_stat/sendMessage",
    "/vapi/vue_login/sso_login_v2",
    "/vapi/new_member/get_user_info",
})


class ImusicApiError(ImusicError):
    """An API failure with safe, structured fields, never a raw response body.

    The endpoint is selected from known code-owned routes. Server descriptions
    are deliberately excluded: even a short message may contain a session token.
    A business error alone does not prove a submitted mutation was not accepted.
    """
    def __init__(self, code, api=""):
        value = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ""
        self.code = value if re.fullmatch(r"[0-9]{1,8}", value) else "unknown"
        self.api = api if isinstance(api, str) and api in _IMUSIC_SAFE_APIS else "未知接口"
        if self.code == "0007":
            self.description = "活动登录已失效，请检查该账号登录状态"
        elif self.code == "10003" and self.api == "act/LaborApi/operationIntegralRedeemPrize":
            self.description = "兑换库存不足"
        elif self.code == "10013":
            self.description = "服务端返回业务错误，尚不能确认是否受理"
        else:
            self.description = "服务端未返回成功结果"
        super().__init__(f"活动接口未成功：{self.api}；返回码 {self.code}；{self.description}")


def _md(value):
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _bearer(token):
    token = str(token or "").strip()
    return token if not token or token.lower().startswith("bearer ") else "Bearer " + token


def encryption_context(timestamp=None, random_value=None):
    """Optional fixed inputs support offline verification against captured traffic."""
    timestamp = str(timestamp if timestamp is not None else time.time_ns() // 1000000)
    random_value = random_value or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    key = _md(_b64(_md(timestamp) + random_value) + random_value)
    return {"imencrypt": "1", "imtimestamp": timestamp,
            "imrandomnum": random_value, "imencryptkey": key}


def _cipher(headers, response=False):
    t, r, k = (headers[x] for x in ("imtimestamp", "imrandomnum", "imencryptkey"))
    if response:
        key, iv = _md(_b64(t) + k + _md(r)), _md(_b64(r) + k + _md(t))
    else:
        key, iv = _md(_b64(r) + _md(t) + k), _md(_b64(t) + _md(r) + k)
    return AES.new(key[:16].encode("ascii"), AES.MODE_CBC, iv[:16].encode("ascii"))


def encrypt_payload(payload, headers):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(_cipher(headers).encrypt(pad(raw, 16))).decode("ascii")


def decrypt_payload(payload, headers, response=True):
    raw = base64.b64decode(payload, validate=True)
    return json.loads(unpad(_cipher(headers, response).decrypt(raw), 16).decode("utf-8"))


def _parse_cookie(cookie):
    if isinstance(cookie, dict):
        return {str(k): str(v) for k, v in cookie.items()}
    return dict(part.strip().split("=", 1) for part in str(cookie or "").split(";") if "=" in part)

class ImusicClient:
    def __init__(self, phone, token="", cookie="", channel_id=DEFAULT_CHANNEL,
                 state_dir=None, use_token_cache=True):
        self.phone = str(phone)
        self.channel_id = str(channel_id)
        self.state_dir = Path(state_dir).expanduser() if state_dir else None
        self.ticket_provider = None
        cached_token = self._load_activity_token() if use_token_cache else ""
        self.token = _bearer(token or cached_token)
        # The official page reuses one context for the life of its session.
        self.encryption_headers = encryption_context()
        self.activity_sid = ""
        self.invite_code = ""
        self.invite_entry_done = False
        self.session = requests.Session()
        # Explicit opt-in for environment proxies; local unrelated proxy settings
        # must not make an otherwise reachable activity appear unavailable.
        self.session.trust_env = os.getenv("IMUSIC_TRUST_ENV", "0") == "1"
        app_user_agent = ("CtClient;13.3.0;Android;14;V2203A;" + _b64(self.phone[-6:]).rstrip("=")
                          + "!#!" + _b64(self.phone[:5]).rstrip("="))
        self.session.headers.update({"User-Agent": app_user_agent,
                                     "Accept": "application/json, text/plain, */*", "Origin": BASE_URL,
                                     "X-Requested-With": "com.ct.client",
                                     "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                                     "Referer": BASE_URL + "/h5v/fusion/ai-luck-winnew?" + urlencode({"cc": self.channel_id, "ca": "w7x6"})})
        for name, value in _parse_cookie(cookie).items():
            self.session.cookies.set(name, value, domain="ai.imusic.cn", path="/")
        self.identity_verified = False

    def close(self):
        self.session.close()

    def _activity_token_path(self):
        if not self.state_dir:
            return None
        name = hashlib.sha256(self.phone.encode("utf-8")).hexdigest()[:16] + ".token.json"
        return Path(self.state_dir) / name

    def _load_activity_token(self):
        path = self._activity_token_path()
        if not path:
            return ""
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
                return ""
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            token = data.get("token") if isinstance(data, dict) else ""
            if not isinstance(token, str) or not 16 <= len(token.strip()) <= 4096:
                return ""
            return token.strip()
        except (OSError, ValueError, TypeError, UnicodeError):
            return ""

    def _save_activity_token(self, token):
        token = _bearer(token)
        if not token or token == self.token:
            return
        self.token = token
        path = self._activity_token_path()
        if not path:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = {"version": 1, "updated": datetime.now(CST).isoformat(), "token": token}
            temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
            temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                 encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError:
            # A cache write failure must not invalidate a live in-memory session.
            pass
        finally:
            try:
                if temporary and temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def open_activity_page(self):
        """Obtain the activity's own current tracking session via its normal entry."""
        if not self.identity_verified:
            raise ImusicError("账号身份未经核对")
        if self.activity_sid:
            return
        self.session.cookies.set("cc", self.channel_id, domain="ai.imusic.cn", path="/")
        target = BASE_URL + "/ca/w7x6"
        for _ in range(3):
            parsed = urlsplit(target)
            if (parsed.scheme != "https" or parsed.netloc != "ai.imusic.cn" or
                    parsed.path not in {"/ca/w7x6", "/h5v/fusion/ai-luck-winnew"}):
                raise ImusicError("活动入口跳转地址异常，停止制作")
            if self.invite_code and parsed.path == "/h5v/fusion/ai-luck-winnew":
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                query.update({"isshare": "0", "invitationCode": self.invite_code})
                target = urlunsplit(parsed._replace(query=urlencode(query)))
            try:
                response = self.session.get(target, timeout=30, allow_redirects=False)
            except requests.RequestException:
                raise ImusicError("活动页面连接失败，尚未提交制作") from None
            if response.status_code in {301, 302, 303, 307, 308}:
                target = urljoin(target, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise ImusicError("活动页面加载失败，HTTP " + str(response.status_code))
            # Do not put redirect query strings in subsequent headers or logs.
            self.activity_sid = next((c.value for c in self.session.cookies
                                      if c.name == "imusic" and c.domain.lstrip(".") == "ai.imusic.cn"), "")
            if not self.activity_sid:
                raise ImusicError("活动页面未返回会话标识，尚未提交制作")
            return
        raise ImusicError("活动页面跳转次数异常，尚未提交制作")

    def activity_event(self, name, value):
        if not self.identity_verified or not self.activity_sid:
            raise ImusicError("活动页面尚未准备完成")
        return self._post("/vapi/vue_stat/sendMessage", self._params({
            "invitationCode": self.invite_code or "", "actname": name, "actparam": value + "_ca_w7x6",
            "sid": self.activity_sid}), True, True)

    def _params(self, params):
        data = {"channelId": self.channel_id, "portal": "45", "mobile": self.phone}
        data.update(params or {})
        if str(data.get("mobile", self.phone)) != self.phone:
            raise ImusicError("拒绝使用其他账号的手机号")
        return data

    def _post(self, path, payload, encrypted, encrypted_response, plain_query=False):
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ImusicError("无效的活动接口路径")
        headers = {} if encrypted or plain_query else {"Content-Type": "application/json;charset=UTF-8"}
        if self.token:
            headers["Authorization"] = self.token
        kwargs = {}
        if encrypted:
            headers.update(self.encryption_headers)
            kwargs["params"] = {"formData": encrypt_payload(payload, headers)}
        elif plain_query:
            kwargs["params"] = payload
        else:
            kwargs["json"] = payload
        try:
            response = self.session.post(BASE_URL + path, headers=headers, timeout=30,
                                         allow_redirects=False, **kwargs)
        except requests.RequestException:
            # requests exception strings can include credential-bearing query strings.
            raise ImusicError("活动请求连接失败或超时") from None
        if response.status_code != 200:
            raise ImusicError("活动请求失败，HTTP " + str(response.status_code))
        if response.headers.get("authorization"):
            self._save_activity_token(response.headers["authorization"])
        try:
            data = response.json()
        except ValueError:
            data = response.text.strip()
        if isinstance(data, str) and encrypted_response:
            try:
                data = decrypt_payload(data, headers)
            except (ValueError, KeyError, UnicodeError):
                raise ImusicError("活动响应解密失败") from None
        if not isinstance(data, dict):
            raise ImusicError("活动响应格式异常")
        if str(data.get("code", data.get("returnCode", ""))) == "0007":
            self.identity_verified = False
            raise ImusicApiError("0007", payload.get("apiName", path))
        return data

    def api(self, api_name, params=None, encrypted_response=True):
        payload = self._params(params)
        payload["apiName"] = api_name
        return self._post("/hapi/en/api" if encrypted_response else "/hapi/de/api",
                          payload, encrypted_response, encrypted_response,
                          plain_query=not encrypted_response)

    def direct(self, path, params=None, encrypted=True, encrypted_response=None):
        if encrypted_response is None:
            encrypted_response = encrypted
        return self._post(path, self._params(params), encrypted, encrypted_response,
                          plain_query=not encrypted)

    def get_user_info(self):
        # This endpoint uses plaintext query parameters in all captured exchanges.
        return self._post("/vapi/new_member/get_user_info", self._params({}),
                          False, False, plain_query=True)

    def decrypt_invite_code(self, code):
        """Official entry step: decode the opaque entry token.

        The activity page passes the opaque token as `mobile` for this
        one endpoint; it is never another account's login name.
        Response data is "<decoded id>|..." per the captured official flow.
        """
        code = str(code or "").strip()
        if not code:
            raise ImusicError("入口参数为空，无法执行官方入口解密步骤")
        payload = {"activityId": "ai119", "mobile": code, "type": 2,
                   "invitationCode": code, "apiName": "diy/DiyVideoApi/getEncryptDecryptMobile",
                   "channelId": self.channel_id, "portal": "45"}
        return self._post("/hapi/en/api", payload, True, True)

    def mark_invite_entry(self):
        """Official entry steps, once per client: decode token, send beacon.

        Mirrors the page's mounted() flow. Failures are ignored exactly like
        the official page's catch(){} so these extras can never block
        participation. Returns True when the beacon was sent.
        """
        if not self.invite_code or self.invite_entry_done:
            return True
        self.invite_entry_done = True
        try:
            info = self.decrypt_invite_code(self.invite_code)
            # 官方页面判定：t && "0000"===t.code && t.data 才继续发送埋点。
            if str(info.get("code")) != "0000" or not info.get("data"):
                return False
            inviter = str(info.get("data")).split("|")[0]
            if not inviter:
                return False
            self.activity_event("activity_vring_sharinglink_h5_20260128",
                                "activityID_ai119_templateID_null_vidioID_null_分享人手机号_" + inviter
                                + "_接收人手机号_" + self.phone
                                + "_templateconfID_null_entrance_" + self.channel_id)
            return True
        except (ImusicError, ImusicApiError):
            # 官方页面对该步骤的失败静默忽略；不影响后续参与。
            return False

    def refresh_login(self, ticket=None):
        """Validate identity via cached activity token, then a fresh telecom ticket."""
        payload = {"portal": "45", "channelId": self.channel_id}
        payload.update({"ticket": ticket} if ticket else {"user118100cn": "user118100cn"})
        self.identity_verified = False
        try:
            data = self._post("/vapi/vue_login/sso_login_v2", payload, False, False)
        except ImusicApiError as error:
            if error.code != "0007" or ticket or not callable(self.ticket_provider):
                raise
            ql_log(f"活动Token失效，{mask(self.phone)} 自动重新获取电信Ticket")
            self.token = ""
            fresh_ticket = self.ticket_provider()
            payload = {"portal": "45", "channelId": self.channel_id, "ticket": fresh_ticket}
            data = self._post("/vapi/vue_login/sso_login_v2", payload, False, False)
        if str(data.get("returnCode")) != "0000" or not data.get("token"):
            raise ImusicError("活动登录刷新失败，请检查该账号登录状态")
        if str(data.get("mobile", "")) != self.phone:
            self.token = ""
            self.session.cookies.clear()
            raise ImusicError("登录会话手机号与当前账号不一致，已拒绝继续")
        self._save_activity_token(data["token"])
        self.identity_verified = True
        return data

# ===== AI奇遇活动 =====
"""AI奇遇活动（Author: Alexin）。两种模式：极速满额（auto，默认）——每期做满340点、
期末抽奖只补AI体验券凑满17件、满1000立即兑10元话费、兑后自动转全抽奖；全抽奖（draw）——
全部积分投抽奖。默认只查询；--execute 才制作、兑换或领奖。"""
import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent
ACTIVITY = 'ai119'
CST = timezone(timedelta(hours=8))
PHONE_AWARDS = {'16320105', '16320205', '16320305'}
REDEEM_AWARD = '16330101'
MUTATION_APIS = {'make': '/hapi/diy_video/au/template_make_add_v2', 'claim': 'act/LaborApi/extractPhoneBill', 'redeem': 'act/LaborApi/operationIntegralRedeemPrize', 'draw': 'act/LaborApi/operationIntegralLottery'}
FINAL_DRAW_DATE = datetime(2027, 1, 12, tzinfo=CST)
TOPUP_WINDOW_DAYS = 3
DEFAULT_INVITE_CODE = '936da1ff1561097192ab33e71d5c61937729b5b7eb2c111f7856d33bb552acce'

def log(message):
    print(f'[{datetime.now(CST):%m-%d %H:%M:%S}] {message}', flush=True)
_MAKE_GATE = {'last': 0.0, 'count': 0}
MAKE_BATCH_LIMIT = 8

def pace_make_submission(interval):
    """提交前保证与上一次制作提交至少间隔 interval 秒；interval=0 时不等待。

    同IP连续提交每满8次后主动冷却2~3分钟：实测20秒/次的节奏连续10次会触发
    10007同IP频控，主动冷却把任意滑动窗口内的提交密度压到阈值之下。
    """
    if interval > 0:
        wait = _MAKE_GATE['last'] + interval - time.time()
        if wait > 0:
            time.sleep(wait)
    _MAKE_GATE['last'] = time.time()
    _MAKE_GATE['count'] += 1
    if _MAKE_GATE['count'] >= MAKE_BATCH_LIMIT:
        _MAKE_GATE['count'] = 0
        cooldown = 120 + secrets.randbelow(61)
        log(f'同IP已连续提交制作{MAKE_BATCH_LIMIT}次，主动冷却 {cooldown} 秒预防10007频控')
        time.sleep(cooldown)
        _MAKE_GATE['last'] = time.time()

def number(value):
    if isinstance(value, bool):
        raise ValueError('布尔值不是次数')
    return int(str(value))

def date(value):
    if not isinstance(value, str):
        raise ImusicError('服务端日期格式未知，停止制作')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ImusicError('服务端日期格式未知，停止制作') from None
    return result.replace(tzinfo=CST) if result.tzinfo is None else result.astimezone(CST)

def checked(response, api=''):
    if not isinstance(response, dict) or str(response.get('code')) != '0000':
        raise ImusicApiError(response.get('code') if isinstance(response, dict) else None, api)
    return response.get('data')

def creation_points(records, issue):
    """Count current-period production credits, never activity-wide totalScore.

    Descriptions are server supplied. Unknown incoming descriptions fail closed;
    no unverified numeric scoreType mapping is used.
    """
    start, end = (date(issue['startTime']), date(issue['endTime']))
    total = 0
    for item in records:
        when = date(item.get('createDate'))
        if not start <= when <= end:
            continue
        amount = number(item.get('score'))
        if amount <= 0 or str(item.get('scoreType')) in {'-1', '-2'}:
            continue
        desc = str(item.get('scoreDesc', ''))
        production = '制作' in desc or '创作' in desc
        other = any((word in desc for word in ('邀请', '分享', '彩铃', '抽奖', '中奖', '兑换', '赠送', '设置')))
        if production and other:
            raise ImusicError('积分描述同时涉及制作和其他奖励，无法准确分类；已停止制作')
        if production:
            total += amount
        elif other:
            continue
        else:
            raise ImusicError('积分记录中有未识别的收入描述，无法确认本期制作上限；已停止制作')
    return total

@contextmanager
def account_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ImusicError('这个账号已有任务运行，本次退出') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

class Activity:

    def __init__(self, client, options, state_path):
        self.client, self.options, self.state_path = (client, copy.copy(options), Path(state_path))
        if getattr(self.options, 'reserve_points', None) is None:
            self.options.reserve_points = 0 if self.options.mode in ('auto', 'draw') else 1000
        if self.options.mode == 'observe':
            self.options.execute = False
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
        self.state.setdefault('jobs', [])
        self.state.setdefault('claims', [])
        self._redeem_attempted_this_run = False
        self._draws_this_run = 0
        self._topup_draws = 0
        self._draw_budget_remaining = None
        self._monthly_history_checked = set()
        self._issue = None
        self._final_issue = False

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.state_path)

    def api(self, name, **params):
        api = 'act/LaborApi/' + name
        return checked(self.client.api(api, {'activityId': ACTIVITY, **params}), api)

    def records(self, name):
        rows, seen_pages = ([], set())
        for page in range(1, 101):
            data = self.api(name, pageNo=page, pageSize=10)
            if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                raise ImusicError('记录列表结构未知，停止该操作')
            batch = data['list']
            marker = json.dumps(batch, sort_keys=True, ensure_ascii=False)
            if batch and marker in seen_pages:
                raise ImusicError('服务端重复返回同一页，不能确认完整记录')
            seen_pages.add(marker)
            rows.extend(batch)
            if 'totalCount' in data and len(rows) >= number(data['totalCount']):
                return rows
            if 'totalPage' in data and page >= number(data['totalPage']):
                return rows
            if 'total' in data and len(rows) >= number(data['total']):
                return rows
            if 'pages' in data and page >= number(data['pages']):
                return rows
            if len(batch) < 10:
                return rows
        raise ImusicError('记录超过分页上限，未能完整核对')

    def score(self):
        data = self.api('getOperationTotalScoreOrRemainingScore')
        return (number(data['totalScore']), number(data['remainingScore']))

    def quota(self):
        api = 'ismp/IsmpApi/queryAiMakePkgInfo'
        return checked(self.client.api(api, {'aid': ACTIVITY}), api)

    def mutate(self, kind, call, details=None):
        if not self.options.execute:
            raise ImusicError('查询模式不能改变活动状态')
        if not self.client.identity_verified:
            raise ImusicError('账号身份未经核对')
        if self.state.get('uncertain'):
            raise ImusicError('上次请求结果未确认，请先在活动页面核对记录')
        self.state['uncertain'] = {'kind': kind, 'time': datetime.now(CST).isoformat(), **(details or {})}
        self.save()
        result = call()
        data = checked(result, MUTATION_APIS.get(kind, ''))
        return data

    def committed(self):
        self.state.pop('uncertain', None)
        self.save()

    def notify_tickets(self):
        rows = self.records('getOperationMyTickets')
        log(f'查询到券码 {len(rows)} 个（包含服务端返回的历史期）')
        for row in rows:
            if str(row.get('useStatus')) == '1':
                log('发现中奖券码！请在活动“我的券码”核对并填写收货信息')

    def ring_set_points_used(self, issue):
        """本期已通过设置彩铃获得的积分（服务端规则：每次20点、每期上限100点）。"""
        start, end = (date(issue['startTime']), date(issue['endTime']))
        used = 0
        for item in self.records('getOperationRecordList'):
            if not start <= date(item.get('createDate')) <= end:
                continue
            if number(item.get('score')) > 0 and '设置' in str(item.get('scoreDesc', '')):
                used += number(item.get('score'))
        return used

    def settable_videos(self, want):
        """翻全部页收集可设置作品：未设为彩铃（ringState!=1），审核通过的优先。

        排序：审核通过（audit_state=1）在前、审核中的在后，同级内从最早作品开始；
        作品列表按新→老分页，必须翻完才能确定最早的N个，故不做页内早停。
        state仅反映是否设置过（1=从未设置，2=设置中/已设置），不参与排序；
        已设彩铃（ringState=1）与设置中（state=2，几分钟后自动变为已设置）均跳过。
        """
        audited, pending, seen, page = [], [], set(), 1
        while page <= 20:
            data = checked(self.client.direct('/hapi/diy_ugc/au/get_ugc_list',
                           {'aid': ACTIVITY, 'pageNo': page, 'pageSize': 10}, False, False),
                           '/hapi/diy_ugc/au/get_ugc_list')
            listing = data.get('data') if isinstance(data, dict) else None
            if not isinstance(listing, dict) or not isinstance(listing.get('list'), list):
                raise ImusicError('作品列表结构未知，本轮跳过彩铃设置')
            batch = listing.get('list') or []
            for item in batch:
                video_id = str(item.get('video_id', ''))
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                if number(item.get('ringState')) == 1 or number(item.get('state')) == 2:
                    continue
                (audited if number(item.get('audit_state')) == 1 else pending).append(item)
            if not batch or page >= max(1, number(listing.get('total_page'))):
                break
            page += 1
        oldest = lambda item: str(item.get('create_time', ''))
        return (sorted(audited, key=oldest) + sorted(pending, key=oldest))[:want]

    def order_ringtone(self, video):
        """把已生成作品设为视频彩铃；载荷与官方一致（state为目标状态码2，写死）。"""
        payload = {'videoId': str(video.get('video_id', '')), 'name': str(video.get('name', '')),
                   'validCode': '', 'aid': ACTIVITY, 'column': '',
                   'other_diy': number(video.get('other_diy')) or 1,
                   'state': 2,
                   'sendMsgType': 'act_order', 'isOpenResonance': 0, 'isCheckAuto': 0}
        data = checked(self.client.direct('/hapi/diy_ugc/au/order_or_update', payload),
                       '/hapi/diy_ugc/au/order_or_update')
        return isinstance(data, dict) and str(data.get('code')) == '0' and data.get('success') is True

    def handle_ringtones(self, issue):
        """每期最多设置5个未设置作品为彩铃：每次+20点、每期上限100点；已设置作品跳过。"""
        if not self.options.execute:
            return
        try:
            used = self.ring_set_points_used(issue)
        except ImusicError:
            log('积分记录暂不可读，本轮跳过彩铃设置')
            return
        slots = (100 - used) // 20
        if slots <= 0:
            log('本期彩铃设置积分已满100点，无需再设置')
            return
        try:
            candidates = self.settable_videos(slots)
        except ImusicError:
            log('作品列表暂不可读，本轮跳过彩铃设置')
            return
        if not candidates:
            log('作品均已设置为彩铃，无需操作')
            return
        planned = candidates[:slots]
        audited_n = sum(1 for v in planned if number(v.get('audit_state')) == 1)
        extra = f'，其中审核通过 {audited_n} 个' + (f'、审核中 {len(planned) - audited_n} 个' if len(planned) > audited_n else '')
        log(f'检测到 {len(candidates)} 个未设置作品；本期设置积分还剩 {slots * 20} 点，本轮设置 {len(planned)} 个（审核通过优先、从最早开始{extra}）')
        done = 0
        for video in planned:
            try:
                if self.order_ringtone(video):
                    done += 1
                    log(f'彩铃设置已受理：{video.get("name", "")}')
                else:
                    log('彩铃设置未确认成功，继续下一个')
            except ImusicError as exc:
                if getattr(exc, 'code', '') == '0007':
                    log('登录态已失效，本轮停止彩铃设置')
                    return
                log('彩铃设置请求异常，继续下一个')
            time.sleep(5 + secrets.randbelow(6))
        if done:
            log(f'本轮共设置 {done} 个作品为彩铃；设置积分以服务端实际到账为准')

    def claim_phone_bills(self):
        rows = self.records('getOperationPrizeRecordList')
        for row in rows:
            if str(row.get('awardIndex')) not in PHONE_AWARDS or str(row.get('status')) != '0':
                continue
            award_id = row.get('awardId')
            if not award_id:
                raise ImusicError('待领话费缺少奖品编号')
            if str(award_id) in self.state['claims']:
                continue
            if not self.options.execute:
                log('有 1 元中奖话费待领取；执行模式会领取')
                continue
            self.mutate('claim', lambda: self.client.api('act/LaborApi/extractPhoneBill', {'activityId': ACTIVITY, 'awardId': award_id}), {'awardId': award_id})
            self.state['claims'].append(str(award_id))
            self.committed()
            log('已提交中奖话费领取，到账以运营商处理为准')

    def creation_history(self, since):
        """Read both captured history feeds completely; never submit a work.

        Successful works and unfinished/failed works are separate feeds. A
        missing row is useful only after both feeds have passed pagination and
        consistency checks. Server creation times have one-second precision.
        """
        lower = date(since) if isinstance(since, str) else since
        if not isinstance(lower, datetime):
            raise ImusicError('制作对账起始时间无效，保留未知请求')
        lower = (lower.replace(tzinfo=CST) if lower.tzinfo is None else lower.astimezone(CST)).replace(microsecond=0)
        found = {}

        def add_rows(batch, source, source_ids):
            if not isinstance(batch, list):
                raise ImusicError('作品历史不是列表，保留未知请求')
            for row in batch:
                if not isinstance(row, dict):
                    raise ImusicError('作品历史记录格式变化，保留未知请求')
                ident = row.get('video_id')
                if isinstance(ident, bool) or not isinstance(ident, (str, int)) or (not str(ident).strip()):
                    raise ImusicError('作品历史缺少作品编号，保留未知请求')
                ident = str(ident)
                created = date(row.get('create_time'))
                status = str(row.get('state'))
                template_id = row.get('template_conf_id')
                if status not in {'1', '2', '-1', '-2'} or (source == 'success' and status not in {'1', '2'}):
                    raise ImusicError('作品历史状态未知或矛盾，保留未知请求')
                if not isinstance(template_id, str) or not template_id.strip():
                    raise ImusicError('作品历史缺少模板编号，保留未知请求')
                item = {'video_id': ident, 'create_time': created.isoformat(), 'state': status, 'template_conf_id': template_id, 'name': str(row.get('name', ''))}
                previous = found.get(ident)
                if previous is not None and previous != item:
                    raise ImusicError('同一作品的历史记录矛盾，保留未知请求')
                found[ident] = item
                source_ids.add(ident)
        success_api = '/hapi/diy_ugc/au/get_ugc_list'
        success_ids, seen_pages = (set(), set())
        expected_count = expected_pages = None
        for page in range(1, 101):
            outer = checked(self.client.direct(success_api, {'aid': ACTIVITY, 'pageNo': page, 'pageSize': 10}, encrypted=False), success_api)
            if not isinstance(outer, dict) or str(outer.get('code')) != '0' or outer.get('success') is not True:
                raise ImusicError('成功作品历史查询未确认成功，保留未知请求')
            data = outer.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                raise ImusicError('成功作品历史结构变化，保留未知请求')
            try:
                total_count, total_pages = (number(data.get('total_count')), number(data.get('total_page')))
            except (TypeError, ValueError):
                raise ImusicError('成功作品历史缺少分页总量，保留未知请求') from None
            if total_count < 0 or total_pages < 0 or total_pages > 100 or (total_count > 0 and total_pages == 0):
                raise ImusicError('成功作品历史分页总量无效，保留未知请求')
            if expected_count is None:
                expected_count, expected_pages = (total_count, total_pages)
            elif (total_count, total_pages) != (expected_count, expected_pages):
                raise ImusicError('作品历史分页期间发生变化，稍后再对账')
            batch = data['list']
            marker = json.dumps(batch, sort_keys=True, ensure_ascii=False)
            if batch and marker in seen_pages:
                raise ImusicError('作品历史重复返回同一页，保留未知请求')
            seen_pages.add(marker)
            add_rows(batch, 'success', success_ids)
            if len(batch) > 10 or len(success_ids) > expected_count:
                raise ImusicError('作品历史条数与分页总量矛盾，保留未知请求')
            if page >= max(1, expected_pages):
                if len(success_ids) != expected_count:
                    raise ImusicError('成功作品历史未读取完整，保留未知请求')
                break
            if not batch:
                raise ImusicError('成功作品历史提前返回空页，保留未知请求')
        else:
            raise ImusicError('成功作品历史超过分页上限，保留未知请求')
        unfinished_api = 'diy/DiyVideoApi/queryFailOrCreativeDiy'
        unfinished_ids, seen_pages = (set(), set())
        for page in range(1, 101):
            batch = checked(self.client.api(unfinished_api, {'aid': ACTIVITY, 'pageNo': page, 'pageSize': 100}, encrypted_response=False), unfinished_api)
            if not isinstance(batch, list) or len(batch) > 100:
                raise ImusicError('未完成作品历史结构变化，保留未知请求')
            marker = json.dumps(batch, sort_keys=True, ensure_ascii=False)
            if batch and marker in seen_pages:
                raise ImusicError('未完成作品历史重复返回同一页，保留未知请求')
            seen_pages.add(marker)
            prior_count = len(unfinished_ids)
            add_rows(batch, 'unfinished', unfinished_ids)
            if batch and len(unfinished_ids) == prior_count:
                raise ImusicError('未完成作品历史分页没有进展，保留未知请求')
            if len(batch) < 100:
                break
        else:
            raise ImusicError('未完成作品历史超过分页上限，保留未知请求')
        return sorted((row for row in found.values() if date(row['create_time']) >= lower), key=lambda row: (row['create_time'], row['video_id']))

    def reconcile_make(self, issue):
        """Adopt a uniquely matched work, or reserve an unconfirmed old attempt.

        True means local reconciliation completed. The caller can plan fresh
        works within the remaining budget; neither outcome retries the old work.
        Absence from history is never reported as proof of a rejected request.
        """
        pending = self.state.get('uncertain')
        if not isinstance(pending, dict) or pending.get('kind') != 'make':
            return False
        if not self.client.identity_verified:
            raise ImusicError('账号身份未经核对，保留未知制作请求')
        when = date(pending.get('time'))
        now = date(issue.get('currentTime'))
        start, end = (date(issue.get('startTime')), date(issue.get('endTime')))
        if when > now + timedelta(seconds=120):
            raise ImusicError('未知制作请求时间晚于服务端时间，保留核对标记')
        issue_number = pending.get('issueNumber')
        if issue_number is None:
            if not start <= when <= end:
                raise ImusicError('无法确认未知制作所属期数，保留核对标记')
            issue_number = issue.get('issueNumber')
        if isinstance(issue_number, bool) or not isinstance(issue_number, (str, int)) or (not str(issue_number).strip()):
            raise ImusicError('未知制作所属期数无效，保留核对标记')
        if start <= when <= end and str(issue_number) != str(issue.get('issueNumber')):
            raise ImusicError('未知制作的时间与期数矛盾，保留核对标记')
        try:
            score_before = number(pending.get('scoreBefore'))
            original_after = float(self.state.get('make_after', 0))
        except (TypeError, ValueError):
            raise ImusicError('未知制作的原始记录不完整，保留核对标记') from None
        if score_before < 0 or not 0 <= original_after < float('inf'):
            raise ImusicError('未知制作的积分或冷却时间无效，保留核对标记')
        template_id = pending.get('templateConfId') or getattr(self.options, 'template', '')
        if not isinstance(template_id, str) or not template_id.strip():
            raise ImusicError('无法确认未知制作使用的模板，保留核对标记')
        history = self.creation_history(when)
        upper = when + timedelta(seconds=120)
        known_id = str(pending.get('makeId', '')).strip()
        known_name = pending.get('videoName') or pending.get('templateName')
        if pending.get('videoName') and pending.get('templateName') and (pending['videoName'] != pending['templateName']):
            raise ImusicError('未知制作保存的作品名称矛盾，保留核对标记')
        matches = []
        if known_id:
            matches = [row for row in history if row['video_id'] == known_id]
        elif isinstance(known_name, str) and known_name:
            matches = [row for row in history if row['name'] == known_name]
        if any((row['template_conf_id'] != template_id or (known_name and row['name'] != known_name) for row in matches)):
            raise ImusicError('旧制作标识与服务端作品信息矛盾，保留核对标记')
        if len(matches) > 1:
            raise ImusicError('未知制作时间附近有多个候选作品，保留核对标记')
        if not matches and now <= upper and (str(pending.get('resultCode', '')) != '0007'):
            log('旧制作请求仍在对账等待时段，本轮不重发；下次自动核对')
            return False
        updated = copy.deepcopy(self.state)
        for key in ('jobs', 'make_reservations', 'uncertain_history'):
            if key not in updated:
                updated[key] = []
            if not isinstance(updated[key], list) or any((not isinstance(row, dict) for row in updated[key])):
                raise ImusicError('本地制作对账记录格式异常，保留核对标记')
        updated['make_after'] = max(original_after, when.timestamp() + self.options.cooldown_hours * 3600)
        archive = {**copy.deepcopy(pending), 'reconciledAt': now.isoformat()}
        if matches:
            row = matches[0]
            status = {'1': 'success', '2': 'ringtone_set', '-1': 'pending', '-2': 'failed'}[row['state']]
            existing = [job for job in updated['jobs'] if str(job.get('makeId')) == row['video_id']]
            if len(existing) > 1:
                raise ImusicError('本地作品编号重复，保留核对标记')
            job = {'makeId': row['video_id'], 'status': status, 'issueNumber': issue_number, 'submitted': row['create_time'], 'scoreBefore': score_before, 'templateConfId': template_id, 'recovered': True}
            if 'creationPointsBefore' in pending:
                job.update(creationPointsBefore=number(pending['creationPointsBefore']), creditVerified=False)
            if existing:
                old = existing[0]
                old_status = old.get('status')
                compatible = old_status in {'pending', status} or {old_status, status} <= {'success', 'ringtone_set'}
                if str(old.get('issueNumber')) != str(issue_number) or not compatible:
                    raise ImusicError('本地作品与服务端历史矛盾，保留核对标记')
                old.update(job)
            else:
                updated['jobs'].append(job)
            archive.update(resolution='matched_history', makeId=row['video_id'], status=status)
            message = '已从服务端历史找到旧制作，接管作品状态，继续核对可用制作额度'
        elif str(pending.get('resultCode', '')) == '0007':
            archive.update(resolution='rejected_auth')
            message = '旧制作请求已确认被登录态校验拒绝，未受理；不占用本期制作名额'
        else:
            reservation_id = hashlib.sha256(json.dumps([str(issue_number), when.isoformat(), score_before], ensure_ascii=False).encode()).hexdigest()[:24]
            if not any((row.get('id') == reservation_id for row in updated['make_reservations'])):
                updated['make_reservations'].append({'id': reservation_id, 'issueNumber': issue_number, 'time': when.isoformat(), 'scoreBefore': score_before, 'count': 1, 'score': 20, 'status': 'unconfirmed'})
            archive.update(resolution='reserved_unconfirmed', reservationId=reservation_id)
            message = '旧制作已完成历史对账；不判定失败，仍预留1次/20点，继续使用其余制作额度'
        updated['uncertain_history'].append(archive)
        updated.pop('uncertain', None)
        previous_state = self.state
        self.state = updated
        try:
            self.save()
        except Exception:
            self.state = previous_state
            raise
        log(message)
        return True

    def resume_jobs(self):
        for job in self.state['jobs']:
            if job.get('status') != 'pending':
                continue
            data = checked(self.client.api('diy/DiyVideoApi/querySingleDiyResult', {'videoId': job['makeId']}, encrypted_response=False), 'diy/DiyVideoApi/querySingleDiyResult')
            status = str(data.get('state'))
            if status == '1':
                job['status'] = 'success'
                log('之前提交的作品已制作成功')
            elif status == '2':
                job['status'] = 'ringtone_set'
                log('之前提交的作品已设置为彩铃')
            elif status == '-2':
                job['status'] = 'failed'
                log('之前提交的作品制作失败，保留记录，不自动重复提交')
            elif status not in {'-1'}:
                raise ImusicError('作品状态尚未识别，保留待处理记录')
        self.save()
        return any((job.get('status') == 'pending' for job in self.state['jobs']))

    def _template_failure_counts(self, issue):
        """本期各模板的提交失败次数；只统计当前期，历史期记录在写入时清理。"""
        rows = self.state.get('template_failures', [])
        if not isinstance(rows, list):
            raise ImusicError('模板失败记录格式异常，停止制作')
        counts = {}
        for row in rows:
            if not isinstance(row, dict) or str(row.get('issueNumber')) != str(issue['issueNumber']):
                continue
            conf = str(row.get('conf', ''))
            if conf:
                counts[conf] = number(row.get('count', 1))
        return counts

    def _record_template_failure(self, conf, issue):
        """记住本期提交失败的模板；auto 模式下轮优先换用其他推荐模板。"""
        rows = [row for row in self.state.get('template_failures', []) if isinstance(row, dict) and str(row.get('issueNumber')) == str(issue['issueNumber'])]
        key = str(conf)
        row = next((r for r in rows if str(r.get('conf')) == key), None)
        if row is None:
            rows.append({'conf': key, 'issueNumber': str(issue['issueNumber']), 'count': 1})
        else:
            row['count'] = number(row.get('count', 1)) + 1
        self.state['template_failures'] = rows
        self.save()

    def template(self, issue=None):
        data = checked(self.client.api('diy/DiyVideoApi/queryActRecommendTemplateList', {'activityId': 'ai119_4', 'pageNo': 1, 'pageSize': 50}, encrypted_response=False), 'diy/DiyVideoApi/queryActRecommendTemplateList')
        rows = data.get('list', [])
        if self.options.template and self.options.template != 'auto':
            for row in rows:
                if row.get('templateConfId') == self.options.template:
                    if row.get('mediaType') != 'video' or number(row.get('imageCount', -1)) != 0:
                        raise ImusicError('模板需要额外素材或不是视频，停止制作')
                    if not row.get('userWords') or not row.get('templateId'):
                        raise ImusicError('模板缺少提示词或编号')
                    return row
            raise ImusicError('指定模板已不在活动推荐列表，停止制作')
        ready = [row for row in rows if row.get('mediaType') == 'video' and number(row.get('imageCount', -1)) == 0 and row.get('userWords') and row.get('templateId')]
        if not ready:
            raise ImusicError('当前推荐列表没有可直接制作的视频模板，停止制作')
        chosen = ready[0]
        if issue is not None:
            failed = self._template_failure_counts(issue)
            fresh = [row for row in ready if not failed.get(str(row.get('templateConfId')))]
            if fresh:
                chosen = fresh[0]
                if chosen is not ready[0]:
                    log('跳过本期提交失败过的模板，改用：' + str(chosen.get('videoName')) + '（' + str(chosen.get('templateConfId')) + '）')
            else:
                least = min(ready, key=lambda row: failed.get(str(row.get('templateConfId')), 0))
                if failed.get(str(least.get('templateConfId')), 0) >= 3:
                    raise ImusicError('当前推荐的视频模板本期提交均多次失败，停止制作；稍后重试或用IMUSIC_TEMPLATE指定')
                chosen = least
        log('自动选用当前推荐视频模板：' + str(chosen.get('videoName')) + '（' + str(chosen.get('templateConfId')) + '）')
        return chosen

    def make_payload(self, template):
        name = str(template['videoName']) + datetime.now(CST).strftime('%H%M%S') + secrets.token_hex(2)
        payload = {key: '' for key in ('openId', 'makeId', 'background', 'userPhotos', 'inviterMobile', 'arrangeId', 'aiGatewayImagMakeId', 'fromType', 'sessionId', 'voice', 'videoOwnerCode', 'videoResourceCode', 'workId', 'imageMakeType', 'songId', 'imageRate', 'productionType')}
        payload.update(aid=ACTIVITY, templateId=template['templateId'], templateConfId=template['templateConfId'], userWords=template['userWords'], templateName=name, videoName=name, aiPack=0, autoOrderUgc=0)
        return payload

    def prepare_make(self, template):
        """Run the observed page/template preparation before submitting a job."""
        self.client.open_activity_page()
        if self.client.invite_code and (not self.client.mark_invite_entry()):
            log('官方入口步骤未完成，不影响制作提交')
        channel = self.client.channel_id
        checked(self.client.activity_event('page_vring_index', '玩转AI赢手机_activityID_' + ACTIVITY + '_entrance_' + channel), '/vapi/vue_stat/sendMessage')
        quota = checked(self.client.api('ismp/IsmpApi/queryAiMakePkgInfo', {'templateId': template['templateId'], 'aid': ACTIVITY}), 'ismp/IsmpApi/queryAiMakePkgInfo')
        if number(quota['privilegeVrbtAIVideoLeftNum']) <= 0:
            raise ImusicError('所选模板当前无制作额度，尚未提交制作')
        checked(self.client.direct('/hapi/diy_video/imu/query_template_info', {'templateConfId': template['templateConfId'], 'templateId': template['templateId']}, encrypted=False), '/hapi/diy_video/imu/query_template_info')
        param = 'activityID_' + ACTIVITY + '_templateID_' + template['templateId'] + '_entrance_' + channel + '_templateconfID_' + template['templateConfId']
        checked(self.client.activity_event('activity_vring_make_1.9', '_' + param), '/vapi/vue_stat/sendMessage')
        checked(self.client.activity_event('activity_2511AI-makeonekey_1.6', param), '/vapi/vue_stat/sendMessage')

    def make(self, issue):
        if self.options.make_count == 0:
            return
        if self.state.get('uncertain'):
            raise ImusicError('还有未经对账的请求，本轮先保留制作额度')
        if self.resume_jobs():
            log('已有作品生成中，继续按积分到账情况和剩余额度安排制作')
        wait_until = float(self.state.get('make_after', 0)) if self.options.cooldown_hours > 0 else 0
        if time.time() < wait_until:
            next_time = datetime.fromtimestamp(wait_until, CST).strftime('%m-%d %H:%M:%S')
            log(f'已设置的制作间隔，{next_time} 后再检查额度')
            return
        start, end, now = (date(issue['startTime']), date(issue['endTime']), date(issue['currentTime']))
        if not start <= now <= end:
            log('服务端当前期不在参与时段，跳过制作')
            return
        points = creation_points(self.records('getOperationRecordList'), issue)
        for job in self.state['jobs']:
            if job.get('creditVerified') is False and str(job.get('issueNumber')) == str(issue['issueNumber']):
                if points < number(job['creationPointsBefore']) + 20:
                    log('已有制作的积分尚未确认，本轮先保留额度；下次继续核对')
                    return
                job['creditVerified'] = True
                self.save()
        reserved = self.reserved_make_points(issue)
        left = max(0, (340 - points - reserved) // 20)
        quota = self.quota()
        count = min(self.options.make_count, left, max(0, number(quota['privilegeVrbtAIVideoLeftNum'])))
        log(f'本期已识别制作获点 {points}/340；本轮计划最多制作 {count} 个')
        if reserved:
            log(f'本期另有 {reserved} 点按旧请求可能已受理预留，避免重复占用制作上限')
        if not count or not self.options.execute:
            return
        template = self.template(issue)
        throttle_retries = 0
        for _ in range(count):
            if points + reserved >= 340:
                break
            current_issue = self.api('getOperationCurrentIssueInfo')
            if str(current_issue['issueNumber']) != str(issue['issueNumber']) or not start <= date(current_issue['currentTime']) <= end:
                log('当前期已切换或结束，本轮停止；下次按新期重新计算')
                break
            data = None
            auth_retry = 0
            while data is None:
                self.prepare_make(template)
                before_total, _ = self.score()
                payload = self.make_payload(template)
                self.state['make_after'] = time.time() + self.options.cooldown_hours * 3600
                self.save()
                pace_make_submission(self.options.make_interval)
                try:
                    data = self.mutate('make', lambda: self.client.direct('/hapi/diy_video/au/template_make_add_v2', payload), {'issueNumber': issue['issueNumber'], 'scoreBefore': before_total, 'videoName': payload['videoName'], 'templateConfId': payload['templateConfId'], 'creationPointsBefore': points})
                except ImusicApiError as exc:
                    if exc.code in ('10007', '10017'):
                        # 10007/10017 为服务端明确未受理：退避后重试不会重复制作。
                        # 先解除本地未确认锁（服务端已明确拒绝，无受理风险），否则重试会被锁拦截。
                        _MAKE_GATE['count'] = 0
                        if throttle_retries >= 2:
                            log(f'{exc.code}退避重试后仍被服务端拒绝：本轮停止制作，下轮自动对账后处理')
                            raise
                        throttle_retries += 1
                        backoff = 150 + secrets.randbelow(61)
                        log(f'服务端返回{exc.code}（疑似同IP高频制作限制）：等待 {backoff} 秒后退避重试（{throttle_retries}/2）')
                        self.state.pop('uncertain', None)
                        self.save()
                        time.sleep(backoff)
                        continue
                    if exc.code == '0007':
                        if auth_retry == 0:
                            auth_retry = 1
                            log('制作提交返回0007（登录态未通过）：重建会话后重试一次')
                            self.state.pop('uncertain', None)
                            self.save()
                            self.client.refresh_login()
                            self.client.activity_sid = ''
                            continue
                        log('重试仍返回0007：按服务端明确拒绝归档，不占本期制作预留')
                        pending = self.state.get('uncertain')
                        if isinstance(pending, dict):
                            pending['resultCode'] = '0007'
                            self.save()
                        raise
                    self._record_template_failure(payload['templateConfId'], issue)
                    raise
            if str(data.get('code')) != '0000' or data.get('success') is not True or (not data.get('makeId')):
                raise ImusicError('制作提交结果不明确，已保留核对标记，不重复请求')
            job = {'makeId': data['makeId'], 'status': 'pending', 'issueNumber': issue['issueNumber'], 'submitted': datetime.now(CST).isoformat(), 'scoreBefore': before_total, 'creationPointsBefore': points, 'creditVerified': False}
            self.state['jobs'].append(job)
            self.committed()
            log('制作已受理，核对积分与生成进度')
            gained = False
            for _check in range(3):
                time.sleep(5)
                current_points = creation_points(self.records('getOperationRecordList'), issue)
                if current_points >= points + 20:
                    gained, points = (True, current_points)
                    job['creditVerified'] = True
                    self.save()
                    break
            self.resume_jobs()
            if not gained:
                log('暂未核实本期制作积分增加，本轮停止继续消耗额度')
                return
            log(f'已核实制作积分到账；本期累计 {points}/340')
            if points + reserved >= 340:
                break

    def reserved_make_points(self, issue):
        """Keep conservative point accounting only for fully archived attempts."""
        reservations = self.state.get('make_reservations', [])
        archives = self.state.get('uncertain_history', [])
        if not isinstance(reservations, list) or not isinstance(archives, list):
            raise ImusicError('旧制作对账记录格式异常，保留状态')
        reserved = 0
        for row in reservations:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or (not row['id']):
                raise ImusicError('旧制作预留缺少对账编号，保留状态')
            matches = [a for a in archives if isinstance(a, dict) and a.get('reservationId') == row['id'] and (a.get('resolution') == 'reserved_unconfirmed') and (a.get('kind') == 'make')]
            if len(matches) != 1 or number(row.get('count')) != 1 or number(row.get('score')) != 20:
                raise ImusicError('旧制作预留缺少完整对账凭据，保留状态')
            if str(row.get('issueNumber')) == str(issue['issueNumber']):
                reserved += 20
        return reserved

    def monthly_clock(self):
        when = date(self.api('getOperationCurrentIssueInfo')['currentTime'])
        return (when.strftime('%Y-%m'), when)

    def is_final_issue(self, issue):
        """规则：最后一期开奖（2027-01-12）后剩余点数清零；判断当前期是否需要抽光点数。"""
        marks = [issue.get('drawTime'), issue.get('endTime')]
        known = [date(value) for value in marks if value]
        if not known:
            log('当前期缺少开奖与结束时间，无法判断是否最后一期，按非末期处理')
            return False
        return any((value >= FINAL_DRAW_DATE for value in known))

    def final_redeem_reachable(self, remaining):
        """最后一期里估算制作上限内还能否攒满1000点；攒不满就提前抽光，避免开奖清零。"""
        issue = self._issue
        if issue is None:
            raise ImusicError('尚未取得当前期信息，不能判断期末策略')
        points = creation_points(self.records('getOperationRecordList'), issue)
        reserved = self.reserved_make_points(issue)
        left = max(0, (340 - points - reserved) // 20)
        quota = max(0, number(self.quota()['privilegeVrbtAIVideoLeftNum']))
        return remaining + min(left, quota) * 20 >= 1000

    def monthly_record(self, month):
        rows = self.state.get('monthly_spending', {})
        if not isinstance(rows, dict):
            raise ImusicError('月度策略记录格式异常，保留点数')
        row = rows.get(month)
        if row is not None and (not isinstance(row, dict) or row.get('status') not in {'redeemed', 'out_of_stock'}):
            raise ImusicError('月度策略状态无法识别，保留点数')
        return row

    def commit_monthly(self, month, record, clear_uncertain=False):
        """Persist the decision and its journal completion in one atomic write."""
        self.monthly_record(month)
        previous = self.state
        updated = copy.deepcopy(previous)
        updated.setdefault('monthly_spending', {})[month] = copy.deepcopy(record)
        if record.get('status') == 'redeemed':
            updated['ten_yuan_claimed'] = True
        if clear_uncertain:
            updated.pop('uncertain', None)
        self.state = updated
        try:
            self.save()
        except Exception:
            self.state = previous
            raise

    def record_redemption_receipt(self, when):
        """Keep a known success and its server-time bound while resolving a month boundary."""
        previous = self.state
        updated = copy.deepcopy(previous)
        updated['uncertain'].update(responseCode='0000', responseTime=when.isoformat())
        self.state = updated
        try:
            self.save()
        except Exception:
            self.state = previous
            raise

    def redemption_records(self):
        found = {}
        for row in self.records('getOperationPrizeRecordList'):
            if str(row.get('awardIndex')) != REDEEM_AWARD:
                continue
            ident = row.get('awardId')
            if not isinstance(ident, (str, int)) or isinstance(ident, bool) or (not str(ident).strip()):
                raise ImusicError('10元兑换记录缺少编号，暂不兑换或抽奖')
            when = date(row.get('lotteryDate'))
            item = {'awardId': str(ident), 'time': when.isoformat(), 'month': when.strftime('%Y-%m')}
            if item['awardId'] in found and found[item['awardId']] != item:
                raise ImusicError('10元兑换记录时间矛盾，暂不兑换或抽奖')
            found[item['awardId']] = item
        return list(found.values())

    def sync_monthly_redemption(self, month):
        record = self.monthly_record(month)
        if record is not None or month in self._monthly_history_checked:
            return (record, [])
        rows = self.redemption_records()
        current = [row for row in rows if row['month'] == month]
        if current:
            latest = max(current, key=lambda row: row['time'])
            record = {'status': 'redeemed', 'source': 'prize_history', 'time': latest['time'], 'awardIds': [row['awardId'] for row in current]}
            self.commit_monthly(month, record)
        self._monthly_history_checked.add(month)
        return (record, [row['awardId'] for row in rows])

    def reconcile_redemption(self):
        """Recover a lost receipt only from a matching new redemption record."""
        pending = self.state.get('uncertain')
        if not isinstance(pending, dict) or pending.get('kind') != 'redeem':
            return False
        if not self.client.identity_verified:
            raise ImusicError('账号身份未经核对，保留未知兑换请求')
        when = date(pending.get('time'))
        request_month = when.strftime('%Y-%m')
        if pending.get('month', request_month) != request_month:
            return False
        known = pending.get('knownAwardIds')
        if not isinstance(known, list) or any((not isinstance(ident, str) for ident in known)):
            return False
        response_time = None
        if pending.get('responseCode') == '0000':
            response_time = date(pending.get('responseTime'))
            if response_time < when:
                return False
        rows = self.redemption_records()
        candidates = [row for row in rows if row['awardId'] not in known and date(row['time']) >= when.replace(microsecond=0) and (date(row['time']) <= response_time.replace(microsecond=999999) if response_time is not None else row['month'] == request_month)]
        if len(candidates) != 1:
            return False
        row = candidates[0]
        record = {'status': 'redeemed', 'source': 'reconciled_prize_history', 'time': row['time'], 'awardIds': [row['awardId']]}
        if 'totalBefore' in pending and 'balanceBefore' in pending:
            record['drawGuard'] = {'totalBefore': number(pending['totalBefore']), 'balanceAfterRedeem': max(0, number(pending['balanceBefore']) - 1000), 'spent': 0}
        self.commit_monthly(row['month'], record, clear_uncertain=True)
        log('已从服务端记录确认之前的10元兑换；按兑换月份恢复策略，到账以运营商处理为准')
        return True

    def monthly_available(self, month, total, remaining):
        record = self.monthly_record(month) or {}
        guard = record.get('drawGuard')
        if guard is None:
            return remaining
        if not isinstance(guard, dict):
            raise ImusicError('兑换后点数核对记录异常，暂停抽奖')
        baseline = number(guard.get('totalBefore'))
        after = number(guard.get('balanceAfterRedeem'))
        spent = number(guard.get('spent'))
        if min(baseline, after, spent) < 0 or total < baseline:
            raise ImusicError('兑换后点数尚未核对，暂停抽奖')
        return min(remaining, max(0, after + total - baseline - spent))

    def draw_once(self, month=None, reserve=0):
        """One 20-point lottery draw. Returns the masked award name, or None
        when the run draw cap, the point floor, or a month change says stop."""
        if self._draws_this_run >= self.options.max_draws:
            return None
        total, remaining = self.score()
        available = self.monthly_available(month, total, remaining) if month else remaining
        if self._draw_budget_remaining is not None and self._draw_budget_remaining < 20:
            return None
        if available - 20 < reserve:
            return None
        if month and self.monthly_clock()[0] != month:
            return None
        self._draws_this_run += 1
        if self._draw_budget_remaining is not None:
            self._draw_budget_remaining -= 20
        data = self.mutate('draw', lambda: self.client.api('act/LaborApi/operationIntegralLottery', {'activityId': ACTIVITY}), {'balanceBefore': remaining, **({'month': month} if month else {})})
        if not isinstance(data, dict) or not data.get('awardName'):
            raise ImusicError('抽奖返回缺少结果，保留核对标记')
        name = re.sub('1[3-9]\\d{9}', '[手机号]', str(data['awardName']))[:80]
        if month:
            record = self.monthly_record(month)
            if record is not None:
                record = copy.deepcopy(record)
                if record.get('drawGuard') is not None:
                    record['drawGuard']['spent'] += 20
                self.commit_monthly(month, record, clear_uncertain=True)
            else:
                self.committed()
        else:
            self.committed()
        log('抽奖结果：' + name)
        time.sleep(3)
        return name

    def draw_points(self, reserve, month=None):
        if self.state.get('uncertain'):
            raise ImusicError('上次请求结果未确认，暂不消耗点数抽奖')
        total, remaining = self.score()
        available = self.monthly_available(month, total, remaining) if month else remaining
        draw_limit = min(max(0, self.options.max_draws - self._draws_this_run), max(0, (available - reserve) // 20))
        if self._draw_budget_remaining is not None:
            draw_limit = min(draw_limit, self._draw_budget_remaining // 20)
        if not draw_limit:
            log('本轮抽奖次数已满，或可用点数不足20点，等待下次运行')
            return
        if not self.options.execute:
            log(f'当前可用 {available} 点，执行模式本轮最多抽奖 {draw_limit} 次')
            return
        if self._draw_budget_remaining is None:
            self._draw_budget_remaining = draw_limit * 20
        for _ in range(draw_limit):
            if month and self.monthly_clock()[0] != month:
                log('已进入新的自然月，停止本轮抽奖；下次重新优先兑换10元')
                return
            if self.draw_once(month, reserve) is None:
                log('点数不足或达到保留点数，本轮停止抽奖')
                return

    def topup_make(self, issue):
        """极速满额补件：期末免费额度用光且本期未满340点时，抽奖只为补AI体验券。

        规则对应的执行策略：15 次免费制作（300 点）优先用光；缺的名额在临近
        本期结束（最后 TOPUP_WINDOW_DAYS 天，免费额度基本不会再刷新）时用
        抽奖赢 AI 体验券（中奖率约 43%）补齐到 17 件满 340 点；抽中即制作回
        20 点。抽奖只为补制作、别多抽：每轮封顶 2×缺额+1 次，抽中或满额即停。
        """
        if not self.options.execute or self.options.make_count == 0 or issue is None:
            return
        start, end, now = (date(issue['startTime']), date(issue['endTime']), date(issue['currentTime']))
        if not start <= now <= end:
            return
        if (end - now).days > TOPUP_WINDOW_DAYS:
            points = creation_points(self.records('getOperationRecordList'), issue)
            reserved = self.reserved_make_points(issue)
            if points + reserved < 340:
                log(f'本期 {points + reserved}/340 点且额度用光：距结束还有 {(end - now).days} 天，先等免费额度刷新；最后 {TOPUP_WINDOW_DAYS} 天才抽奖补件凑满 340 点')
            return
        while True:
            points = creation_points(self.records('getOperationRecordList'), issue)
            reserved = self.reserved_make_points(issue)
            needed = max(0, (340 - points - reserved + 19) // 20)
            if needed <= 0:
                log('本期 340 点已凑满，停止补件抽奖')
                return
            if number(self.quota()['privilegeVrbtAIVideoLeftNum']) <= 0:
                _, remaining = self.score()
                if remaining < 20:
                    log(f'余额 {remaining} 点不足 20，本期无法继续补件抽奖；保留点数攒兑换')
                    return
                if self._topup_draws >= max(3, 2 * needed + 1):
                    log('本轮补件抽奖已达上限（只为补体验券，不额外多抽），等待下次运行')
                    return
                if self.draw_once() is None:
                    log('本轮抽奖次数已满或点数不足，停止补件抽奖')
                    return
                self._topup_draws += 1
                continue
            before = (points, number(self.quota()['privilegeVrbtAIVideoLeftNum']))
            try:
                self.make(issue)
            except (ImusicError, KeyError, ValueError, TypeError) as exc:
                if self.state.get('uncertain'):
                    raise
                log('补件制作未完成：' + (str(exc) if isinstance(exc, ImusicError) else '制作数据结构变化'))
                return
            after = (creation_points(self.records('getOperationRecordList'), issue), number(self.quota()['privilegeVrbtAIVideoLeftNum']))
            if after == before:
                return

    def spend_monthly(self, allow_draw=True):
        if self.state.get('uncertain'):
            raise ImusicError('上次请求结果未确认，暂不兑换或抽奖')
        month, when = self.monthly_clock()
        if self.state.get('ten_yuan_claimed'):
            if allow_draw:
                log('10元话费已兑换到手：极速满额自动转为全抽奖，后续点数每轮抽光')
                self.draw_points(self.options.reserve_points, month)
            return
        record, known_ids = self.sync_monthly_redemption(month)
        if record is None:
            total, remaining = self.score()
            if remaining < 1000:
                if self._final_issue and allow_draw and (not self._redeem_attempted_this_run) and (not self.final_redeem_reachable(remaining)):
                    log(f'{month} 已是最后一期且制作上限内攒不满1000点：{remaining} 点全部投入抽奖，避免开奖后清零')
                    self.draw_points(self.options.reserve_points, month)
                    return
                log(f'{month} 优先攒10元话费：保留 {remaining} 点，还差 {1000 - remaining} 点；尚未确认缺货，不抽奖')
                if allow_draw:
                    self.topup_make(self._issue)
                return
            if not self.options.execute:
                log(f'{month} 尚未兑换10元，执行时先兑换；成功或明确缺货后才转抽奖')
                return
            if self._redeem_attempted_this_run:
                return
            fresh_month, when = self.monthly_clock()
            if fresh_month != month:
                log('月份已变化，本次不提交兑换，稍后按新月份重新计算')
                return
            if not known_ids:
                self._monthly_history_checked.discard(month)
                record, known_ids = self.sync_monthly_redemption(month)
                if record is not None:
                    if allow_draw:
                        self.draw_points(self.options.reserve_points, month)
                    return
            self._redeem_attempted_this_run = True
            try:
                self.mutate('redeem', lambda: self.client.api('act/LaborApi/operationIntegralRedeemPrize', {'activityId': ACTIVITY}), {'month': month, 'time': when.isoformat(), 'balanceBefore': remaining, 'totalBefore': total, 'knownAwardIds': known_ids})
            except ImusicApiError as exc:
                if exc.code != '10003' or exc.api != MUTATION_APIS['redeem']:
                    raise
                record = {'status': 'out_of_stock', 'source': 'redeem_api', 'code': '10003', 'time': when.isoformat()}
                self.commit_monthly(month, record, clear_uncertain=True)
                log(f'{month} 兑换接口明确返回库存不足，本月自动转为积分抽奖，次月重新优先兑换')
            else:
                response_month, response_time = self.monthly_clock()
                if response_time < when:
                    raise ImusicError('兑换后的服务端时间不一致，保留请求记录等待核对')
                if response_month != month:
                    self.record_redemption_receipt(response_time)
                    if not self.reconcile_redemption():
                        raise ImusicError('10元兑换已受理，但请求跨月，等待奖品记录确认归属月份；暂不抽奖或重复兑换')
                    return
                record = {'status': 'redeemed', 'source': 'redeem_api', 'time': when.isoformat(), 'drawGuard': {'totalBefore': total, 'balanceAfterRedeem': remaining - 1000, 'spent': 0}}
                self.commit_monthly(month, record, clear_uncertain=True)
                log(f'{month} 10元话费兑换已受理，本月后续点数用于抽奖；话费到账以运营商处理为准')
        elif allow_draw:
            reason = '已有10元兑换记录' if record['status'] == 'redeemed' else '此前收到明确缺货结果'
            log(f'{month} {reason}，本月采用积分抽奖')
        if allow_draw:
            self.draw_points(self.options.reserve_points, month)

    def spend(self):
        if self.options.mode == 'auto':
            self.spend_monthly()
            return
        if self.options.mode == 'save':
            month, when = self.monthly_clock()
            if self._redeem_attempted_this_run:
                return
            record = self.monthly_record(month)
            if record is not None:
                if record['status'] == 'out_of_stock':
                    log(f'{month} 本月兑换已确认缺货（跨重启保存），次月自动重试；本轮保留点数')
                else:
                    log(f'{month} 本月10元话费已兑换（跨重启保存），本月不再兑换')
                return
            _, remaining = self.score()
            if remaining < 1000:
                if self._final_issue and not self.final_redeem_reachable(remaining):
                    if self.options.execute:
                        log(f'{month} 已是最后一期且制作上限内攒不满1000点：{remaining} 点全部投入抽奖，避免开奖后清零')
                        self.draw_points(0)
                    else:
                        log(f'{month} 已是最后一期且攒不满1000点，执行时将把 {remaining} 点全部投入抽奖避免清零')
                else:
                    log(f'保留 {remaining} 点，距离兑换 10 元话费还差 {1000 - remaining} 点')
                return
            if not self.options.execute:
                log('已满 1000 点，执行模式将尝试兑换一次 10 元话费')
                return
            self._redeem_attempted_this_run = True
            try:
                self.mutate('redeem', lambda: self.client.api('act/LaborApi/operationIntegralRedeemPrize', {'activityId': ACTIVITY}), {'balanceBefore': remaining})
            except ImusicApiError as exc:
                if exc.code == '10003' and exc.api == MUTATION_APIS['redeem']:
                    self.commit_monthly(month, {'status': 'out_of_stock', 'source': 'redeem_api', 'code': '10003', 'time': when.isoformat()}, clear_uncertain=True)
                    log(f'{month} 服务端返回兑换库存不足，本月保留点数；缺货状态已跨重启保存，次月自动重试')
                    return
                raise
            self.commit_monthly(month, {'status': 'redeemed', 'source': 'redeem_api', 'time': when.isoformat()}, clear_uncertain=True)
            log('10 元话费兑换已受理，本月不再兑换（状态已跨重启保存）；到账时间以活动规则为准')
        elif self.options.mode == 'draw':
            self.draw_points(self.options.reserve_points)

    def run(self):
        if not self.client.identity_verified:
            self.client.refresh_login()
        log('活动登录身份核对通过')
        issue = self.api('getOperationCurrentIssueInfo')
        self._issue = issue
        self._final_issue = self.is_final_issue(issue)
        total, remaining = self.score()
        quota = self.quota()
        self.state['last_observed'] = {'time': datetime.now(CST).isoformat(), 'issueNumber': issue.get('issueNumber'), 'totalScore': total, 'remainingScore': remaining, 'quota': {key: quota.get(key) for key in ('privilegeVrbtAIVideoLeftNum', 'monthFreeNum', 'aidDailyNum')}}
        self.save()
        log(f"{issue.get('issueName')}，开奖 {issue.get('drawTime')}；累计获点 {total}，余额 {remaining}")
        log(f"可制作 {quota.get('privilegeVrbtAIVideoLeftNum')} 次；月度剩余 {quota.get('monthFreeNum')}，活动剩余 {quota.get('aidDailyNum')}")
        if self.options.clear_uncertain:
            self.state.pop('uncertain', None)
            self.save()
            log('已按明确参数解除本地未知结果锁，本轮只查询、不参与')
            return
        if self.state.get('uncertain'):
            recovered = False
            if self.options.execute:
                recovered = self.reconcile_redemption() if self.state['uncertain'].get('kind') == 'redeem' else self.reconcile_make(issue)
            if not recovered or self.state.get('uncertain'):
                log('上次变更请求结果未确认，停止所有变更；请核对活动记录后使用 --clear-uncertain')
                return
        self.notify_tickets()
        self.claim_phone_bills()
        self.handle_ringtones(issue)
        if self.options.mode == 'auto':
            self.spend_monthly(allow_draw=False)
        if self.options.execute and self.options.mode == 'save' and (self.score()[1] >= 1000):
            self.spend()
        try:
            self.make(issue)
        except (ImusicError, KeyError, ValueError, TypeError) as exc:
            if self.state.get('uncertain'):
                raise
            log(str(exc) if isinstance(exc, ImusicError) else '制作数据结构变化，已跳过制作')
        self.spend()
        if self.options.execute:
            self.claim_phone_bills()
        total, remaining = self.score()
        log(f'本轮结束：累计获点 {total}，余额 {remaining}')

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phone', action='append', default=[], help='仅处理指定手机号，可重复')
    p.add_argument('--execute', action='store_true', help='执行参与；默认只查询和计算计划')
    p.add_argument('--mode', choices=('save', 'auto', 'draw', 'observe'), default='save', help='只攒分=save（默认）：满1000兑换10元不抽奖，当月状态跨重启保存；极速满额=auto：每期做满340点，期末抽奖补体验券，满1000立即兑10元、兑后自动转全抽奖；全抽奖=draw：全部积分投抽奖；observe=只查询')
    p.add_argument('--make-count', type=int, default=17, help='每轮制作上限；实际按剩余额度及本期340点上限计算，0=不制作')
    p.add_argument('--cooldown-hours', type=float, default=0, help='额外制作间隔；默认0按服务端额度参与，不额外等待')
    p.add_argument('--make-interval', type=int, default=20, help='同一账号相邻两次制作的间隔秒数；默认20，降低同IP快速连发触发10007的概率')
    p.add_argument('--max-draws', type=int, default=10, help='每轮最多抽奖次数；最后一期用于抽光余额')
    p.add_argument('--reserve-points', type=int, default=None, help='抽奖保留点数：auto默认0，draw默认1000')
    p.add_argument('--template', default='auto', help='模板编号；默认auto，自动选用当前推荐列表中可直接制作的视频模板')
    p.add_argument('--invite-code', default=os.getenv('IMUSIC_INVITE_CODE', DEFAULT_INVITE_CODE).strip(), help='入口扩展参数：随页面访问和制作请求提交；留空关闭')
    p.add_argument('--state-dir', type=Path, default=ROOT / 'state')
    p.add_argument('--clear-uncertain', action='store_true', help='手动核对页面后解除未知结果锁；本轮不参与')
    return p
# ===== 电信登录工具（仅供AI奇遇Ticket自动登录使用） =====
import os, sys, json, time, random, string, base64, certifi, requests
from datetime import datetime
from pathlib import Path
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5, DES3, AES
from Crypto.Util.Padding import pad, unpad
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
KEYS = {'login_rsa': '-----BEGIN PUBLIC KEY-----\nMIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofdWzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMiPMpq0/XKBO8lYhN/gwIDAQAB\n-----END PUBLIC KEY-----', 'des3': b'1234567`90koiuyhgtfrdews'}
TELECOM_CACHE_FILE = Path(__file__).parent / 'Cache.json'
telecom_logs, telecom_cache = ([], {})
telecom_log = lambda m: (telecom_logs.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {m}"), print(telecom_logs[-1]))
mask = lambda p: f'{p[:3]}****{p[-4:]}' if len(p) >= 7 else p
ts = lambda: datetime.now().strftime('%Y%m%d%H%M%S')
rd_str = lambda l: ''.join(random.choices(string.ascii_letters + string.digits, k=l))
encode = lambda s: ''.join((chr(ord(c) + 2) for c in s))

class CustomSSLAdapter(HTTPAdapter):

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ciphers='DEFAULT@SECLEVEL=1:!aNULL:!eNULL:!MD5')
        ctx.check_hostname = False
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)
telecom_session = requests.Session()
telecom_session.verify = certifi.where()
telecom_session.headers.update({'User-Agent': 'Mozilla/5.0 (Linux; U; Android 12; zh-cn) AppleWebKit/533.1 (KHTML, like Gecko) Version/5.0 Mobile Safari/533.1'})
telecom_session.mount('https://', CustomSSLAdapter())

def encrypt_des3(data, mode='enc'):
    cipher = DES3.new(KEYS['des3'], DES3.MODE_CBC, 8 * b'\x00')
    if mode == 'enc':
        return cipher.encrypt(pad(data.encode(), 8)).hex()
    return unpad(cipher.decrypt(bytes.fromhex(data)), 8).decode()

def encrypt_rsa(data, key_type='login', out='hex'):
    cipher = PKCS1_v1_5.new(RSA.import_key(KEYS[f'{key_type}_rsa']))
    data = json.dumps(data, separators=(',', ':')) if isinstance(data, (dict, list)) else data
    if out == 'hex':
        return ''.join((cipher.encrypt(data[i:i + 32].encode()).hex() for i in range(0, len(data), 32)))
    return base64.b64encode(cipher.encrypt(data.encode())).decode()

def api_req(url, method='POST', log_msg=None, raw=False, **kwargs):
    try:
        r = telecom_session.request(method, url, timeout=15, **kwargs)
        if raw:
            return r.text
        res = r.json()
        if not isinstance(res, dict):
            return {}
        return res
    except Exception as e:
        return '' if raw else {}


def load_cache():
    global telecom_cache
    if TELECOM_CACHE_FILE.exists():
        telecom_cache = json.loads(TELECOM_CACHE_FILE.read_text(encoding='utf-8'))

def save_cache():
    TELECOM_CACHE_FILE.write_text(json.dumps(telecom_cache, ensure_ascii=False), encoding='utf-8')
# ===== 电信登录校验与脱敏诊断 =====
"""电信登录响应校验及脱敏诊断，内联在旧任务之后以覆盖 login。"""
import hashlib
import re
import tempfile
import xml.etree.ElementTree as _telecom_et
from datetime import timezone, timedelta


TELECOM_AUTH_RESULTS = {}
TELECOM_PASSWORD_ATTEMPTS = {}


def _telecom_timestamp():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')


def _telecom_redact(value, phone, password):
    """仅用于已选中的错误字段；绝不保存完整请求或响应。"""
    text = str(value)
    secrets = [str(phone), str(password), encode(str(phone)), encode(str(password))]
    cached = telecom_cache.get(phone, {})
    if isinstance(cached, dict):
        secrets.extend(str(cached.get(key, '')) for key in ('token', 'userId'))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'https?://[^\s<>\"\']+', '[链接已隐藏]', text, flags=re.I)
    text = re.sub(r'(?i)\b(?:token|cookie|password|authorization|ticket|userId)\b[\"\']?\s*[=:]\s*[\"\']?[^\s,;，；\"\'}]+',
                  '[凭据已隐藏]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
    text = re.sub(r'[A-Za-z0-9_+/=-]{24,}', '[长标识已隐藏]', text)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text)[:300]


def _telecom_dict(value):
    # Some versions serialize responseData, data, or result a second time.
    for _ in range(3):
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            break
    return {}


def _telecom_decode(response):
    raw = response.content
    if not isinstance(raw, bytes):
        raise ValueError('响应不是字节数据')
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('响应过大')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError('响应字符编码无法识别')


def _telecom_json(response):
    try:
        parsed = json.loads(_telecom_decode(response))
    except (ValueError, TypeError, UnicodeError):
        return {}, '响应不是有效 JSON 对象（可能为空、HTML 或编码异常）'
    if not isinstance(parsed, dict):
        return {}, '响应 JSON 顶层不是对象'
    result = dict(parsed)
    for key in ('headerInfos', 'responseData', 'data', 'biz'):
        if key in result:
            result[key] = _telecom_dict(result[key])
    response_data = result.get('responseData', {})
    if 'data' in response_data:
        response_data['data'] = _telecom_dict(response_data['data'])
    return result, ''


def _telecom_details(payload, phone, password):
    result = {}
    header = _telecom_dict(payload.get('headerInfos'))
    response_data = _telecom_dict(payload.get('responseData'))
    data = _telecom_dict(response_data.get('data')) or _telecom_dict(payload.get('data'))
    selected = [('headerInfos', header, ('code', 'reason', 'resultCode', 'resultDesc')),
                ('responseData', response_data, ('resultCode', 'resultDesc', 'code', 'msg', 'message')),
                ('data', data, ('resultCode', 'resultDesc', 'code', 'msg', 'message')),
                ('response', payload, ('code', 'msg', 'message', 'resultCode', 'resultDesc'))]
    for parent_name, parent in [('data', data), ('responseData', response_data), ('response', payload)]:
        if 'loginFailResult' in parent:
            failure = parent['loginFailResult']
            nested = _telecom_dict(failure)
            if nested:
                selected.append((parent_name + '.loginFailResult', nested,
                                 ('code', 'reason', 'resultCode', 'resultDesc', 'msg', 'message',
                                  'loginFailCode', 'loginFailMsg', 'errorCode', 'errorMsg')))
            elif isinstance(failure, (str, int)) and not isinstance(failure, bool):
                result[parent_name + '.loginFailResult'] = _telecom_redact(failure, phone, password)
    for prefix, value, keys in selected:
        for key in keys:
            item = value.get(key)
            if isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item):
                result[prefix + '.' + key] = _telecom_redact(item, phone, password)
    return result


def _telecom_structure(payload, phone, password):
    # Only field names and types, with both depth and width bounded.
    result = {}
    def walk(value, prefix, depth):
        if not isinstance(value, dict) or depth > 3:
            return
        for name, item in list(value.items())[:24]:
            safe = str(name) if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,47}', str(name)) else '[其他字段]'
            safe = _telecom_redact(safe, phone, password)
            path = prefix + '.' + safe if prefix else safe
            result[path] = type(item).__name__
            walk(item, path, depth + 1)
    walk(payload, '', 0)
    return result


def _telecom_record(phone, password, stage, ok, message, status=None, payload=None):
    message = _telecom_redact(message, phone, password)
    summary = {'stage': stage, 'ok': bool(ok), 'message': message,
               'password_attempts': TELECOM_PASSWORD_ATTEMPTS.get(phone, 0)}
    TELECOM_AUTH_RESULTS[phone] = summary
    diagnostic = {'version': 1, 'at': _telecom_timestamp(), **summary}
    if isinstance(status, int):
        diagnostic['http_status'] = status
    if isinstance(payload, dict):
        diagnostic['business_fields'] = _telecom_details(payload, phone, password)
        diagnostic['structure'] = _telecom_structure(payload, phone, password)
    target = Path(TELECOM_CACHE_FILE).parent / (hashlib.sha256(str(phone).encode()).hexdigest()[:16]
                                               + '.telecom-diagnostic.json')
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=str(target.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(diagnostic, handle, ensure_ascii=False, separators=(',', ':'))
        if os.name != 'nt':
            Path(temporary).chmod(0o600)
        os.replace(temporary, target)
    except OSError:
        telecom_log(f'[登录诊断] {mask(phone)} 本地诊断文件无法写入')
    finally:
        try:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass  # Diagnostic cleanup must not turn a completed login into a failure.
    telecom_log(f'[电信{stage}] {mask(phone)} {message}')
    return summary


def _telecom_fail(phone, password, stage, message, status=None, payload=None):
    details = _telecom_details(payload or {}, phone, password)
    if details:
        def priority(item):
            key = item[0]
            if 'loginFailResult' in key:
                return (0, 0 if key.lower().endswith('code') else 1)
            if key.endswith('resultDesc'):
                return (1, 0)
            if key.endswith('resultCode'):
                return (2, 0)
            return (3, 0)
        message += '；' + '；'.join(key + '=' + value[:100] for key, value in
                                  sorted(details.items(), key=priority))
    _telecom_record(phone, password, stage, False, message, status, payload)
    return None


def _telecom_valid_login(value):
    return (isinstance(value, dict) and isinstance(value.get('token'), str)
            and bool(value['token'].strip()) and isinstance(value.get('userId'), (str, int))
            and not isinstance(value.get('userId'), bool) and bool(str(value['userId']).strip()))


TELECOM_LOGIN_RETRIES = 3

def _telecom_retry_delay(attempt):
    """登录网络类重试延迟：2/4/8 秒递增，±30%抖动，避免同拍重放。"""
    return round(2 * (2 ** attempt) * random.uniform(0.7, 1.3), 1)


def _telecom_save_cache():
    try:
        save_cache()
    except OSError:
        telecom_log('[电信缓存] 无法保存缓存，本次已获取的登录态仍可使用')


def _telecom_password_login(phone, password, android_id=''):
    if TELECOM_PASSWORD_ATTEMPTS.get(phone, 0):
        previous = TELECOM_AUTH_RESULTS.get(phone, {}).get('message', '')
        return _telecom_fail(phone, password, 'password', '本轮已尝试密码登录，不再重复请求' +
                             ('；上次：' + previous if previous else ''))
    if not password:
        return _telecom_fail(phone, password, 'password', '没有可用缓存且未提供电信密码')
    stamp = _telecom_timestamp()
    android_id = str(android_id or '')
    encoded_android_id = ''.join(chr(ord(c) + 2) for c in android_id) if android_id else ''
    use_android = bool(re.fullmatch(r'[0-9a-fA-F]{16}', android_id)) if android_id else False
    client_type = TELECOM_CLIENT_TYPES.get(phone, '')
    if android_id and not use_android:
        telecom_log(f'[登录] {mask(phone)} AndroidID格式有误（需16位十六进制），本次按无设备指纹登录')
    if use_android and client_type:
        # 与网龄脚本同源：网站导出的 clientType + encoded androidId，服务端可识别受信设备。
        cipher_head = f'Xiaomi 20 8.0.0.{android_id[:12]}'
        system_version = '8.0.0'
        device_uid = ''
        is_chinatelecom = ''
        fingerprint_note = '，携带真实设备指纹'
    elif use_android:
        client_type = '#11.0.0#channel8#Xiaomi 20#'
        cipher_head = f'Xiaomi 20 8.0.0.{android_id[:12]}'
        system_version = '8.0.0'
        device_uid = ''
        is_chinatelecom = ''
        fingerprint_note = '，携带真实设备指纹'
    else:
        # Preserve the iOS profile; header/RSA time and both device fields must agree.
        device_id = rd_str(16)
        client_type = '#10.5.0#channel50#iPhone 14 Pro Max#'
        cipher_head = f'iPhone 14 15.4.{device_id[:12]}'
        system_version = '15.4.0'
        device_uid = device_id
        is_chinatelecom = '0'
        fingerprint_note = ''

    fields = {'loginType': '4', 'accountType': '',
              'loginAuthCipherAsymmertric': encrypt_rsa(
                  f'{cipher_head}{phone}{stamp}{password}0$$$0.', 'login', 'b64'),
              'deviceUid': device_uid, 'phoneNum': encode(phone),
              'isChinatelecom': is_chinatelecom, 'systemVersion': system_version}
    if use_android:
        fields['androidId'] = encoded_android_id
        fields['loginAuthCipher'] = ''
    fields['authentication'] = encode(password)
    body = {'headerInfos': {'code': 'userLoginNormal', 'timestamp': stamp, 'broadAccount': '',
                            'broadToken': '', 'clientType': client_type,
                            'shopId': '20002', 'source': '110003', 'sourcePassword': 'Sid98s',
                            'token': '', 'userLoginName': encode(phone)},
            'content': {'attach': 'test', 'fieldData': fields}}
    TELECOM_PASSWORD_ATTEMPTS[phone] = TELECOM_PASSWORD_ATTEMPTS.get(phone, 0) + 1
    telecom_log(f'[登录] {mask(phone)} 正在请求新 Token（本轮最多一次{fingerprint_note}）')
    response = None
    failure = ''
    for attempt in range(TELECOM_LOGIN_RETRIES + 1):
        try:
            response = telecom_session.request('POST', 'https://appgologin.189.cn:9031/login/client/userLoginNormal',
                                               json=body, timeout=15, allow_redirects=False)
        except Exception:
            response, failure = None, '密码登录请求未完成（网络异常）'
        else:
            code = response.status_code
            if isinstance(code, int) and 500 <= code <= 599:
                response, failure = None, f'密码登录 HTTP {code}（服务端网关异常）'
            else:
                failure = ''
        if response is not None or attempt >= TELECOM_LOGIN_RETRIES:
            break
        delay = _telecom_retry_delay(attempt)
        telecom_log(f'[登录] {mask(phone)} {failure}；{delay} 秒后重试（{attempt + 1}/{TELECOM_LOGIN_RETRIES}）')
        time.sleep(delay)
    if response is None:
        return _telecom_fail(phone, password, 'password', failure + '；已重试' + str(TELECOM_LOGIN_RETRIES) + '次仍未成功')
    status = response.status_code
    payload, parse_error = _telecom_json(response)
    if not isinstance(status, int) or not 200 <= status < 300:
        return _telecom_fail(phone, password, 'password', f'密码登录 HTTP {status}，未取得登录态', status, payload)
    if parse_error:
        return _telecom_fail(phone, password, 'password', parse_error, status, payload)
    response_data = _telecom_dict(payload.get('responseData'))
    data = _telecom_dict(response_data.get('data')) or _telecom_dict(payload.get('data'))
    result = _telecom_dict(data.get('loginSuccessResult'))
    if not _telecom_valid_login(result):
        return _telecom_fail(phone, password, 'password',
                             '未取得有效登录态：缺少 responseData.data.loginSuccessResult.token/userId', status, payload)
    telecom_cache[phone] = result
    _telecom_save_cache()
    _telecom_record(phone, password, 'password', True, '密码登录响应已确认有效', status)
    return result


def _telecom_ticket_xml(phone, cached):
    root = _telecom_et.Element('Request')
    header = _telecom_et.SubElement(root, 'HeaderInfos')
    values = {'Code': 'getSingle', 'Timestamp': _telecom_timestamp(), 'BroadAccount': '', 'BroadToken': '',
              'ClientType': TELECOM_CLIENT_TYPES.get(phone, '#9.6.1#channel50#iPhone 14 Pro Max#'), 'ShopId': '20002', 'Source': '110003',
              'SourcePassword': 'Sid98s', 'Token': cached['token'], 'UserLoginName': encode(phone)}
    for key, value in values.items():
        _telecom_et.SubElement(header, key).text = str(value)
    content = _telecom_et.SubElement(root, 'Content')
    _telecom_et.SubElement(content, 'Attach').text = 'test'
    fields = _telecom_et.SubElement(content, 'FieldData')
    _telecom_et.SubElement(fields, 'TargetId').text = encrypt_des3(str(cached['userId']))
    _telecom_et.SubElement(fields, 'Url').text = '4a6862274835b451'
    return _telecom_et.tostring(root, encoding='utf-8')


def _telecom_ticket_once(phone, password, cached):
    """单次获取 Ticket；返回 (uid, expired, retry_reason)。

    retry_reason 非 None 表示网络类失败（请求异常/5xx/响应格式异常），可安全重试；
    业务类失败（明确拒绝、Token 过期、解密失败）当场记录，返回 None 表示不重试。
    """
    try:
        response = telecom_session.request('POST', 'https://appgologin.189.cn:9031/map/clientXML',
                                           data=_telecom_ticket_xml(phone, cached),
                                           headers={'Content-Type': 'application/xml; charset=UTF-8'},
                                           timeout=15, allow_redirects=False)
    except Exception:
        return None, False, '获取 Ticket 请求未完成（网络或请求构造异常）'
    status = response.status_code
    if isinstance(status, int) and 500 <= status <= 599:
        return None, False, f'获取 Ticket HTTP {status}（服务端网关异常）'
    if not isinstance(status, int) or not 200 <= status < 300:
        _telecom_fail(phone, password, 'ticket', f'获取 Ticket HTTP {status}', status)
        return None, False, None
    try:
        text = _telecom_decode(response)
    except (ValueError, TypeError, UnicodeError):
        return None, False, f'获取 Ticket 响应编码无法识别（HTTP {status}）'
    try:
        root = _telecom_et.fromstring(text)
    except _telecom_et.ParseError:
        # The service sometimes returns a short plaintext expiry notice, not XML.
        # Only explicit token/令牌 expiry text qualifies; HTML and other bodies do not.
        plain_expired = (len(text) <= 240 and not any(char in text for char in '<>{}') and
                         bool(re.search(r'(?:token|令牌).{0,16}(?:过期|失效|expired|invalid)|'
                                        r'(?:expired|invalid)\s+token', text, re.I)))
        if plain_expired:
            _telecom_fail(phone, password, 'ticket', '获取 Ticket 返回明确的 Token 过期或失效提示', status)
            return None, True, None
        return None, False, f'获取 Ticket 响应不是有效 XML（HTTP {status}）'
    if root.tag.rsplit('}', 1)[-1].lower() == 'html':
        return None, False, f'获取 Ticket 返回 HTML 页面，未取得票据（HTTP {status}）'
    ticket, messages = '', {}
    for node in root.iter():
        name = node.tag.rsplit('}', 1)[-1]
        if name.lower() == 'ticket':
            ticket = (node.text or '').strip()
        elif name.lower() in ('code', 'reason', 'resultcode', 'resultdesc', 'message', 'msg'):
            messages[name] = node.text or ''
    failure_text = ' '.join(messages.values()) or ' '.join(root.itertext())
    expired = bool(re.search(r'过期|失效|校验错误|(?:token|令牌).{0,20}(?:无效|错误)|'
                             r'(?:token\s*(?:expired|invalid))|(?:(?:expired|invalid)\s*token)',
                             failure_text, re.I))
    if not ticket:
        detail = '；'.join(key + '=' + _telecom_redact(value, phone, password) for key, value in messages.items())
        _telecom_fail(phone, password, 'ticket', '未取得 Ticket' + ('；' + detail if detail else ''), status)
        return None, expired, None
    try:
        uid = encrypt_des3(ticket, 'dec')
    except Exception:
        _telecom_fail(phone, password, 'ticket', 'Ticket 解密失败，未取得可用登录票据', status)
        return None, False, None
    if not isinstance(uid, str) or not uid.strip():
        _telecom_fail(phone, password, 'ticket', 'Ticket 解密结果为空', status)
        return None, False, None
    return uid, False, None


def _telecom_ticket(phone, password, cached):
    """获取 Ticket：网络类失败自动重试（最多3次，2/4/8秒递增+抖动），业务类失败不重试。"""
    uid = expired = None
    for attempt in range(TELECOM_LOGIN_RETRIES + 1):
        uid, expired, retry_reason = _telecom_ticket_once(phone, password, cached)
        if retry_reason is None:
            return uid, expired
        if attempt >= TELECOM_LOGIN_RETRIES:
            _telecom_fail(phone, password, 'ticket', retry_reason + '；已重试' + str(TELECOM_LOGIN_RETRIES) + '次仍未成功')
            return uid, expired
        delay = _telecom_retry_delay(attempt)
        telecom_log(f'[电信ticket] {mask(phone)} {retry_reason}；{delay} 秒后重试（{attempt + 1}/{TELECOM_LOGIN_RETRIES}）')
        time.sleep(delay)
    return uid, expired


def login(phone, password, android_id=''):
    cached = telecom_cache.get(phone)
    used_cache = _telecom_valid_login(cached)
    if not used_cache:
        cached = _telecom_password_login(phone, password, android_id)
        if cached is None:
            return None
    uid, expired = _telecom_ticket(phone, password, cached)
    # An explicit expired-cache response permits one password request; never recurse.
    if uid is None and expired and used_cache and not TELECOM_PASSWORD_ATTEMPTS.get(phone, 0):
        telecom_cache.pop(phone, None)
        _telecom_save_cache()
        cached = _telecom_password_login(phone, password, android_id)
        if cached is None:
            return None
        uid, _ = _telecom_ticket(phone, password, cached)
    if uid is None:
        return None
    user = {**cached, 'uid': uid, 'phoneNbr': phone}
    _telecom_record(phone, password, 'ticket', True, '电信 Ticket 已取得')
    return user



def ql_import_legacy_telecom_cache(phones):
    """Copy eligible configured identities from the original script's Cache.json.

    Called while holding the telecom state lock. Existing valid state wins;
    the legacy file stays unchanged and only token/userId are carried forward.
    """
    if not isinstance(telecom_cache, dict):
        return 0

    def valid_for_phone(record, phone):
        return (_telecom_valid_login(record) and
                ("phoneNbr" not in record or str(record["phoneNbr"]) == phone))

    wanted = {str(phone) for phone in phones if re.fullmatch(r"1[3-9]\d{9}", str(phone))}
    wanted = {phone for phone in wanted if not valid_for_phone(telecom_cache.get(phone), phone)}
    if not wanted:
        return 0
    source = Path(__file__).resolve().parent / "Cache.json"
    try:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
            return 0
        with source.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return 0
        legacy = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(legacy, dict):
            return 0
    except (OSError, ValueError, TypeError, UnicodeError):
        ql_log("旧电信 Cache.json 无法读取，已跳过自动迁移")
        return 0
    imported = 0
    for phone in wanted:
        record = legacy.get(phone)
        if valid_for_phone(record, phone):
            telecom_cache[phone] = {"token": record["token"], "userId": record["userId"]}
            imported += 1
    if imported:
        try:
            _telecom_save_cache()
        except (OSError, ValueError, TypeError):
            ql_log("迁移的电信缓存无法保存，本轮仍可使用已读取的登录态")
        ql_log(f"已从脚本同目录 Cache.json 迁移 {imported} 个已配置账号的电信登录缓存", "OK")
    return imported


def _fresh_telecom_ticket(phone, password, state_dir, android_id="", auth_failures=None):
    """Return one fresh telecom ticket for an existing AI account session."""
    user, _ = _ql_telecom_user(phone, password, state_dir, android_id=android_id)
    if not isinstance(user, dict) or str(user.get("phoneNbr", "")) != phone or not user.get("uid"):
        raise ImusicError("电信未返回可用登录票据，未能重建活动会话")
    return str(user["uid"])


def _ql_try_imusic(phone, ticket, state_dir=None):
    """Return a live verified client; try cached activity token before the ticket."""
    client = ImusicClient(phone=phone, state_dir=state_dir)
    try:
        if client.token:
            try:
                client.refresh_login()
            except (ImusicError, ImusicApiError, ValueError, TypeError, KeyError,
                    requests.RequestException):
                client.close()
                client = ImusicClient(phone=phone, state_dir=state_dir, use_token_cache=False)
            else:
                if client.identity_verified and str(client.phone) == phone:
                    return client
                raise ImusicError("活动账号身份校验未完成")
        client.refresh_login(ticket=ticket)
        if not client.identity_verified or str(client.phone) != phone:
            raise ImusicError("活动账号身份校验未完成")
        return client
    except BaseException:
        client.close()
        raise


def _ql_telecom_user(phone, password, state_dir, force_login=False, allow_password=True, android_id=""):
    """Run the legacy authentication only in an isolated, bounded context.

    Its getSingle retry recurses through the global login name. The guard allows
    cached-token authentication and at most one password login, including that
    recursion. No sign, draw, task, or other participation call is made here.
    android_id optionally carries the account's real AndroidID so the password
    login presents a trusted device fingerprint instead of a random one.
    Returns (legacy_user_or_None, password_login_was_attempted).
    """
    global TELECOM_CACHE_FILE, telecom_cache, telecom_session, login, telecom_log
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        state_dir.chmod(0o700)
    used_password = False
    password_attempts_before = TELECOM_PASSWORD_ATTEMPTS.get(phone, 0)
    with account_lock(state_dir / "telecom.lock"):
        saved = (TELECOM_CACHE_FILE, telecom_cache, telecom_session, login, telecom_log)
        isolated = None
        try:
            TELECOM_CACHE_FILE = state_dir / "telecom_cache.json"
            telecom_cache = {}
            try:
                load_cache()
                if not isinstance(telecom_cache, dict):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise ImusicError("电信缓存损坏或不可读，请检查持久状态目录") from None
            ql_import_legacy_telecom_cache([phone])
            if force_login:
                telecom_cache.pop(phone, None)
            record = telecom_cache.get(phone)
            if record is not None and (not isinstance(record, dict) or not record.get("token") or not record.get("userId")):
                telecom_cache.pop(phone, None)
            # Never reuse another account's cookie jar or leave this context's
            # globals installed after success, failure, or an interrupt.
            isolated = requests.Session()
            telecom_session = isolated
            isolated.trust_env = os.getenv("IMUSIC_TRUST_ENV", "0") == "1"
            isolated.verify = certifi.where()
            isolated.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Mobile Safari/537.36"})
            isolated.mount("https://", CustomSSLAdapter())
            original_login = saved[3]
            # Legacy responses can contain identifiers. Authentication outcomes
            # are reported by the caller using fixed, credential-free messages.
            telecom_log = lambda message: None
            calls = 0

            def bounded_login(current_phone, current_password, current_android_id=""):
                nonlocal used_password, calls
                calls += 1
                if calls > 2 or str(current_phone) != phone:
                    return None
                if phone not in telecom_cache:
                    if not allow_password or used_password or not current_password:
                        return None
                    used_password = True
                return original_login(current_phone, current_password, current_android_id)

            login = bounded_login
            user = bounded_login(phone, password, android_id)
            used_password = used_password or TELECOM_PASSWORD_ATTEMPTS.get(phone, 0) > password_attempts_before
            if not isinstance(user, dict) or str(user.get("phoneNbr", "")) != phone or not user.get("uid"):
                return None, used_password
            return user, used_password
        finally:
            try:
                if isolated is not None:
                    isolated.close()
                if TELECOM_CACHE_FILE.exists() and os.name != "nt":
                    TELECOM_CACHE_FILE.chmod(0o600)
            finally:
                TELECOM_CACHE_FILE, telecom_cache, telecom_session, login, telecom_log = saved


def ql_authenticate(phone, password="", legacy_user=None, state_dir=None,
                    telecom_failure=None, android_id=""):
    """Authenticate one account via the telecom ticket SSO chain.

    Order: the ticket from this run's telecom login; the persistent telecom
    token; at most one password login. android_id (the account's real AndroidID)
    only affects that final password login. Call inside the account lock.
    Success is determined solely by SSO returning the requested phone.
    """
    phone = str(phone)
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise ImusicError("账号手机号格式有误")
    if state_dir is None:
        state_dir = ql_state_dir()
    if telecom_failure:
        raise ImusicError("本轮电信认证已失败，跳过重复密码请求。原因：" + str(telecom_failure))
    tried_legacy = False
    if legacy_user is not None:
        if not isinstance(legacy_user, dict) or str(legacy_user.get("phoneNbr", "")) != phone:
            raise ImusicError("电信登录账号与目标账号不一致")
        if legacy_user.get("uid"):
            tried_legacy = True
            ql_log("开始电信自动登录衔接：尝试当前账号的电信票据")
            try:
                client = _ql_try_imusic(phone, legacy_user["uid"], state_dir)
            except (ImusicError, ValueError, TypeError, KeyError, requests.RequestException):
                pass
            else:
                ql_log("电信Ticket单点登录成功")
                return client

    # A supplied ticket has already failed; only a fresh password login can add
    # information. Without it, try the persistent telecom token before a refresh.
    password_used = False
    ticket_tested = tried_legacy
    can_try_telecom = bool(password) or (Path(state_dir) / "telecom_cache.json").is_file()
    if can_try_telecom and (not tried_legacy or password):
        ql_log("开始电信自动登录衔接：获取当前账号的登录票据")
        user, password_used = _ql_telecom_user(phone, password, state_dir, force_login=tried_legacy,
                                                android_id=android_id)
        if user:
            ticket_tested = True
            try:
                client = _ql_try_imusic(phone, user["uid"], state_dir)
            except (ImusicError, ValueError, TypeError, KeyError, requests.RequestException):
                pass
            else:
                ql_log("电信Ticket单点登录成功")
                return client
        if password and not password_used and not TELECOM_PASSWORD_ATTEMPTS.get(phone):
            user, password_used = _ql_telecom_user(phone, password, state_dir, force_login=True,
                                                    android_id=android_id)
            if user:
                ticket_tested = True
                try:
                    client = _ql_try_imusic(phone, user["uid"], state_dir)
                except (ImusicError, ValueError, TypeError, KeyError, requests.RequestException):
                    pass
                else:
                    ql_log("电信Ticket单点登录成功")
                    return client
    if not ticket_tested:
        result = TELECOM_AUTH_RESULTS.get(phone, {})
        reason = result.get("message") or "电信未返回有效登录票据"
        raise ImusicError("电信认证未完成。原因：" + reason)
    raise ImusicError("已取得电信票据，但活动身份衔接尚未通过校验；需根据登录响应继续排查")

# ===== 青龙环境变量与入口 =====
"""青龙单文件构建入口；构建后随业务源码内联。"""
import math

QL_LOGS = []
QL_BUILD_VERSION = "2026.10.16-dxai-notify-default1"


def ql_log(message, level="INFO"):
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: m[0][:3] + "****" + m[0][-4:], str(message))
    marks = {"INFO": "ℹ️", "OK": "✅", "WARN": "⚠️", "ERROR": "❌", "STEP": "▶️", "SUMMARY": "📊"}
    mark = marks.get(level, "ℹ️")
    line = f"[{datetime.now(CST):%m-%d %H:%M:%S}] {mark} {text}"
    QL_LOGS.append(line)
    print(line, flush=True)


def ql_section(title):
    ql_log(title, "STEP")


def ql_notify_body():
    logs = list(QL_LOGS)
    # Keep emoji status but drop timestamps and separators in notifications.
    compact = "\n".join(
        re.sub(r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] [^\S\n]*", "", line).strip()
        for line in logs
    )
    compact = re.sub(r"(?m)^(?:[ℹ️✅⚠️❌▶️📊]\s*)?\s*$\n?", "", compact)
    header = f"🎮 电信AI奇遇\n🧾 版本：{QL_BUILD_VERSION}\n🕒 时间：{datetime.now(CST):%Y-%m-%d %H:%M:%S}\n──────────────"
    return header + "\n" + compact[-12000:] if compact else "\n".join(logs)[-12000:]


def env_bool(name, default=False):
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value not in {"0", "1", "true", "false"}:
        raise ImusicError(f"{name} 只能填写0或1")
    return value in {"1", "true"}


def env_number(name, default, minimum, maximum, floating=False):
    try:
        value = (float if floating else int)(os.getenv(name, str(default)))
    except ValueError:
        raise ImusicError(f"{name} 必须是数字") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ImusicError(f"{name} 超出允许范围 {minimum}~{maximum}")
    return value


def ql_state_dir():
    explicit = os.getenv("IMUSIC_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    for base in (Path("/ql/data/config"), Path("/ql/config")):
        if base.is_dir():
            return base / "telecom_ai119"
    return Path(__file__).resolve().parent / ".telecom_ai119"




TELECOM_ANDROID_IDS = {}
TELECOM_CLIENT_TYPES = {}


def read_telecom_accounts():
    """Read accounts from the same TELEFEN_AUTH format as dxgy.py."""
    result = {}
    TELECOM_ANDROID_IDS.clear()
    TELECOM_CLIENT_TYPES.clear()
    pattern = re.compile(
        r"(?P<remark>[^#\r\n]+)#(?P<phone>1[3-9]\d{9})#(?P<password>[^#\r\n]+)"
        r"#(?P<client_type>#[^#\r\n]+#[^#\r\n]+#[^#\r\n]+#)"
        r"#(?P<encoded_id>[^#\r\n]+)"
    )
    for row in os.getenv("TELEFEN_AUTH", "").replace("\r", "").replace("\n", "&").split("&"):
        if not row.strip():
            continue
        match = pattern.fullmatch(row.strip())
        if not match:
            raise ImusicError("TELEFEN_AUTH格式错误：应为备注#手机号#服务密码#clientType#androidId")
        values = match.groupdict()
        android_id = "".join(chr(ord(c) - 2) for c in values["encoded_id"])
        if not re.fullmatch(r"[0-9a-f]{16}", android_id):
            raise ImusicError("TELEFEN_AUTH androidId不是网站导出的编码值")
        phone = values["phone"]
        if phone in result:
            raise ImusicError("TELEFEN_AUTH存在重复手机号")
        result[phone] = values["password"]
        TELECOM_ANDROID_IDS[phone] = android_id
        TELECOM_CLIENT_TYPES[phone] = values["client_type"]
    return result


def ql_options(execute):
    options = parser().parse_args([])
    options.execute = execute
    options.mode = os.getenv("IMUSIC_MODE", "save").strip().lower()
    if options.mode not in {"save", "auto", "draw", "observe"}:
        raise ImusicError("IMUSIC_MODE只能填写save（只攒分，默认）、auto（极速满额）、draw（全抽奖）或observe")
    if options.mode == "observe":
        options.execute = False
    options.make_count = env_number("IMUSIC_MAKE_COUNT", 17, 0, 17)
    options.cooldown_hours = env_number("IMUSIC_COOLDOWN_HOURS", 0, 0, 8760, True)
    options.make_interval = env_number("IMUSIC_MAKE_INTERVAL", 20, 0, 300)
    options.max_draws = env_number("IMUSIC_MAX_DRAWS", 10, 0, 50)
    options.reserve_points = env_number("IMUSIC_RESERVE_POINTS", 0 if options.mode in {"auto", "draw"} else 1000, 0, 100000000)
    options.template = os.getenv("IMUSIC_TEMPLATE", "auto").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", options.template):
        raise ImusicError("IMUSIC_TEMPLATE模板编号格式有误")
    options.invite_code = os.getenv("IMUSIC_INVITE_CODE", DEFAULT_INVITE_CODE).strip()
    if options.invite_code and not re.fullmatch(r"[A-Za-z0-9]{32,128}", options.invite_code):
        raise ImusicError("IMUSIC_INVITE_CODE格式有误：应为32位以上的字母数字串，或留空关闭")
    options.state_dir = ql_state_dir()
    return options


def ql_notify(enabled):
    if not enabled:
        return
    try:
        from notify import send
    except ImportError:
        ql_log("未找到青龙notify模块，本轮结果保留在任务日志", "WARN")
        return
    except Exception:
        ql_log("青龙notify模块加载异常，本轮结果保留在任务日志", "WARN")
        return
    body = ql_notify_body()
    try:
        send("🎮 电信AI奇遇", body)
    except Exception:
        ql_log("通知未发送成功，请查看青龙通知配置；任务结果不受影响", "WARN")
    else:
        ql_log("青龙通知已调用，发送结果以通知模块日志为准", "OK")


def ql_run_telecom(accounts, execute, state_dir, auth_users=None, auth_failures=None):
    """仅为AI奇遇做电信自动登录衔接（取Ticket供活动SSO登录），不再执行原电信任务。"""
    global TELECOM_CACHE_FILE, telecom_cache, telecom_session
    errors = 0
    if not accounts or not execute:
        return 0
    try:
        with account_lock(state_dir / "telecom.lock"):
            TELECOM_CACHE_FILE = state_dir / "telecom_cache.json"
            telecom_cache = {}
            try:
                load_cache()
                if not isinstance(telecom_cache, dict):
                    raise ValueError()
            except (OSError, ValueError, TypeError):
                raise ImusicError("电信缓存损坏或不可读，请检查持久状态目录") from None
            ql_import_legacy_telecom_cache(accounts)
            for phone, password in accounts.items():
                try:
                    telecom_session.close()
                    telecom_session = requests.Session()
                    telecom_session.trust_env = os.getenv("IMUSIC_TRUST_ENV", "0") == "1"
                    telecom_session.verify = certifi.where()
                    telecom_session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Mobile Safari/537.36"})
                    telecom_session.mount("https://", CustomSSLAdapter())
                    user = login(phone, password, TELECOM_ANDROID_IDS.get(phone, ""))
                    if user:
                        if auth_users is not None and str(user.get("phoneNbr")) == phone:
                            auth_users[phone] = user
                    else:
                        errors += 1
                        if auth_failures is not None:
                            auth_failures[phone] = TELECOM_AUTH_RESULTS.get(phone, {}).get("message") or "电信登录未返回有效票据"
                except Exception:
                    errors += 1
                    if auth_failures is not None and (auth_users is None or phone not in auth_users):
                        auth_failures[phone] = TELECOM_AUTH_RESULTS.get(phone, {}).get("message") or "电信认证过程异常"
                finally:
                    telecom_session.close()
            if TELECOM_CACHE_FILE.exists() and os.name != "nt":
                TELECOM_CACHE_FILE.chmod(0o600)
    except (ImusicError, OSError) as exc:
        errors += 1
        ql_log(str(exc) if isinstance(exc, ImusicError) else "电信状态目录不可写")
    return errors


def ql_main(argv=None):
    global log, telecom_log
    QL_LOGS.clear()
    TELECOM_AUTH_RESULTS.clear()
    TELECOM_PASSWORD_ATTEMPTS.clear()
    log = telecom_log = ql_log
    cli = argparse.ArgumentParser(description="青龙单文件：电信AI奇遇。默认执行参与并推送通知；QL_EXECUTE=0或--dry-run只查询，QL_NOTIFY=0关闭通知。账号使用TELEFEN_AUTH。")
    cli.add_argument("--dry-run", action="store_true", help="强制只查询，覆盖QL_EXECUTE")
    cli.add_argument("--clear-uncertain", metavar="手机号", help="手工核对活动记录后解除该账号未知结果锁，本轮不参与")
    args = cli.parse_args(argv)
    errors, notify_enabled = 0, True
    try:
        # 通知默认开启；仅 QL_NOTIFY=0/false 可显式关闭。
        notify_enabled = env_bool("QL_NOTIFY", True)
        execute = env_bool("QL_EXECUTE", True) and not args.dry_run and not args.clear_uncertain
        # Parse every enabled component before performing any requests.
        telecom_accounts = read_telecom_accounts()
        options = ql_options(execute)
        ai_phones = set(telecom_accounts)
        if args.clear_uncertain:
            if args.clear_uncertain not in ai_phones:
                raise ImusicError("待解除锁的手机号未配置账号")
            ai_phones = {args.clear_uncertain}
            options.clear_uncertain = True
        if not ai_phones:
            raise ImusicError("没有配置账号：请设置TELEFEN_AUTH=备注#手机号#服务密码#clientType#androidId，多账号用&分隔")
        options.state_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            options.state_dir.chmod(0o700)
        ql_section("任务启动")
        ql_log("脚本版本：" + QL_BUILD_VERSION)
        mode_names = {"save": "只攒分（默认）", "auto": "极速满额", "draw": "全抽奖", "observe": "只查询"}
        ql_log(f"启动：{'执行参与' if execute else '只查询'}；AI策略 {options.mode}·{mode_names[options.mode]}；AI待登录 {len(ai_phones)} 个")
        ql_section("运行配置")
        if options.mode == "save":
            ql_log("只攒分：不抽奖；满1000立即兑10元；兑换/缺货的当月状态跨重启保存，次月自动重试")
        if options.mode == "auto":
            ql_log("极速满额：每期做满340点；期末免费额度用光才抽奖补AI体验券凑满17件；满1000立即兑10元，兑后自动转全抽奖；最后一期攒不满时自动抽光余额")
        if options.mode == "draw":
            ql_log("全抽奖：不攒兑换，每轮把可抽余额全部投入抽奖（默认不保留1000点）")
        interval_note = f"，相邻制作间隔 {options.make_interval} 秒" if options.make_interval else ""
        cooldown_note = f"，额外等待 {options.cooldown_hours:g} 小时" if options.cooldown_hours else ""
        ql_log(f"制作计划：每轮上限 {options.make_count}{interval_note}{cooldown_note}；按实时额度及本期340点上限计算")
        ql_section("账号执行")
        if TELECOM_ANDROID_IDS:
            ql_log(f"设备指纹：{len(TELECOM_ANDROID_IDS)} 个账号的密码登录将携带真实AndroidID（全新环境可避开3006风控）")
        auth_users, auth_failures = {}, {}
        errors += ql_run_telecom(telecom_accounts, execute, options.state_dir, auth_users, auth_failures)
        # 执行顺序与登录顺序一致：按chinaTelecomAccount配置顺序（即电信Ticket登录顺序）
        ordered_phones = [phone for phone in telecom_accounts if phone in ai_phones]
        for phone in ordered_phones:
            ql_log(f"AI账号 {mask(phone)}")
            ql_section(f"账号 {mask(phone)}")
            client = None
            try:
                identity = hashlib.sha256(phone.encode()).hexdigest()[:16]
                with account_lock(options.state_dir / (identity + ".lock")):
                    client = ql_authenticate(phone, password=telecom_accounts.get(phone, ""),
                                             legacy_user=auth_users.get(phone),
                                             android_id=TELECOM_ANDROID_IDS.get(phone, ""),
                                             state_dir=options.state_dir,
                                             telecom_failure=auth_failures.get(phone))
                    client.ticket_provider = lambda: _fresh_telecom_ticket(
                        phone, telecom_accounts.get(phone, ""), options.state_dir,
                        TELECOM_ANDROID_IDS.get(phone, ""), auth_failures)
                    client.invite_code = options.invite_code
                    Activity(client, options, options.state_dir / (identity + ".json")).run()
            except (ImusicError, ValueError, KeyError, TypeError, OSError):
                errors += 1
                # Never print server text or credential-bearing transport errors.
                exc = sys.exc_info()[1]
                ql_log(str(exc), "ERROR") if isinstance(exc, ImusicError) else ql_log("账号配置、状态或接口结构异常，停止本账号并继续", "ERROR")
            finally:
                if client:
                    client.close()
        ql_section("任务汇总")
        ql_log(f"任务结束：{'全部成功' if not errors else str(errors) + ' 项失败'}", "OK" if not errors else "ERROR")
        ql_log(f"账号数量：{len(ordered_phones)}；失败项：{errors}；通知：{'已启用' if notify_enabled else '未启用'}", "SUMMARY")
    except (ImusicError, ValueError, TypeError, OSError):
        errors += 1
        exc = sys.exc_info()[1]
        ql_log(str(exc), "ERROR") if isinstance(exc, ImusicError) else ql_log("配置读取失败：请核对JSON格式、路径与权限", "ERROR")
    finally:
        ql_notify(notify_enabled)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(ql_main())
