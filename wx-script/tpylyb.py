# name: 太平洋蓝医保
# cron: 48 6,7 * * *  （打卡窗口 06:00-09:00，两次兜底）
# -*- coding: utf-8 -*-
"""
name: 太平洋蓝医保（健康分 2.0）
Date: 2026-10-28
YYB_SERVER：YYB-Go-Enhanced 路由，每行：地址@账号标识
YYB_API_KEY / YYB_PROTOCOL_TOKEN:       启用协议鉴权时配置，二选一
账号自动从 yyb_go 拉取全部存活账号

2026-10-23 活动改版要点（健康分 2.0）：
  * 旧的 /lyb-api/healthScore/upgrade/* 接口全部下线（POST 一律 500「网络繁忙」）
  * 新接口在 /asset-api/**，且**所有请求必须带防篡改三件套**：
        ua-encrypt: 1
        timestamp : 毫秒时间戳
        nonce     : AES-CBC-PKCS7(base64) of "<path>_<md5(规范化JSON body)>_<timestamp>"
    密钥/IV 取自小程序 env.CRYPTO_KEY = "AD42F6697B035B7580E4FEF93BE20BAD"
  * 响应 data 为同一密钥加密的 base64，需解密后 JSON.parse
  * 登录不再需要 wx-phone：新版 silentLogin 只调 /lyb-api/login/wx-session，
    用手机号 code 换 token 反而会拿到受限会话（所有健康分接口 500）

version: 3.0.0
"""

import base64
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===== YYB-Go-Enhanced + QingLong standalone adapter =====
def _yyb_routes():
    routes = []
    for number, line in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            raise RuntimeError(f"YYB_SERVER 第 {number} 行格式错误，应为 地址@账号标识")
        server, ref = line.rsplit("@", 1)
        if not server.strip() or not ref.strip():
            raise RuntimeError(f"YYB_SERVER 第 {number} 行格式错误，应为 地址@账号标识")
        server = server.strip().rstrip("/")
        if not server.lower().startswith(("http://", "https://")):
            server = "http://" + server
        routes.append({"server": server, "ref": ref.strip()})
    if not routes:
        raise RuntimeError("未配置 YYB_SERVER（每行：地址@账号标识）")
    return routes


def _yyb_clean_ref(value):
    text = str(value or "").split("#", 1)[0].strip()
    for prefix in ("wx:", "yyb:", "wmpf:", "syzs:"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _yyb_route(identifier):
    routes = _yyb_routes()
    wanted = _yyb_clean_ref(identifier)
    for route in routes:
        if _yyb_clean_ref(route["ref"]) == wanted:
            return route
    if wanted.isdigit() and 0 < int(wanted) <= len(routes):
        return routes[int(wanted) - 1]
    if len(routes) == 1:
        return routes[0]
    raise RuntimeError(f"YYB_SERVER 中找不到账号标识：{wanted or '(空)'}")


def _yyb_call(endpoint, app_id, identifier):
    route = _yyb_route(identifier)
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    response = requests.post(route["server"] + endpoint,
                             json={"ref": route["ref"], "app_id": app_id},
                             timeout=30, headers=headers)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or int(body.get("code", -1)) != 0:
        raise RuntimeError(f"{endpoint} 返回失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    if result is None:
        raise RuntimeError(f"{endpoint} 未返回 data.result")
    return result


class _YybClient:
    def get_online_accounts(self):
        return [{"id": x["ref"], "openid": x["ref"], "wxid": x["ref"],
                 "remark": f"YYB账号{i}", "status": "online", "login_type": "WX"}
                for i, x in enumerate(_yyb_routes(), 1)]

    def get_accounts(self, force_refresh=False):
        return self.get_online_accounts()


class _YybCompat:
    YYBClient = _YybClient

    @staticmethod
    def load_accounts(*_args, **_kwargs):
        return _YybClient().get_online_accounts()

    @staticmethod
    def resolve_accounts(env_name=""):
        return [x["ref"] for x in _yyb_routes()]

    @staticmethod
    def get_global_server_url():
        return _yyb_routes()[0]["server"]

    @staticmethod
    def get_single_code(app_id, identifier):
        result = _yyb_call("/wxapp/getCode", app_id, identifier)
        code = result if isinstance(result, str) else result.get("code")
        if not code:
            raise RuntimeError("/wxapp/getCode 未返回 data.result.code")
        return str(code)

    @staticmethod
    def normalize_login_type(value):
        return str(value or "WX").upper()

    @staticmethod
    def login_type_label(value):
        return {"WX": "应用宝", "WMPF": "微信小程序", "SYZS": "手游助手"}.get(
            _YybCompat.normalize_login_type(value), "应用宝")

    @staticmethod
    def _cache_path(name):
        import pathlib
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        return pathlib.Path(os.getenv("QL_DIR", "/ql/data")) / "config" / "yyb_token_caches" / f"{safe}.json"

    @classmethod
    def _read_cache(cls, name):
        try:
            return json.loads(cls._cache_path(name).read_text(encoding="utf-8"))
        except Exception:
            return {}

    @classmethod
    def get_cached_token(cls, name, identifier, options=None):
        item = cls._read_cache(name).get(_yyb_clean_ref(identifier))
        if not item:
            return None
        max_age = int((options or {}).get("max_age_ms", 0))
        if max_age and int(time.time() * 1000) - int(item.get("_saved_at", 0)) > max_age:
            return None
        return item

    @classmethod
    def save_cached_token(cls, name, identifier, value):
        data = cls._read_cache(name)
        data[_yyb_clean_ref(identifier)] = dict(value, _saved_at=int(time.time() * 1000))
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def remove_cached_token(cls, name, identifier):
        data = cls._read_cache(name)
        data.pop(_yyb_clean_ref(identifier), None)
        path = cls._cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


yyb = _YybCompat()


def _send_qinglong_notify(title, content):
    for path in (os.path.dirname(os.path.abspath(__file__)), "/ql/data/scripts", "/ql/scripts"):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from notify import send as _ql_send
        _ql_send(title, content)
        return True
    except Exception as exc:
        print(f"青龙通知失败（不影响任务结果）：{exc}")
        return False
# ===== adapter end =====


# ==================== 配置 ====================
APP_NAME = "太平洋蓝医保"
WX_APPID = "wx41b377f9104ee1c2"
BASE_URL = "https://lyb-api.cpic.com.cn"
# 小程序版本号（Referer 用）：随官方发版更新，可用环境变量覆盖
WX_VER = os.getenv("TPYLYB_WXVER", "150")
REFERER = f"https://servicewechat.com/{WX_APPID}/{WX_VER}/page-frame.html"
ENV_NAME = "lpzl_tpylyb"
CACHE_NAME = "tpylyb"

# 健康分 2.0 的 AES 密钥/IV（小程序 env.CRYPTO_KEY）
CRYPTO_KEY = b"AD42F6697B035B7580E4FEF93BE20BAD"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK_FILE = os.path.join(_BASE_DIR, "tpylyb_answer_bank.json")
_CK_FILE_LEGACY = os.path.join(_BASE_DIR, "tpylyb_token_cache.json")

# 早起打卡时间窗（服务端校验，这里只用于提前跳过无谓请求）
ZQDK_WINDOW = (6, 9)

# 只做查询/提示、不做自动化的任务说明
TASK_HINT = {
    "JBRF": "步数任务需微信运动 encryptedData（需真机授权），脚本不代做",
    "AI_MEAL_REPORT": "AI 识别三餐需上传餐食照片，脚本不代做",
    "AI_SPORT": "AI 运动需 H5 摄像头跟练，脚本不代做",
    "AI_HEALTH_REPORT": "体检报告解读需上传报告，脚本不代做",
    "LQYWBZ": "领取意外保障需在投保页实名投保（真实保单），脚本仅给链接",
    "LQZJBZ": "领取重疾保障需在投保页实名投保（真实保单），脚本仅给链接",
}

SERVER_URL = yyb.get_global_server_url()


def log(msg):
    print(msg, flush=True)


# ==================== 加解密 / 签名 ====================
def _aes_encrypt(text):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    cipher = AES.new(CRYPTO_KEY, AES.MODE_CBC, CRYPTO_KEY[:16])
    return base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode()


def _aes_decrypt(b64_text):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    cipher = AES.new(CRYPTO_KEY, AES.MODE_CBC, CRYPTO_KEY[:16])
    return unpad(cipher.decrypt(base64.b64decode(b64_text)), 16).decode("utf-8")


def _sorted_json(obj):
    """等价于小程序 sortedStringify：对象键递归排序，数组保持原序。"""
    def norm(o):
        if isinstance(o, dict):
            return {k: norm(o[k]) for k in sorted(o)}
        if isinstance(o, list):
            return [norm(x) for x in o]
        return o
    return json.dumps(norm(obj), ensure_ascii=False, separators=(",", ":"))


def sign_headers(path, data):
    """防篡改三件套：ua-encrypt / timestamp / nonce。"""
    ts = int(time.time() * 1000)
    body = "" if data is None else _sorted_json(data)
    digest = hashlib.md5(body.encode("utf-8")).hexdigest()
    return {
        "ua-encrypt": "1",
        "timestamp": str(ts),
        "nonce": _aes_encrypt("%s_%s_%s" % (path, digest, ts)),
    }


# ==================== 本地文件 ====================
def load_bank():
    try:
        with open(BANK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_bank(data):
    try:
        with open(BANK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as exc:
        log(f"错题本写入失败（不影响本次结果）：{exc}")


# ==================== 账号 ====================
def parse_accounts():
    try:
        accounts = yyb.load_accounts(ENV_NAME)
    except Exception as e:
        log(f"❌ 拉取 yyb_go 账号失败: {e}")
        return []

    result = []
    for idx, acc in enumerate(accounts or [], 1):
        identifier = str(acc.get("openid") or acc.get("id") or acc.get("wxid") or "").strip()
        if not identifier:
            continue
        remark = acc.get("remark") or acc.get("nickname") or acc.get("alias") or f"账号{idx}"
        result.append({
            "identifier": identifier,
            "remark": remark,
            "login_type": yyb.normalize_login_type(acc.get("login_type")),
            "protocol": yyb.login_type_label(acc.get("login_type")),
        })

    if result:
        log(f"✅ 从 yyb_go 同步到 {len(result)} 个存活账号（服务地址: {SERVER_URL}）")
    else:
        log(f"❌ 未获取到存活账号，请确认 yyb_go 已启动且有在线账号（服务地址: {SERVER_URL}）")
    return result


# ==================== 客户端 ====================
class TpylybClient:
    """健康分 2.0 客户端：/asset-api/** + 防篡改签名头。"""

    def __init__(self, account):
        self.identifier = account["identifier"]
        self.remark = account["remark"]
        self.protocol = account["protocol"]
        self.login_type = account.get("login_type", "")
        self.openid = ""
        self.unionid = ""
        self.token = ""
        self.score = None
        self.bean = None
        self.tasks = []
        self.session = requests.Session()
        self.headers = {
            "Host": "lyb-api.cpic.com.cn",
            "Connection": "keep-alive",
            "xweb_xhr": "1",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
                           "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF "
                           "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254181d) XWEB/19841"),
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": REFERER,
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cached = yyb.get_cached_token(CACHE_NAME, self.identifier, {"max_age_ms": 6 * 3600 * 1000})
        if not (cached and cached.get("token")):
            cached = self._legacy_cache()
        if cached:
            self.openid = cached.get("openid", "")
            self.unionid = cached.get("unionid", "")
            self.token = cached.get("token", "")

    # ---------- 缓存 ----------
    def _legacy_cache(self):
        try:
            with open(_CK_FILE_LEGACY, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        for key in (f"yyb:{self.identifier}", f"niuzi:{self.identifier}", self.identifier):
            item = data.get(key)
            if isinstance(item, dict) and item.get("token"):
                return item
        return None

    def _save_cache(self):
        yyb.save_cached_token(CACHE_NAME, self.identifier, {
            "openid": self.openid,
            "unionid": self.unionid,
            "token": self.token,
        })

    def _clear_cache(self):
        yyb.remove_cached_token(CACHE_NAME, self.identifier)

    # ---------- 请求 ----------
    def _headers(self, signed_path=None, data=None):
        h = dict(self.headers)
        if self.openid:
            h["lyb-m-openid"] = self.openid
        if self.unionid:
            h["lyb-m-unionid"] = self.unionid
        if self.token:
            h["lyb-m-token"] = self.token
        else:
            h.pop("lyb-m-token", None)
        if signed_path:
            h.update(sign_headers(signed_path, data))
        return h

    def _post(self, path, data=None, prefix="/asset-api", signed=True):
        """返回 (code, payload)。payload 已解密；code != 0 时 payload 为 {"msg": ...}"""
        body = data if data is not None else {}
        headers = self._headers(path if signed else None, body if signed else None)
        resp = self.session.post(BASE_URL + prefix + path, headers=headers, json=body, timeout=20)
        token_header = resp.headers.get("lyb-m-token")
        if token_header:
            self.token = token_header
        try:
            raw = resp.json()
        except Exception:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")

        code = raw.get("code")
        payload = raw.get("data")
        if isinstance(payload, str) and payload:
            try:
                payload = json.loads(_aes_decrypt(payload))
            except Exception as exc:
                payload = {"_decrypt_error": str(exc)}
        if code != 0:
            return code, {"msg": raw.get("msg") or raw.get("message") or f"code={code}"}
        return code, payload

    # ---------- 登录 ----------
    def login(self):
        code = yyb.get_single_code(WX_APPID, self.identifier)
        if not code:
            raise RuntimeError("获取微信 code 失败")
        log(f"[{self.remark}] 获取微信 code 成功: {code[:8]}...")
        headers = {
            "content-type": "application/json",
            "Referer": REFERER,
            "User-Agent": self.headers["User-Agent"],
        }
        resp = self.session.post(BASE_URL + "/lyb-api/login/wx-session",
                                 headers=headers, json={"code": code}, timeout=20)
        token_header = resp.headers.get("lyb-m-token")
        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"/wx-session 返回异常: HTTP {resp.status_code}")
        if body.get("code") != 0:
            raise RuntimeError(f"/wx-session 失败: {body.get('msg') or body}")
        data = body.get("data") or {}
        self.openid = data.get("openid") or ""
        self.unionid = data.get("unionid") or ""
        self.token = token_header or ""
        if not self.openid:
            raise RuntimeError("/wx-session 未返回 openid")
        if not self.token:
            raise RuntimeError("/wx-session 未返回 lyb-m-token")
        self._save_cache()
        log(f"[{self.remark}] 登录成功，token: {self.token[:8]}...")

    def ensure_login(self):
        if self.token and self.openid:
            code, _ = self._post("/user-asset/get-growth-info-V1", {})
            if code == 0:
                log(f"[{self.remark}] 命中 token 缓存，跳过取码登录")
                return
            log(f"[{self.remark}] token 失效({code})，清除缓存并重新登录")
            self._clear_cache()
            self.token = self.openid = self.unionid = ""
        self.login()

    # ---------- 业务 ----------
    def growth(self):
        code, data = self._post("/user-asset/get-growth-info-V1", {})
        if code != 0:
            raise RuntimeError(f"获取健康分失败: {data.get('msg')}")
        self.score = data.get("currentHealthScore")
        return data

    def blue_bean(self):
        code, data = self._post("/blue-bean/get-balance-V1", {})
        if code == 0:
            self.bean = data.get("currentBlueBean")
        return self.bean

    def task_list(self, query_type=1):
        code, data = self._post("/task/list-V1", {"queryType": query_type})
        if code != 0:
            raise RuntimeError(f"获取任务列表失败: {data.get('msg')}")
        return data.get("healthTaskList") or []

    # ---- 早起打卡 ----
    def zqdk_home(self):
        code, data = self._post("/task/zqdk/homeInit-V1", {"taskCode": "ZQDK"})
        return data if code == 0 else {"_error": data.get("msg")}

    def zqdk_sign(self):
        code, data = self._post("/task/zqdk/dk-V1", {"taskCode": "ZQDK"})
        if code == 0:
            return True, data
        return False, data.get("msg") or f"code={code}"

    # ---- 健康问答 ----
    def jkwd_question(self):
        code, data = self._post("/task/jkwd/getQuestion-V1", {"taskCode": "JKWD"})
        if code != 0:
            return None, data.get("msg") or f"code={code}"
        return data, None

    def jkwd_answer(self, question_id, select_value):
        code, data = self._post("/task/jkwd/getQuestionAnswer-V1",
                                {"questionId": question_id, "selectValue": select_value})
        if code != 0:
            return None, data.get("msg") or f"code={code}"
        return data, None

    def jkwd_revive(self, answer_id):
        code, data = self._post("/task/jkwd/revive-V1", {"answerId": str(answer_id)})
        if code != 0:
            return False, data.get("msg") or f"code={code}"
        return data.get("reviveResult") == "Y", data.get("reviveFailReason") or ""

    def lqbz_status(self, task_code):
        code, data = self._post("/task/lqbz/checkStatus-V1", {"taskCode": task_code})
        return data if code == 0 else None

    # ---------- 任务编排 ----------
    def _answer_pick(self, bank, question):
        """选答案：优先错题本，其次挑选最长选项（健康科普题的长描述多为正确项）。"""
        text = (question.get("questionText") or "").strip()
        options = question.get("options") or []
        keys = [o.get("optionKey") for o in options]
        item = bank.get(text)
        if item and item.get("answer") in keys:
            return item["answer"], "错题本"
        if not keys:
            return None, ""
        best = max(options, key=lambda o: len(str(o.get("optionValue") or "")))
        return best.get("optionKey"), "最长选项"

    def do_jkwd(self, bank):
        """每日健康问答：答错则记录正确答案并分享复活换题，最多 4 轮。"""
        bank_changed = False
        for attempt in range(1, 5):
            question, err = self.jkwd_question()
            if err:
                log(f"[{self.remark}] 健康问答取题失败：{err}")
                return bank_changed, False
            if (question.get("historyAnswerResult") or "") == "Y":
                log(f"[{self.remark}] 健康问答：今日已完成")
                return bank_changed, True

            qid = question.get("questionId")
            text = (question.get("questionText") or "").strip()
            if not qid:
                log(f"[{self.remark}] 健康问答：未返回 questionId")
                return bank_changed, False

            key, source = self._answer_pick(bank, question)
            if not key:
                log(f"[{self.remark}] 健康问答：题目无选项")
                return bank_changed, False

            result, err = self.jkwd_answer(qid, key)
            if err:
                log(f"[{self.remark}] 健康问答作答失败：{err}")
                return bank_changed, False

            if (result.get("historyAnswerResult") or "") == "Y":
                bank[text] = {"answer": key, "updated": int(time.time())}
                bank_changed = True
                gain = str(result.get("answerScoreResultDesc") or "+1").lstrip("+")
                beans = str(result.get("answerBeanResultDesc") or "+10").lstrip("+")
                log(f"[{self.remark}] 健康问答答对 ✅ {result.get('questionText', text)}"
                    f"（{source}→{key}，+{gain}分/+{beans}豆）")
                return bank_changed, True

            right = (result.get("answerResultDesc") or "").strip()
            bank[text] = {"answer": right, "updated": int(time.time())}
            bank_changed = True
            log(f"[{self.remark}] 健康问答答错 ❌ {text}｜我选 {key}，正确答案 {right}"
                f"（第 {attempt} 轮，已记入错题本）")

            if not result.get("showReviveButton"):
                log(f"[{self.remark}] 无复活入口，今日问答结束")
                return bank_changed, False
            ok, reason = self.jkwd_revive(result.get("answerId"))
            if not ok:
                log(f"[{self.remark}] 分享复活失败：{reason}")
                return bank_changed, False
            log(f"[{self.remark}] 分享复活成功，换一题继续")
            time.sleep(random.uniform(1.0, 2.0))
        log(f"[{self.remark}] 健康问答：{4} 轮未答对，今日放弃")
        return bank_changed, False

    def do_zqdk(self, tasks):
        now_hour = datetime.now().hour
        target = next((t for t in tasks if t.get("taskCode") == "ZQDK"), None)
        if target is None:
            log(f"[{self.remark}] 早起打卡：今日任务已消失（视为已完成）")
            return True
        if target.get("taskStatus") not in (1, None):
            log(f"[{self.remark}] 早起打卡：{target.get('buttonText') or '已完成'}")
            return True

        home = self.zqdk_home()
        today = next((r for r in (home.get("records") or []) if r.get("todayFlag") == "1"), None)
        if today and today.get("taskStatus") != 2:
            log(f"[{self.remark}] 早起打卡：今日已打卡"
                f"{('，红包状态 ' + str(today.get('redStatus'))) if today.get('redStatus') else ''}")
            return True
        start, end = home.get("dkStartTime") or "06:00", home.get("dkEndTime") or "09:00"
        log(f"[{self.remark}] 早起打卡窗口 {start}-{end}（当前 {datetime.now():%H:%M}）")
        if not (ZQDK_WINDOW[0] <= now_hour < ZQDK_WINDOW[1]):
            log(f"[{self.remark}] 早起打卡：不在打卡时段，跳过")
            return False

        ok, data = self.zqdk_sign()
        if ok:
            log(f"[{self.remark}] 早起打卡成功 ✅ +{data.get('taskScoreReward') or 1}分"
                f"/+{data.get('taskBeanReward') or 10}豆")
            return True
        log(f"[{self.remark}] 早起打卡失败：{data}")
        return False

    def report_others(self, tasks):
        for task in tasks:
            code_name = task.get("taskCode") or ""
            status = task.get("taskStatus")
            label = task.get("taskName") or code_name
            if code_name in ("ZQDK", "JKWD"):
                continue
            if status not in (1, None):
                log(f"[{self.remark}] {label}：{task.get('buttonText') or '已完成'}")
                continue
            hint = TASK_HINT.get(code_name)
            if code_name in ("LQYWBZ", "LQZJBZ"):
                info = self.lqbz_status(code_name) or {}
                url = info.get("insureUrl") or ""
                log(f"[{self.remark}] {label}：需实名投保（{info.get('toastMsg') or '去投保'}）{url}")
            elif hint:
                log(f"[{self.remark}] {label}：{hint}")

    def run(self, bank):
        self.ensure_login()
        info = self.growth()
        self.blue_bean()
        log(f"[{self.remark}] 执行前：健康分 {self.score}"
            f"（{info.get('currentLevel')}，下一档 {info.get('nextLevelThreshold')}），蓝豆 {self.bean}")

        tasks = self.task_list(1)
        changed = False
        self.do_zqdk(tasks)
        changed |= self.do_jkwd(bank)[0]
        self.report_others(self.task_list(2))

        self.growth()
        self.blue_bean()
        log(f"[{self.remark}] 执行后：健康分 {self.score}，蓝豆 {self.bean}")
        return changed


def main():
    accounts = parse_accounts()
    if not accounts:
        log("未找到账号：请确认 yyb_go 已启动且有存活账号")
        return 1

    log(f"{' ' * 10}꧁༺ {APP_NAME}（健康分 2.0）༻꧂\n")
    bank = load_bank()
    bank_dirty = False
    summaries = []
    failed = 0

    for i, acc in enumerate(accounts, 1):
        log(f"\n----------- 🎊 第 {i} 个账号 🎊 -----------")
        try:
            client = TpylybClient(acc)
            bank_dirty |= client.run(bank)
            summaries.append(f"账号{i}：健康分 {client.score}，蓝豆 {client.bean}")
        except Exception as exc:
            failed += 1
            log(f"[{acc['remark']}] 执行异常: {exc}")
            summaries.append(f"账号{i}：执行异常 {exc}")

    if bank_dirty:
        save_bank(bank)

    log("\n----------- 🎊 执 行  结 束 🎊 -----------\n")
    _send_qinglong_notify(APP_NAME, "\n".join(summaries))
    return 0 if failed < len(accounts) else 1


if __name__ == "__main__":
    sys.exit(main())
