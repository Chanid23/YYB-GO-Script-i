#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 雨云自动签到
# cron: 44 8,21 * * *
# 环境变量名称：RAINYUN
# 单账户填写：账号#密码
# 多账户填写：账号1#密码1@账号2#密码2
#
# 需自备验证码识别 Token 配置教程：
# 1. 打开 https://mf.yinxiaoxing.dpdns.org/ 并登录。
# 2. 在“我的 Token”中申请新 Token，求解系统选择“腾讯图形点选”。
# 3. 进入青龙面板“环境变量”，新建变量 OCR_TOKEN，值填写申请到的 Token。
# 4. CAPTCHA_API_URL 默认上方服务，可自行“环境变量”中配置地址。

"""
雨云(Rainyun)自动签到脚本
依赖：requests, opencv-python, numpy, pillow
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
import base64
import re
import hashlib
from datetime import datetime
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    import numpy as np

# Windows 终端 UTF-8 输出（避免 emoji 打印报错）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
import urllib3
from PIL import Image
from io import BytesIO

try:
    import cv2
    import numpy as np
    CV2_OK = True
except ImportError:
    CV2_OK = False

try:
    from py_mini_racer import MiniRacer
    JS_OK = True
    try:
        _pmr_version = MiniRacer()._v8_version if hasattr(MiniRacer(), '_v8_version') else "unknown"
    except Exception:
        _pmr_version = "ok"
    print(f"[rainyun] py_mini_racer 已加载 (version={_pmr_version})", flush=True)
except ImportError as e:
    JS_OK = False
    print(f"[rainyun] py_mini_racer 未安装: {e}，将使用 Node.js 降级方案", flush=True)
except Exception as e:
    JS_OK = False
    print(f"[rainyun] py_mini_racer 初始化失败: {e}，将使用 Node.js 降级方案", flush=True)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 配置常量
# ============================================================

API_BASE      = "https://api.v2.rainyun.com"
CAPTCHA_BASE  = "https://turing.captcha.qcloud.com"
APP_URL       = "https://app.rainyun.com"
CAPTCHA_AID   = "2039519451"

# ====== 青龙面板配置区（直接修改这里）======
# 验证码求解 API 地址
CAPTCHA_API_URL = os.environ.get("CAPTCHA_API_URL", "https://mf.yinxiaoxing.dpdns.org/").rstrip("/")
# 人机求解系统 Token（必须通过青龙环境变量 OCR_TOKEN 配置，脚本自身不保存 Token）
OCR_TOKEN = os.environ.get("OCR_TOKEN", "").strip()
# 雨云账号列表（支持多账号，格式: [("备注名", "用户名", "密码"), ...]）
ACCOUNTS = [
    ("账号1", "用户名1", "用户名1密码"),
    ("账号2", "用户名2", "用户名2密码"),
]


def parse_rainyun_accounts(raw: str) -> List[Tuple[str, str, str]]:
    """解析 RAINYUN=账号#密码@账号#密码；兼容单账户。"""
    accounts: List[Tuple[str, str, str]] = []
    for index, entry in enumerate(str(raw or "").strip().split("@"), 1):
        entry = entry.strip()
        if not entry:
            continue
        if "#" not in entry:
            raise ValueError(f"RAINYUN 第 {index} 个账户格式错误：缺少 # 分隔符")
        username, password = entry.split("#", 1)
        username = username.strip()
        password = password.strip()
        if not username or not password:
            raise ValueError(f"RAINYUN 第 {index} 个账户格式错误：账号或密码为空")
        accounts.append((f"账号{len(accounts) + 1}", username, password))
    return accounts

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR   = os.path.join(SCRIPT_DIR, "rainyun_templates")
CONFIG_FILE    = os.path.join(SCRIPT_DIR, "rainyun_config.json")
COOKIES_FILE   = os.path.join(SCRIPT_DIR, "rainyun_cookies.json")
LOG_FILE       = os.path.join(SCRIPT_DIR, "rainyun_sign.log")

MATCH_THRESHOLD = 0.72
RETRY_LIMIT     = 5
CAPTCHA_RETRIES = 6   # 验证码最多重试次数

# ====== 提现配置 ======
# 提现金额:
#   "all"  = 提现全部积分（需达到最低提现门槛 WITHDRAW_MIN_POINTS）
#   数字    = 总积分达到该值时提现对应积分，如 60000 = 提现 60000 积分
WITHDRAW_POINTS     = os.environ.get("WITHDRAW_POINTS", "60000")
# 最低提现门槛（平台要求，"all" 模式下必须达到才能提现）
WITHDRAW_MIN_POINTS = int(os.environ.get("WITHDRAW_MIN_POINTS", "60000"))
# 提现目标: alipay(支付宝)
WITHDRAW_TARGET     = os.environ.get("WITHDRAW_TARGET", "alipay")


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line.encode('utf-8', errors='replace').decode('utf-8'), flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_qinglong_notification(title: str, content: str) -> bool:
    """调用青龙 notify.py；通知失败不影响签到业务结果。"""
    search_dirs = [SCRIPT_DIR, "/ql/data/scripts", "/ql/scripts"]
    for directory in search_dirs:
        if directory and directory not in sys.path:
            sys.path.insert(0, directory)
    try:
        from notify import send
        send(title, content)
        print("青龙通知：发送请求成功", flush=True)
        return True
    except Exception as e:
        print(f"青龙通知：发送失败（{type(e).__name__}: {e}）", flush=True)
        return False


def ensure_dirs():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)


# ============================================================
# HTTP 会话
# ============================================================

class Session:
    def __init__(self, username: str = ""):
        self.s = requests.Session()
        self.s.verify = False
        self._set_headers()
        self.dev_code      = ""
        self.rain_session  = ""
        self.csrf_token    = ""
        self.cookies_data  = {}
        self.username      = username

    def _set_headers(self):
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": APP_URL,
            "Referer": APP_URL + "/",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })

    def post(self, path, data=None, params=None, extra_headers=None):
        url = API_BASE + path
        h = dict(self.s.headers)
        if extra_headers:
            h.update(extra_headers)
        try:
            r = self.s.post(url, json=data, params=params, headers=h, timeout=20)
            # 雨云会把业务错误（如账号错误、需要验证码）放在 4xx JSON 中。
            # 先解析响应体，避免 raise_for_status() 把真正的业务原因吞掉。
            result = r.json()
            if not r.ok and not isinstance(result, dict):
                r.raise_for_status()
            return result
        except Exception as e:
            log(f"  ❌ POST {path} 失败: {e}")
            return None

    def get(self, path, params=None, extra_headers=None):
        url = API_BASE + path
        h = dict(self.s.headers)
        if extra_headers:
            h.update(extra_headers)
        try:
            r = self.s.get(url, params=params, headers=h, timeout=20)
            result = r.json()
            if not r.ok and not isinstance(result, dict):
                r.raise_for_status()
            return result
        except Exception as e:
            log(f"  ❌ GET {path} 失败: {e}")
            return None

    def get_captcha(self, path, params=None, extra_headers=None):
        """获取验证码接口（使用验证码域名）"""
        url = CAPTCHA_BASE + path
        h = dict(self.s.headers)
        h["Referer"] = APP_URL + "/"
        if extra_headers:
            h.update(extra_headers)
        try:
            r = self.s.get(url, params=params, headers=h, timeout=20)
            r.raise_for_status()
            return r
        except Exception as e:
            log(f"  ❌ CAPTCHA GET {path} 失败: {e}")
            return None

    def post_captcha(self, path, data=None, extra_headers=None):
        """提交验证码（使用验证码域名）"""
        url = CAPTCHA_BASE + path
        h = dict(self.s.headers)
        h["Referer"] = APP_URL + "/"
        h["Content-Type"] = "application/x-www-form-urlencoded"
        if extra_headers:
            h.update(extra_headers)
        try:
            r = self.s.post(url, data=data, headers=h, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log(f"  ❌ CAPTCHA POST {path} 失败: {e}")
            return None

    def download_img(self, url: str) -> Optional[bytes]:
        """下载图片原始字节"""
        if url.startswith("/"):
            url = CAPTCHA_BASE + url
        try:
            r = self.s.get(url, timeout=15, stream=True, verify=False)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log(f"  ❌ 下载图片失败: {e}")
            return None

    def bytes_to_cv(self, raw: bytes, keep_alpha: bool = False) -> Optional[np.ndarray]:
        """bytes → OpenCV 图像
        keep_alpha=False: BGR 3通道(背景图用)
        keep_alpha=True: BGRA 4通道(碎片图用,保留透明通道)
        """
        if not raw:
            return None
        arr = np.frombuffer(raw, np.uint8)
        flag = cv2.IMREAD_UNCHANGED if keep_alpha else cv2.IMREAD_COLOR
        img = cv2.imdecode(arr, flag)
        if keep_alpha and img is not None and len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        return img

    def save_cookies(self):
        cookies = {}
        for c in self.s.cookies:
            if c.name in ("rain-session", "dev-code", "HMACCOUNT", "X-CSRF-Token"):
                cookies[c.name] = c.value
        cookies["csrf_token"] = self.csrf_token
        cookies["dev_code"]   = self.dev_code

        # 读取已有数据,按用户名保存
        all_data = {}
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                    all_data = json.load(f)
            except Exception:
                all_data = {}
        all_data[self.username] = cookies
        try:
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(all_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        log(f"  💾 已保存 Cookies")

    def load_cookies(self) -> bool:
        if not os.path.exists(COOKIES_FILE):
            return False
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                all_data = json.load(f)
            cookies = all_data.get(self.username, {})
            if not cookies:
                return False
            for name, val in cookies.items():
                if name in ("rain-session", "dev-code", "HMACCOUNT", "X-CSRF-Token"):
                    self.s.cookies.set(name, val, domain=".rainyun.com")
            self.dev_code      = cookies.get("dev_code", cookies.get("dev-code", ""))
            self.rain_session  = cookies.get("rain-session", "")
            self.csrf_token    = cookies.get("csrf_token", "")
            log(f"  🔑 已加载 Cookies")
            return True
        except Exception as e:
            log(f"  ⚠️ 加载 Cookies 失败: {e}")
            return False


# ============================================================
# 验证码求解器
# ============================================================

class CaptchaSolver:
    """
    腾讯云 TCaptcha 点选验证码求解器
    流程：
      1. GET /cap_union_prehandle → 获取 sess / 图片相对路径
      2. 下载背景图 + 碎片图
      3. 分割碎片图为3块
      4. SIFT/ORB 特征匹配定位每块在背景图中的坐标
      5. POST /cap_union_new_verify 提交答案，获取 ticket/randstr
    """

    def __init__(self, sess: Session):
        self.sess = sess
        self.ticket     = ""
        self.randstr    = ""
        self.coords: List[Tuple[int, int]] = []
        self.captcha_data: dict = {}
        self.captcha_aid: str = CAPTCHA_AID

    # ---------- 获取验证码配置 ----------

    def get_prehandle(self) -> Optional[dict]:
        """调用 cap_union_prehandle 获取验证码配置"""
        params = {
            "aid": self.captcha_aid,
            "protocol": "https",
            "accver": "1",
            "showtype": "popup",
            "ua": base64.b64encode(self.sess.s.headers["User-Agent"].encode()).decode(),
            "noheader": "1",
            "fb": "1",
            "aged": "0",
            "enableAged": "0",
            "enableDarkMode": "0",
            "grayscale": "1",
            "clientype": "2",
            "cap_cd": "",
            "uid": "",
            "lang": "zh-cn",
            "entry_url": "https://turing.captcha.gtimg.com/1/template/drag_ele.html",
            "elder_captcha": "0",
            "js": "/tcaptcha-frame.97a921e6.js",
            "login_appid": "",
            "wb": "1",
            "subsid": "9",
            "callback": "",
            "sess": "",
        }
        r = self.sess.get_captcha("/cap_union_prehandle", params=params)
        if not r:
            return None
        # 返回格式是 JSONP: (...) 包裹
        text = r.text.strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        try:
            data = json.loads(text)
            if data.get("state") != 1:
                log(f"  ⚠️ prehandle 返回 state={data.get('state')}")
                return None
            log(f"  ✅ 验证码配置获取成功")
            return data
        except json.JSONDecodeError as e:
            log(f"  ❌ prehandle 响应解析失败: {e}")
            return None

    def refresh_prehandle(self, old_sess: str) -> Optional[dict]:
        """刷新验证码（当旧 ticket 失效时）"""
        try:
            r = self.sess.s.post(
                CAPTCHA_BASE + "/cap_union_new_getsig",
                data={"sess": old_sess},
                headers={
                    "User-Agent": self.sess.s.headers["User-Agent"],
                    "Referer": APP_URL + "/",
                },
                timeout=15, verify=False
            )
            r.raise_for_status()
            data = r.json()
            if data.get("state") == 1:
                return data
            return None
        except Exception as e:
            log(f"  ⚠️ 刷新验证码失败: {e}")
            return None

    # ---------- 下载图片 ----------

    def download_images(self, prehandle_data: dict) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """下载背景图和碎片图，返回 (bg, sprite)"""
        dyn = prehandle_data.get("data", {})
        info = dyn.get("dyn_show_info", {})
        bg_cfg   = info.get("bg_elem_cfg", {})
        bg_url   = bg_cfg.get("img_url", "")
        sprite_url = info.get("sprite_url", "")

        if not bg_url or not sprite_url:
            log("  ❌ 未找到图片 URL")
            return None, None

        bg_raw   = self.sess.download_img(bg_url)
        sprite_raw = self.sess.download_img(sprite_url)

        bg   = self.sess.bytes_to_cv(bg_raw, keep_alpha=False)   if bg_raw   else None
        sprite = self.sess.bytes_to_cv(sprite_raw, keep_alpha=True) if sprite_raw else None

        if bg is None:
            log("  ❌ 背景图解码失败")
        elif sprite is None:
            log("  ❌ 碎片图解码失败")
        else:
            log(f"  ✅ 图片已下载  背景:{bg.shape[1]}x{bg.shape[0]}  碎片:{sprite.shape[1]}x{sprite.shape[0]}")

        return bg, sprite

    # ---------- 分割碎片 ----------

    def split_sprite(self, sprite: np.ndarray, ins_elem_cfg: Optional[List[dict]] = None) -> List[np.ndarray]:
        """
        将碎片图分割成多个小碎片。
        ins_elem_cfg 可能只定义整个碎片图的区域（1个元素），
        但 instruction "请依次点击：" 暗示需要三等分。
        当 ins_elem_cfg 只有1个元素时，仍然三等分。
        """
        h, w = sprite.shape[:2]
        if ins_elem_cfg and len(ins_elem_cfg) > 1:
            # 多个碎片：按 ins_elem_cfg 分割
            pieces = []
            for elem in ins_elem_cfg:
                pos = elem.get("sprite_pos", [0, 0])
                size = elem.get("size_2d", [w, h])
                x, y = pos[0], pos[1]
                pw, ph = size[0], size[1]
                pieces.append(sprite[y:y+ph, x:x+pw].copy())
            return pieces
        # ins_elem_cfg 只有1个元素或没有：三等分
        piece_w = w // 3
        pieces = []
        for i in range(3):
            x1 = i * piece_w
            x2 = w if i == 2 else (i + 1) * piece_w
            pieces.append(sprite[:, x1:x2].copy())
        return pieces

    # ---------- 特征匹配 ----------

    def find_positions(self, bg: np.ndarray, pieces: List[np.ndarray]) -> List[Tuple[int, int]]:
        """
        在背景图上定位每个碎片的位置
        方法: 边缘检测 + 多尺度模板匹配 + SIFT特征匹配
        返回: [(center_x, center_y), ...]
        """
        if not CV2_OK or not pieces:
            return []

        positions = []
        bg_h, bg_w = bg.shape[:2]
        used_regions: List[Tuple[int, int, int, int]] = []
        log(f"  📐 背景图尺寸: {bg_w}x{bg_h}")

        # 预处理背景图: 灰度 + Canny边缘
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        bg_edges = cv2.Canny(bg_gray, 50, 150)
        # 灰度阈值处理（适合数字/高对比度碎片）
        # 阈值120只保留深色轮廓，避免背景渐变/几何体边缘被误检测
        _, bg_thresh = cv2.threshold(bg_gray, 120, 255, cv2.THRESH_BINARY_INV)

        # SIFT 检测器
        try:
            sift = cv2.SIFT_create()
            bg_kp, bg_des = sift.detectAndCompute(bg_gray, None)
        except Exception:
            sift = None
            bg_kp, bg_des = None, None

        for i, piece in enumerate(pieces):
            if piece is None or piece.size == 0:
                log(f"  ⚠️ 碎片 {i+1} 为空")
                continue

            # 检查是否有alpha通道
            has_alpha = piece.shape[2] == 4 if len(piece.shape) == 3 else False
            if has_alpha:
                piece_bgr = piece[:, :, :3]
                piece_alpha = piece[:, :, 3]
            else:
                piece_bgr = piece
                piece_alpha = None

            piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)
            ph, pw = piece_gray.shape[:2]
            piece_edges = cv2.Canny(piece_gray, 50, 150)

            # 提前检测碎片特征数（用于决策匹配策略）
            piece_kp_count = 0
            is_low_feature = False
            if sift is not None:
                try:
                    _kp, _des = sift.detectAndCompute(piece_gray, None)
                    piece_kp_count = len(_kp) if _kp else 0
                    is_low_feature = piece_kp_count < 30  # 特征少于30个 → 数字/简单符号
                except Exception:
                    pass
            log(f"  🔍 碎片{i+1} 特征点数: {piece_kp_count}")

            best_pos, best_score = None, 0.0
            best_method = ""

            # 方法1: 边缘检测 + 多尺度模板匹配（仅对高特征碎片使用）
            # 低特征碎片（数字"0"/"1"/"5"等）用edge_tm会匹配到背景伪目标，跳过
            if not is_low_feature:
                for scale in [0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0]:
                    new_w = max(10, int(pw * scale))
                    new_h = max(10, int(ph * scale))
                    scaled_edges = cv2.resize(piece_edges, (new_w, new_h))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    res = cv2.matchTemplate(bg_edges, scaled_edges, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)

                    cand_x1 = max_loc[0]
                    cand_y1 = max_loc[1]
                    cand_x2 = cand_x1 + new_w
                    cand_y2 = cand_y1 + new_h
                    overlaps = False
                    for ux1, uy1, ux2, uy2 in used_regions:
                        if cand_x1 < ux2 and cand_x2 > ux1 and cand_y1 < uy2 and cand_y2 > uy1:
                            overlaps = True
                            break
                    if overlaps:
                        continue

                    if max_val > best_score:
                        best_score = max_val
                        best_pos = (cand_x1 + new_w // 2, cand_y1 + new_h // 2)
                        best_method = f"edge_tm(scale={scale})"

            # 方法2: 带mask的模板匹配(如果有alpha通道)
            if has_alpha and best_score < 0.55 and not is_low_feature:
                for scale in [0.8, 0.9, 1.0, 1.1, 1.2, 1.5]:
                    new_w = max(10, int(pw * scale))
                    new_h = max(10, int(ph * scale))
                    scaled_piece = cv2.resize(piece_gray, (new_w, new_h))
                    scaled_mask = cv2.resize(piece_alpha, (new_w, new_h))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    res = cv2.matchTemplate(bg_gray, scaled_piece, cv2.TM_CCORR_NORMED, mask=scaled_mask)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)

                    cand_x1 = max_loc[0]
                    cand_y1 = max_loc[1]
                    cand_x2 = cand_x1 + new_w
                    cand_y2 = cand_y1 + new_h
                    overlaps = False
                    for ux1, uy1, ux2, uy2 in used_regions:
                        if cand_x1 < ux2 and cand_x2 > ux1 and cand_y1 < uy2 and cand_y2 > uy1:
                            overlaps = True
                            break
                    if overlaps:
                        continue

                    if max_val > best_score:
                        best_score = max_val
                        best_pos = (cand_x1 + new_w // 2, cand_y1 + new_h // 2)
                        best_method = f"mask_tm(scale={scale})"
                    if max_val >= 0.55:
                        break

            # 方法3: SIFT 特征匹配（高特征碎片首选，低特征碎片也尝试但作为次要）
            if sift is not None and bg_des is not None:
                try:
                    piece_kp, piece_des = sift.detectAndCompute(piece_gray, None)
                    if piece_des is not None and len(piece_des) > 2:
                        flann = cv2.FlannBasedMatcher(
                            {"algorithm": 1, "trees": 5}, {"checks": 50}
                        )
                        matches = flann.knnMatch(piece_des, bg_des, k=2)
                        good_matches = []
                        for m, n in matches:
                            if m.distance < 0.85 * n.distance:
                                good_matches.append(m)
                        if len(good_matches) >= 8:
                            src_pts = np.float32(
                                [piece_kp[m.queryIdx].pt for m in good_matches]
                            ).reshape(-1, 1, 2)
                            dst_pts = np.float32(
                                [bg_kp[m.trainIdx].pt for m in good_matches]
                            ).reshape(-1, 1, 2)
                            matrix, mask = cv2.findHomography(
                                src_pts, dst_pts, cv2.RANSAC, 5.0
                            )
                            if matrix is not None:
                                h, w = piece_gray.shape[:2]
                                pts = np.float32(
                                    [[0, 0], [w, 0], [w, h], [0, h]]
                                ).reshape(-1, 1, 2)
                                dst = cv2.perspectiveTransform(pts, matrix)
                                cx = int((dst[0][0][0] + dst[2][0][0]) / 2)
                                cy = int((dst[0][0][1] + dst[2][0][1]) / 2)
                                if 0 <= cx < bg_w and 0 <= cy < bg_h:
                                    # 几何有效性校验：仿射变换后的宽高比应接近原始比例
                                    ws = [dst[j][0][0] for j in range(4)]
                                    hs = [dst[j][0][1] for j in range(4)]
                                    sw, sh = max(ws) - min(ws), max(hs) - min(hs)
                                    if sw > 5 and sh > 5:  # 变换后至少有合理尺寸
                                        orig_ratio = w / max(h, 1)
                                        out_ratio = sw / max(sh, 1)
                                        ratio_ok = 0.3 < out_ratio / max(orig_ratio, 0.01) < 3.0
                                    else:
                                        ratio_ok = False
                                    if ratio_ok:
                                        overlaps = False
                                        for ux1, uy1, ux2, uy2 in used_regions:
                                            if cx < ux2 and cx > ux1 and cy < uy2 and cy > uy1:
                                                overlaps = True
                                                break
                                        if not overlaps:
                                            # SIFT评分：基于匹配距离质量，高质量SIFT始终高于edge_tm
                                            dist_ratios = [m.distance / n.distance for m, n in matches if m in good_matches]
                                            avg_ratio = np.mean(dist_ratios) if dist_ratios else 1.0
                                            quality = (1.0 - avg_ratio) * 1.2 + 0.5
                                            count_bonus = min(len(good_matches) * 0.2, 0.6)
                                            sift_score = min(quality + count_bonus, 2.5)
                                            if sift_score > best_score:
                                                best_score = sift_score
                                                best_pos = (cx, cy)
                                                best_method = f"sift({len(good_matches)}matches)"
                except Exception as e:
                    log(f"  ⚠️ SIFT匹配失败: {e}")

            # 方法4: 灰度阈值模板匹配（低特征碎片首选，高特征碎片兜底）
            # 数字/图标对比度高，在阈值化背景中非常突出，不受纹理干扰
            if sift is not None and bg_thresh is not None:
                try:
                    # 动态判断碎片黑白比例，自动选择阈值方向
                    dark_pixels = np.sum(piece_gray < 80)
                    dark_ratio = dark_pixels / (pw * ph)
                    # 碎片以黑色为主（dark_ratio > 0.3）→ 黑字白底 → THRESH_BINARY_INV（黑变白）
                    # 碎片以白色为主（dark_ratio <= 0.3）→ 白字黑底 → THRESH_BINARY（白变白）
                    if dark_ratio > 0.3:
                        piece_thresh = cv2.threshold(piece_gray, 80, 255, cv2.THRESH_BINARY_INV)[1]
                        thresh_desc = "BINARY_INV"
                    else:
                        piece_thresh = cv2.threshold(piece_gray, 170, 255, cv2.THRESH_BINARY)[1]
                        thresh_desc = "BINARY"
                    res = cv2.matchTemplate(bg_thresh, piece_thresh, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    cand_x1, cand_y1 = max_loc
                    cx = cand_x1 + pw // 2
                    cy = cand_y1 + ph // 2
                    overlaps = False
                    for ux1, uy1, ux2, uy2 in used_regions:
                        if cand_x1 < ux2 and cand_x1 + pw > ux1 and cand_y1 < uy2 and cand_y1 + ph > uy1:
                            overlaps = True
                            break
                    # 灰度匹配最低得分：低特征碎片0.55即可（稀疏数字），高特征0.70
                    min_tm_score = 0.55 if is_low_feature else 0.70
                    if not overlaps and 0 <= cx < bg_w and 0 <= cy < bg_h and max_val > best_score and max_val >= min_tm_score:
                        best_score = max_val
                        best_pos = (cx, cy)
                        best_method = f"gray_tm(score={max_val:.3f})"
                        log(f"  💡 碎片{i+1} 使用灰度模板匹配 (score={max_val:.3f}, thresh={thresh_desc}, dark_ratio={dark_ratio:.2f}) [特征点={piece_kp_count}]")
                except Exception as e:
                    log(f"  ⚠️ 灰度模板匹配失败: {e}")

            if best_pos:
                pw_orig, ph_orig = piece_gray.shape[1], piece_gray.shape[0]
                used_regions.append((
                    max(0, best_pos[0] - pw_orig // 2),
                    max(0, best_pos[1] - ph_orig // 2),
                    min(bg_w, best_pos[0] + pw_orig // 2),
                    min(bg_h, best_pos[1] + ph_orig // 2),
                ))
                positions.append(best_pos)
                log(f"  ✅ 碎片{i+1}: ({best_pos[0]}, {best_pos[1]}), 相似度={best_score:.3f} [{best_method}]")
            else:
                log(f"  ❌ 碎片{i+1} 未找到 (最高分={best_score:.3f})")

        return positions

    def save_debug_images(self, bg: np.ndarray, pieces: List[np.ndarray], positions: List[Tuple[int, int]], output_dir: str):
        """保存调试图片"""
        import os
        os.makedirs(output_dir, exist_ok=True)

        # 保存背景图
        bg_path = os.path.join(output_dir, "debug_bg.png")
        cv2.imwrite(bg_path, bg)
        log(f"  📸 已保存背景图: {bg_path}")

        # 在背景图上标记匹配位置
        debug_bg = bg.copy()
        for i, (x, y) in enumerate(positions, start=1):
            cv2.circle(debug_bg, (x, y), 10, (0, 255, 0), -1)
            cv2.putText(debug_bg, f"P{i}", (x-20, y-15),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        debug_path = os.path.join(output_dir, "debug_matched.png")
        cv2.imwrite(debug_path, debug_bg)
        log(f"  📸 已保存匹配结果: {debug_path}")

        # 保存碎片图
        for i, piece in enumerate(pieces, start=1):
            if piece is not None:
                piece_path = os.path.join(output_dir, f"debug_piece{i}.png")
                cv2.imwrite(piece_path, piece)
                log(f"  📸 已保存碎片{i}: {piece_path}")

    # ---------- TDC JavaScript 指纹 & MD5 碰撞 ----------

    def _get_collect_and_eks(self, tdc_js: str, positions: List[Tuple[int, int]] = None) -> tuple:
        """执行 TDC JS，获取 collect 和 eks 指纹数据。
        优先使用 Node.js（服务端兼容性更好），回退到 py_mini_racer。"""
        aid = self.captcha_data.get("aid", CAPTCHA_AID) if hasattr(self, 'captcha_data') and self.captcha_data else CAPTCHA_AID
        env_path = os.path.join(SCRIPT_DIR, "env.js")
        tdc_path = os.path.join(SCRIPT_DIR, "tdc_debug.js")

        # 保存 TDC JS 到临时文件
        try:
            with open(tdc_path, "w", encoding="utf-8") as f:
                f.write(tdc_js)
        except Exception:
            pass

        # 优先使用 Node.js（服务端验证通过率高）
        runner_path = os.path.join(SCRIPT_DIR, "tdc_runner.js")
        if os.path.exists(runner_path) and os.path.exists(env_path):
            try:
                import subprocess
                positions_json = json.dumps(positions or [])
                result = subprocess.run(
                    ["node", runner_path, env_path, tdc_path, aid, positions_json],
                    capture_output=True, text=True, timeout=15, encoding="utf-8"
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout.strip())
                    collect = data.get("collect", "")
                    eks = data.get("eks", "")
                    if collect and collect != "---":
                        preview = data.get("collect_preview", "")[:200]
                        log(f"  ✅ TDC 指纹已生成 (Node.js)  collect_len={len(collect)}")
                        log(f"  📋 collect预览: {preview}")
                        return collect, eks
                    else:
                        log("  ⚠️ Node.js 返回空指纹，尝试 py_mini_racer")
            except Exception as e:
                log(f"  ⚠️ Node.js 异常: {type(e).__name__}: {e}，尝试 py_mini_racer")

        # 回退到 py_mini_racer
        if JS_OK:
            try:
                ctx = MiniRacer()
                log(f"  🐝 py_mini_racer 初始化成功，开始执行 TDC JS...")
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as f:
                        ctx.eval(f.read())
                ctx.eval(tdc_js)
                ctx.eval('window.TDC && "function" == typeof window.TDC.setData && window.TDC.setData("qf_7Pf__H")')

                if positions:
                    import time as _time
                    prev_x, prev_y = random.randint(100, 300), random.randint(100, 200)
                    for _ in range(3):
                        rand_x = random.randint(50, 650)
                        rand_y = random.randint(50, 450)
                        try:
                            ctx.eval(f'window.mouse_move && window.mouse_move({prev_x}, {prev_y}, {rand_x}, {rand_y})')
                        except Exception:
                            pass
                        prev_x, prev_y = rand_x, rand_y
                        _time.sleep(0.02)

                    for (x, y) in positions:
                        try:
                            ctx.eval(f'window.mouse_move && window.mouse_move({prev_x}, {prev_y}, {x}, {y})')
                        except Exception:
                            pass
                        try:
                            ctx.eval(f'window.mouse_click && window.mouse_click({x}, {y})')
                        except Exception:
                            pass
                        prev_x, prev_y = x, y
                        _time.sleep(0.05)

                collect = ctx.eval(
                    '(window.TDC && "function" == typeof window.TDC.getData) ? window.TDC.getData(true) || "---" : "------"'
                )
                eks = ctx.eval(
                    '(window.TDC && "function" == typeof window.TDC.getInfo) ? window.TDC.getInfo().info || "---" : "------"'
                )
                if collect and collect != "------":
                    log(f"  ✅ TDC 指纹已生成 (py_mini_racer)  collect_len={len(collect)}")
                    return collect, eks
                else:
                    log("  ⚠️ py_mini_racer 返回空指纹")
            except Exception as e:
                log(f"  ⚠️ py_mini_racer 异常: {type(e).__name__}: {e}")

        log("  ⚠️ 所有 TDC 执行方式均失败")
        return "", ""

    def _find_md5_collision(self, target_md5: str, prefix: str) -> tuple:
        """暴力枚举 MD5 碰撞，返回 (collision_str, elapsed_ms)"""
        start = time.time()
        num = 0
        while num < 114514000:
            candidate = prefix + str(num)
            md5_hash = hashlib.md5(candidate.encode("utf-8")).hexdigest()
            if md5_hash == target_md5:
                elapsed = int((time.time() - start) * 1000)
                return candidate, elapsed
            num += 1
        return prefix, int((time.time() - start) * 1000)

    # ---------- 提交验证码 ----------

    def submit_captcha(
        self,
        positions: List[Tuple[int, int]],
        sess_id: str,
        tdc_js: str,
        pow_prefix: str,
        pow_md5: str,
    ) -> Optional[dict]:
        """
        构造表单并提交 cap_union_new_verify
        返回 {errorCode, ticket, randstr} 或 None
        """
        # 构造 ans JSON（紧凑格式，无空格）
        ans = []
        for idx, (x, y) in enumerate(positions, start=1):
            ans.append({
                "elem_id": idx,
                "type": "DynAnswerType_POS",
                "data": f"{x},{y}"
            })
        ans_str = json.dumps(ans, separators=(",", ":"))

        # 获取 TDC 指纹（传入 positions 以模拟鼠标交互）
        collect, eks = self._get_collect_and_eks(tdc_js, positions)

        # 关键: tlg 必须是 collect 解码后的长度(和 captcha-service/tcaptcha.py 一致)
        # collect_orig 是 URL 编码的字符串,需要用 unquote 解码后计算长度
        # 例如: collect 编码后 1790 字节,解码后 1656 字节 → tlg=1656
        collect_orig = collect or ""
        eks_orig = eks or ""
        from urllib.parse import unquote, quote_plus
        tlg = len(unquote(collect_orig))

        # MD5 碰撞
        pow_answer, pow_time = self._find_md5_collision(pow_md5, pow_prefix)

        # 详细调试:打印提交参数
        log(f"  🔍 提交参数详情:")
        log(f"     sess={sess_id[:20]}...  ans={ans_str}")
        log(f"     collect_len={len(collect_orig)}  tlg={tlg}")
        log(f"     eks_len={len(eks_orig)}")
        log(f"     pow_answer={pow_answer}  pow_calc_time={pow_time}ms")

        # 关键: collect 已经是 URL 编码的,不能再用 dict 提交(否则双重编码)
        # 必须手动构造 form body,和 v3.1 成功版本一致
        form_data = (
            f"sess={quote_plus(sess_id)}"
            f"&ans={quote_plus(ans_str)}"
            f"&collect={collect_orig}"
            f"&tlg={tlg}"
            f"&eks={quote_plus(eks_orig)}"
            f"&pow_answer={quote_plus(pow_answer)}"
            f"&pow_calc_time={str(pow_time)}"
        )
        url = CAPTCHA_BASE + "/cap_union_new_verify"
        h = dict(self.sess.s.headers)
        h["Referer"] = APP_URL + "/"
        h["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            r = self.sess.s.post(url, data=form_data, headers=h, timeout=20)
            r.raise_for_status()
            result = r.json()
        except Exception as e:
            log(f"  ❌ CAPTCHA POST 失败: {e}")
            return None

        error_code = int(result.get("errorCode", -1))
        log(f"  📮 验证码响应: {result}")
        if error_code == 0:
            self.ticket     = result.get("ticket", "")
            self.randstr    = result.get("randstr", "")
            log(f"  ✅ 验证码提交成功  ticket={self.ticket[:20]}...  randstr={self.randstr[:20]}...")
            return result
        else:
            log(f"  ❌ 验证码提交失败  errorCode={error_code}  msg={result.get('errMessage', '')}")
            return result

    # ---------- 主求解流程 ----------

    def solve(self) -> Optional[Dict]:
        """
        完整求解验证码
        返回: {"clicks": "x1,y1|x2,y2|x3,y3", "ticket": str, "randstr": str} 或 None
        """
        log("\n🔍 开始求解验证码...")

        # 1. 获取验证码配置
        prehandle = self.get_prehandle()
        if not prehandle:
            log("  ❌ 无法获取验证码配置")
            return None

        sess_id = prehandle.get("sess", "")
        if not sess_id:
            log("  ❌ sess 为空")
            return None

        self.captcha_data = prehandle

        # 2. 下载 TDC JS 和获取 pow 配置
        tdc_path = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("tdc_path", "")
        pow_cfg  = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("pow_cfg", {})
        pow_prefix = pow_cfg.get("prefix", "")
        pow_md5    = pow_cfg.get("md5", "")

        tdc_js = ""
        if tdc_path:
            tdc_raw = self.sess.download_img(tdc_path)
            if tdc_raw:
                tdc_js = tdc_raw.decode("utf-8", errors="replace")
                log(f"  📜 TDC JS 已下载 ({len(tdc_js)} 字符)")
            else:
                log("  ⚠️ TDC JS 下载失败，将使用空指纹")
        else:
            log("  ⚠️ 未找到 TDC JS 路径")

        log(f"  🔐 pow_prefix={pow_prefix}  pow_md5={pow_md5}")

        # 3. 下载图片
        bg, sprite = self.download_images(prehandle)
        if bg is None or sprite is None:
            return None

        # 4. 分割碎片
        ins_elem_cfg = prehandle.get("data", {}).get("dyn_show_info", {}).get("ins_elem_cfg")
        pieces = self.split_sprite(sprite, ins_elem_cfg)
        log(f"  🔪 已分割为 {len(pieces)} 个碎片")

        # 模拟用户阅读验证码（避免请求过快被风控）
        time.sleep(random.uniform(1.5, 3.0))

        # 5. 匹配位置（最多重试 CAPTCHA_RETRIES 次）
        num_pieces = len(pieces)
        debug_dir = os.path.join(SCRIPT_DIR, "debug_output")
        for attempt in range(1, CAPTCHA_RETRIES + 1):
            positions = self.find_positions(bg, pieces)

            # 保存调试图片
            self.save_debug_images(bg, pieces, positions, debug_dir)

            log(f"  🎯 第{attempt}次匹配: 找到 {len(positions)}/{num_pieces} 个图标")

            if len(positions) == num_pieces and num_pieces > 0:
                # 按碎片顺序点击（elem_id 1/2/3 对应碎片提示顺序，不能按坐标排序）
                sorted_pos = positions
                self.coords = sorted_pos
                log(f"  📍 点击序列: {' | '.join(f'({x},{y})' for x,y in sorted_pos)}")

                # 6. 提交验证
                result = self.submit_captcha(sorted_pos, sess_id, tdc_js, pow_prefix, pow_md5)
                if result and int(result.get("errorCode", -1)) == 0:
                    return {
                        "clicks": "|".join(f"{x},{y}" for x, y in sorted_pos),
                        "ticket": self.ticket,
                        "randstr": self.randstr,
                    }
                else:
                    # 验证码校验失败，刷新重试
                    err_code = result.get("errorCode", "?") if result else "?"
                    log(f"  🔄 验证码校验失败 (errorCode={err_code})，刷新重试...")
                    if attempt < CAPTCHA_RETRIES:
                        # 增加延迟，避免触发腾讯风控（和 v3.1 一致）
                        time.sleep(random.uniform(2, 4))
                        # 重新获取完整的 prehandle,确保 sess/pow_cfg/TDC JS 都是新的
                        new_prehandle = self.get_prehandle()
                        if new_prehandle:
                            sess_id = new_prehandle.get("sess", sess_id)
                            new_tdc_path = new_prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("tdc_path", "")
                            new_pow_cfg = new_prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("pow_cfg", {})
                            if new_pow_cfg:
                                pow_prefix = new_pow_cfg.get("prefix", pow_prefix)
                                pow_md5 = new_pow_cfg.get("md5", pow_md5)
                            if new_tdc_path:
                                new_tdc_raw = self.sess.download_img(new_tdc_path)
                                if new_tdc_raw:
                                    tdc_js = new_tdc_raw.decode("utf-8", errors="replace")
                            new_bg, new_sprite = self.download_images(new_prehandle)
                            if new_bg is not None and new_sprite is not None:
                                bg = new_bg
                                sprite = new_sprite
                                pieces = self.split_sprite(sprite, new_prehandle.get("data", {}).get("dyn_show_info", {}).get("ins_elem_cfg"))
                                num_pieces = len(pieces)
                            log(f"  🔐 已刷新 pow_prefix={pow_prefix}  pow_md5={pow_md5}")
                        time.sleep(random.uniform(1, 2))
                    continue
            else:
                if attempt < CAPTCHA_RETRIES:
                    log(f"  🔄 匹配不完整，重新匹配...")
                    time.sleep(random.uniform(0.5, 1.5))
                    continue
                else:
                    log("  ❌ 已达到最大重试次数")
                    return None

        return None


# ============================================================
# OCR 图形库求解器
# ============================================================

class OCRExpertSolver(CaptchaSolver):
    """
    基于图形库的验证码求解器
    原理：图形库用于哈希预过滤加速候选筛选，坐标匹配使用父类 SIFT+边缘检测
    """

    SHAPE_LIB_DIR = os.path.join(SCRIPT_DIR, "shape_library")

    def __init__(self, sess: Session):
        super().__init__(sess)
        self.shape_library: List[dict] = []
        self._load_shape_library()

    def _piece_hash(self, piece_gray: np.ndarray) -> str:
        """计算碎片的结构哈希：4x4分块灰度统计 + 暗像素比例"""
        # 裁剪内容区域
        binary = (piece_gray < 180).astype(np.uint8) * 255
        coords = cv2.findNonZero(binary)
        if coords is None:
            return '0' * 64 + '00'
        x, y, w, h = cv2.boundingRect(coords)
        margin = 2
        x = max(0, x - margin)
        y = max(0, y - margin)
        w = min(w + 2 * margin, piece_gray.shape[1] - x)
        h = min(h + 2 * margin, piece_gray.shape[0] - y)
        content = piece_gray[y:y+h, x:x+w]
        # 缩放到16x16
        resized = cv2.resize(content, (16, 16))
        # 4x4分块统计
        blocks = []
        for by in range(4):
            for bx in range(4):
                region = resized[by*4:(by+1)*4, bx*4:(bx+1)*4]
                blocks.append(region.mean())
        median_val = np.median(blocks)
        hash_bits = ''.join('1' if b > median_val else '0' for b in blocks)
        # 暗像素比例
        dark_ratio = float((piece_gray < 80).sum()) / (piece_gray.size)
        ratio_bins = 0 if dark_ratio < 0.08 else (1 if dark_ratio < 0.25 else 2)
        return hash_bits + f"{ratio_bins:02d}"

    def _load_shape_library(self):
        """加载图形库（兼容新旧格式）"""
        if not os.path.exists(self.SHAPE_LIB_DIR):
            log(f"  ⚠️ 图形库目录不存在: {self.SHAPE_LIB_DIR}")
            log("  💡 请先运行 collect_shapes.py 收集图形")
            return

        self.shape_library = []
        all_files = sorted(os.listdir(self.SHAPE_LIB_DIR))
        shape_files = [f for f in all_files if f.startswith("shape_") and f.endswith(".png")]
        thumb_files = {f.replace("thumb_", "shape_").replace(".png", ""): f
                       for f in all_files if f.startswith("thumb_") and f.endswith(".png")}

        for f in shape_files:
            path = os.path.join(self.SHAPE_LIB_DIR, f)
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            base = f.replace(".png", "")
            parts = base.split("_")  # ['shape', '0001', 'p1', 'hash']
            piece_id = int(parts[2].replace("p", "")) - 1 if len(parts) >= 3 else 0
            hash_val = parts[-1] if len(parts) >= 4 else ""
            self.shape_library.append({
                "file": f, "path": path, "piece_id": piece_id,
                "hash": hash_val, "image": img,
            })

        for key, thumb_f in thumb_files.items():
            if key in [item["file"].replace(".png", "") for item in self.shape_library]:
                continue
            thumb_path = os.path.join(self.SHAPE_LIB_DIR, thumb_f)
            img = cv2.imread(thumb_path, cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            parts = thumb_f.replace(".png", "").split("_")
            piece_id = int(parts[2].replace("p", "")) - 1 if len(parts) >= 3 else 0
            self.shape_library.append({
                "file": thumb_f, "path": thumb_path, "piece_id": piece_id,
                "hash": "", "image": img,
            })

        # 构建哈希索引
        self._hash_index: dict = {}
        for idx, item in enumerate(self.shape_library):
            h = item.get("hash", "")
            if h:
                h8 = h[:8]
                if h8 not in self._hash_index:
                    self._hash_index[h8] = []
                self._hash_index[h8].append(idx)

        log(f"  📚 图形库加载完成，共 {len(self.shape_library)} 个图形（哈希桶: {len(self._hash_index)}）")

    def _learn_piece(self, piece: np.ndarray, piece_idx: int):
        """匹配失败的碎片自动加入图形库（学习机制，提升后续成功率）
        入库后立即热加载到当前进程，同一次求解的后续重试即可使用
        """
        try:
            if piece is None or piece.size == 0:
                return
            # 图形库容量上限，防止错误样本无限增长
            if len(self.shape_library) >= 500:
                log(f"  ⚠️ 图形库已达上限 500，跳过入库")
                return
            piece_bgr = cv2.cvtColor(piece, cv2.COLOR_BGRA2BGR) if len(piece.shape) == 4 else piece
            piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)
            piece_hash = self._piece_hash(piece_gray)

            # 去重: 同碎片位+同哈希已存在则跳过
            for item in self.shape_library:
                if item.get("hash") == piece_hash and item.get("piece_id") == piece_idx:
                    log(f"  📚 碎片{piece_idx+1} 图形已存在（{item['file']}），跳过入库")
                    return

            # 计算新编号（取库内最大编号+1）
            max_id = 0
            for item in self.shape_library:
                try:
                    max_id = max(max_id, int(item["file"].split("_")[1]))
                except (ValueError, IndexError):
                    pass
            new_id = max_id + 1
            fname = f"shape_{new_id:04d}_p{piece_idx+1}_{piece_hash}.png"
            fpath = os.path.join(self.SHAPE_LIB_DIR, fname)

            cv2.imwrite(fpath, piece)  # 保留原始 BGRA 通道

            # 热加载: 追加到当前进程的图形库与哈希索引
            self.shape_library.append({
                "file": fname, "path": fpath, "piece_id": piece_idx,
                "hash": piece_hash, "image": piece,
            })
            h8 = piece_hash[:8]
            self._hash_index.setdefault(h8, []).append(len(self.shape_library) - 1)
            log(f"  📚 碎片{piece_idx+1} 已加入图形库: {fname}（共 {len(self.shape_library)} 个）")
        except Exception as e:
            log(f"  ⚠️ 碎片{piece_idx+1} 入库失败: {e}")

    @staticmethod
    def _remove_bg(gray: np.ndarray) -> tuple:
        """去除背景，返回(二值图mask, 阈值类型字符串)
        使用OTSU自动阈值，自动适配深浅背景
        """
        h, w = gray.shape
        # 检测背景主色调（取四角像素均值）
        margin = max(5, min(w, h) // 20)
        corners = [
            gray[0:margin, 0:margin],
            gray[0:margin, w-margin:w],
            gray[h-margin:h, 0:margin],
            gray[h-margin:h, w-margin:w],
        ]
        bg_color = int(np.mean(np.concatenate([c.ravel() for c in corners])))

        if bg_color > 128:
            # 浅色背景 → 黑色内容（如灰底黑字"7"）→ 只保留深色
            _, mask = cv2.threshold(gray, bg_color - 50, 255, cv2.THRESH_BINARY_INV)
            thresh_type = "BINARY_INV"
        else:
            # 深色背景 → 白色内容
            _, mask = cv2.threshold(gray, bg_color + 50, 255, cv2.THRESH_BINARY)
            thresh_type = "BINARY"

        return mask, thresh_type

    def find_positions_with_library(self, bg: np.ndarray, pieces: List[np.ndarray]) -> List[Tuple[int, int]]:
        """
        使用图形库+模板匹配精确定位：
        1. 对每个碎片，在图形库中通过SIFT找到最相似的图形
        2. 用图形库图片去除背景后的二值图在背景图中做模板匹配
        3. 如果图形库匹配失败，降级到直接SIFT匹配碎片
        """
        positions = []
        bg_h, bg_w = bg.shape[:2]
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        # 对背景图也做二值化，与库图/碎片二值图匹配
        _, bg_mask = cv2.threshold(bg_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        used_regions: List[Tuple[int, int, int, int]] = []

        sift = None
        orb = None
        try:
            sift = cv2.SIFT_create()
            orb = cv2.ORB_create(nfeatures=500)
        except Exception:
            pass

        # 预计算图形库中所有图片的SIFT特征
        lib_features = []
        lib_masks = []
        for item in self.shape_library:
            lib_img = item["image"]
            lib_gray = cv2.cvtColor(lib_img, cv2.COLOR_BGRA2GRAY) if lib_img.shape[-1] == 4 else cv2.cvtColor(lib_img, cv2.COLOR_BGR2GRAY)
            _, lib_des = sift.detectAndCompute(lib_gray, None) if sift is not None else (None, None)
            lib_features.append(lib_des)
            lib_mask, _ = self._remove_bg(lib_gray)
            lib_masks.append(lib_mask)

        # 预计算背景图SIFT特征
        bg_kp, bg_des = None, None
        if sift is not None:
            try:
                bg_kp, bg_des = sift.detectAndCompute(bg_gray, None)
            except Exception:
                pass

        for i, piece in enumerate(pieces):
            if piece is None or piece.size == 0:
                log(f"  ⚠️ 碎片 {i+1} 为空")
                continue

            piece_bgr = cv2.cvtColor(piece, cv2.COLOR_BGRA2BGR) if len(piece.shape) == 4 else piece
            piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)
            ph, pw = piece_gray.shape[:2]
            piece_mask, _ = self._remove_bg(piece_gray)

            # 步骤1: 在图形库中找最相似的图形
            best_lib_idx = -1
            best_lib_score = 0.0
            lib_method = ""

            # 哈希预筛选：快速缩小候选范围
            piece_hash8 = self._piece_hash(piece_gray)[:8] if hasattr(self, '_piece_hash') else ""
            hash_candidates = self._hash_index.get(piece_hash8, list(range(len(lib_features)))) if hasattr(self, '_hash_index') else list(range(len(lib_features)))
            log(f"  🔍 碎片{i+1} hash前缀={piece_hash8} 候选数={len(hash_candidates)}/{len(lib_features)}")

            if sift is not None and lib_features:
                piece_kp, piece_des = sift.detectAndCompute(piece_gray, None)
                piece_kp_count = len(piece_kp) if piece_kp else 0
                log(f"  🔍 碎片{i+1} SIFT特征点数: {piece_kp_count}")
                if piece_des is not None and len(piece_des) >= 2:
                    flann = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 50})
                    for lib_idx in hash_candidates:
                        lib_des = lib_features[lib_idx]
                        if lib_des is None or len(lib_des) < 2:
                            continue
                        matches = flann.knnMatch(piece_des, lib_des, k=2)
                        good = [(m, n) for m, n in matches if m.distance < 0.90 * n.distance]
                        if len(good) >= 2:
                            score = len(good) * 0.1 + (1.0 - np.mean([m.distance / n.distance for m, n in good])) * 0.5
                            log(f"    库[{lib_idx}] lib={self.shape_library[lib_idx]['file'][:15]} matches={len(good)} score={score:.3f}")
                            if score > best_lib_score:
                                best_lib_score = score
                                best_lib_idx = lib_idx
                                lib_method = f"lib({len(good)}matches)"
            # ORB 回退：SIFT 没匹配到时用 ORB
            if best_lib_idx < 0 and orb is not None:
                piece_kp2, piece_des2 = orb.detectAndCompute(piece_gray, None)
                if piece_des2 is not None:
                    for lib_idx, lib_des in enumerate(lib_features):
                        if lib_des is None or len(lib_des) < 2:
                            continue
                        matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False).knnMatch(piece_des2, lib_des, k=2)
                        good = [(m, n) for m, n in matches if m.distance < 0.75 * n.distance]
                        if len(good) >= 2:
                            score = len(good) * 0.1 + (1.0 - np.mean([m.distance / n.distance for m, n in good])) * 0.5
                            if score > best_lib_score:
                                best_lib_score = score
                                best_lib_idx = lib_idx
                                lib_method = f"orb({len(good)}matches)"

            # 步骤2: 多种匹配策略，选择最优结果
            if best_lib_idx >= 0 and best_lib_score > 0.3:
                lib_img = self.shape_library[best_lib_idx]["image"]
                lib_gray_img = cv2.cvtColor(lib_img, cv2.COLOR_BGRA2GRAY) if lib_img.shape[-1] == 4 else cv2.cvtColor(lib_img, cv2.COLOR_BGR2GRAY)
                lib_h, lib_w = lib_gray_img.shape
                lib_file = self.shape_library[best_lib_idx]["file"]
                log(f"  📖 碎片{i+1} → 图形库匹配: {lib_file} [{lib_method}]")

                # 策略B: 多尺度二值化匹配（背景中字符比碎片大1.3~1.8倍）
                lib_mask_bin, _ = self._remove_bg(lib_gray_img)
                bg_dark = cv2.threshold(bg_gray, 120, 255, cv2.THRESH_BINARY_INV)[1]
                lh, lw2 = lib_mask_bin.shape
                best_pos_b, best_score_b = None, 0.0
                for scale in [1.3, 1.4, 1.5, 1.6, 1.7, 1.8]:
                    new_w = max(10, int(lw2 * scale))
                    new_h = max(10, int(lh * scale))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    scaled_mask = cv2.resize(lib_mask_bin, (new_w, new_h))
                    res = cv2.matchTemplate(bg_dark, scaled_mask, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    cx, cy = max_loc[0] + new_w // 2, max_loc[1] + new_h // 2
                    if max_val > best_score_b and 0 < cx < bg_w and 0 < cy < bg_h:
                        if not any(cx < ux2 and cx > ux1 and cy < uy2 and cy > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                            best_score_b = max_val
                            best_pos_b = (cx, cy)

                # 策略C: 旋转二值匹配（二值匹配分数不足时，处理背景中旋转的字符）
                best_pos_c, best_score_c, best_angle = None, 0.0, 0
                if best_score_b < 0.55:
                    for angle in range(15, 360, 15):
                        M = cv2.getRotationMatrix2D((lw2 // 2, lh // 2), angle, 1.0)
                        rotated = cv2.warpAffine(lib_mask_bin, M, (lw2, lh))
                        for scale in [1.5, 1.6, 1.7]:
                            new_w = max(10, int(lw2 * scale))
                            new_h = max(10, int(lh * scale))
                            if new_h > bg_h or new_w > bg_w:
                                continue
                            scaled_rot = cv2.resize(rotated, (new_w, new_h))
                            res = cv2.matchTemplate(bg_dark, scaled_rot, cv2.TM_CCOEFF_NORMED)
                            _, max_val, _, max_loc = cv2.minMaxLoc(res)
                            cx, cy = max_loc[0] + new_w // 2, max_loc[1] + new_h // 2
                            if max_val > best_score_c and 0 < cx < bg_w and 0 < cy < bg_h:
                                if not any(cx < ux2 and cx > ux1 and cy < uy2 and cy > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                                    best_score_c = max_val
                                    best_pos_c = (cx, cy)
                                    best_angle = angle

                # 选择最优策略
                best_pos, best_score, best_method = None, 0.0, ""
                if best_pos_b and best_score_b >= 0.30:
                    best_pos, best_score, best_method = best_pos_b, best_score_b, "bin_tm"
                if best_pos_c and best_score_c > best_score:
                    best_pos, best_score, best_method = best_pos_c, best_score_c, f"rot_tm({best_angle}°)"
                log(f"  🎯 碎片{i+1} 匹配结果: score={best_score:.3f} pos={best_pos} [{best_method}]")
                if best_pos and best_score >= 0.55:
                    positions.append(best_pos)
                    used_regions.append((best_pos[0] - pw//2, best_pos[1] - ph//2, best_pos[0] + pw//2, best_pos[1] + ph//2))
                    log(f"  ✅ 碎片{i+1}: {best_pos}, 相似度={best_score:.3f} [{best_method}]")
                    continue

            # 步骤3: 降级到直接匹配碎片
            log(f"  📖 碎片{i+1} → 图形库无匹配，使用碎片直接定位")
            piece_edges = cv2.Canny(piece_gray, 50, 150)
            has_alpha = len(piece.shape) == 4
            piece_alpha = piece[:, :, 3] if has_alpha else None
            piece_kp_count = 0
            is_low_feature = False
            if sift is not None:
                try:
                    _kp, _des = sift.detectAndCompute(piece_gray, None)
                    piece_kp_count = len(_kp) if _kp else 0
                    is_low_feature = piece_kp_count < 30
                except Exception:
                    pass
            log(f"  🔍 碎片{i+1} 特征点数: {piece_kp_count}")

            best_pos, best_score = None, 0.0
            best_method = ""

            # 方法1: 边缘检测（仅高特征碎片）
            if not is_low_feature:
                for scale in [0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0]:
                    new_w = max(10, int(pw * scale))
                    new_h = max(10, int(ph * scale))
                    scaled_edges = cv2.resize(piece_edges, (new_w, new_h))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    res = cv2.matchTemplate(bg_gray, scaled_edges, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    cx, cy = max_loc[0] + new_w // 2, max_loc[1] + new_h // 2
                    if max_val > best_score and not any(max_loc[0] < ux2 and max_loc[0] + new_w > ux1 and max_loc[1] < uy2 and max_loc[1] + new_h > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                        best_score = max_val
                        best_pos = (cx, cy)
                        best_method = f"edge_tm(scale={scale})"

            # 方法2: 带alpha mask的模板匹配
            if has_alpha and best_score < 0.55 and not is_low_feature:
                for scale in [0.8, 0.9, 1.0, 1.1, 1.2, 1.5]:
                    new_w = max(10, int(pw * scale))
                    new_h = max(10, int(ph * scale))
                    scaled_piece = cv2.resize(piece_gray, (new_w, new_h))
                    scaled_alpha = cv2.resize(piece_alpha, (new_w, new_h))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    res = cv2.matchTemplate(bg_gray, scaled_piece, cv2.TM_CCORR_NORMED, mask=scaled_alpha)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    cx, cy = max_loc[0] + new_w // 2, max_loc[1] + new_h // 2
                    if max_val > best_score and not any(max_loc[0] < ux2 and max_loc[0] + new_w > ux1 and max_loc[1] < uy2 and max_loc[1] + new_h > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                        best_score = max_val
                        best_pos = (cx, cy)
                        best_method = f"mask_tm(scale={scale})"

            # 方法3: SIFT特征匹配
            if sift is not None and bg_kp is not None:
                try:
                    _kp2, piece_des2 = sift.detectAndCompute(piece_gray, None)
                    if piece_des2 is not None and len(piece_des2) > 1:
                        flann2 = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 50})
                        matches2 = flann2.knnMatch(piece_des2, bg_des, k=2)
                        good2 = [m for m, n in matches2 if m.distance < 0.90 * n.distance]
                        if len(good2) >= 4:
                            src_pts = np.float32([piece_kp2[m.queryIdx].pt for m in good2]).reshape(-1, 1, 2)
                            dst_pts = np.float32([bg_kp[m.trainIdx].pt for m in good2]).reshape(-1, 1, 2)
                            matrix, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                            if matrix is not None:
                                h2, w2 = piece_gray.shape[:2]
                                pts = np.float32([[0,0],[w2,0],[w2,h2],[0,h2]]).reshape(-1,1,2)
                                dst = cv2.perspectiveTransform(pts, matrix)
                                cx = int((dst[0][0][0] + dst[2][0][0]) / 2)
                                cy = int((dst[0][0][1] + dst[2][0][1]) / 2)
                                if 0 <= cx < bg_w and 0 <= cy < bg_h:
                                    # 几何有效性校验
                                    ws2 = [dst[j][0][0] for j in range(4)]
                                    hs2 = [dst[j][0][1] for j in range(4)]
                                    sw2, sh2 = max(ws2) - min(ws2), max(hs2) - min(hs2)
                                    ratio_ok2 = sw2 > 5 and sh2 > 5 and 0.3 < (sw2/max(sh2,1)) / (w2/max(h2,1)) < 3.0
                                    if ratio_ok2 and not any(cx < ux2 and cx > ux1 and cy < uy2 and cy > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                                        ratios = [m.distance/n.distance for m in good2]
                                        quality = (1.0 - np.mean(ratios)) * 1.2 + 0.5
                                        count_bonus = min(len(good2) * 0.2, 0.6)
                                        sift_score = min(quality + count_bonus, 2.5)
                                        if sift_score > best_score:
                                            best_score = sift_score
                                            best_pos = (cx, cy)
                                            best_method = f"sift({len(good2)}matches)"
                except Exception:
                    pass
            # ORB 特征匹配回退
            if orb is not None and bg_kp is not None:
                try:
                    piece_kp3, piece_des3 = orb.detectAndCompute(piece_gray, None)
                    _, bg_kp3, bg_des3 = orb.detectAndCompute(bg_gray, None)
                    if piece_des3 is not None and bg_des3 is not None and len(piece_des3) > 2:
                        bfm = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
                        matches3 = bfm.knnMatch(piece_des3, bg_des3, k=2)
                        good3 = [m for m, n in matches3 if m.distance < 0.75 * n.distance]
                        if len(good3) >= 4:
                            src_pts = np.float32([piece_kp3[m.queryIdx].pt for m in good3]).reshape(-1, 1, 2)
                            dst_pts = np.float32([bg_kp3[m.trainIdx].pt for m in good3]).reshape(-1, 1, 2)
                            matrix, _ = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                            if matrix is not None:
                                h3, w3 = piece_gray.shape[:2]
                                pts = np.float32([[0,0],[w3,0],[w3,h3],[0,h3]]).reshape(-1,1,2)
                                dst = cv2.perspectiveTransform(pts, matrix)
                                cx = int((dst[0][0][0] + dst[2][0][0]) / 2)
                                cy = int((dst[0][0][1] + dst[2][0][1]) / 2)
                                if 0 <= cx < bg_w and 0 <= cy < bg_h:
                                    ws3 = [dst[j][0][0] for j in range(4)]
                                    hs3 = [dst[j][0][1] for j in range(4)]
                                    sw3, sh3 = max(ws3) - min(ws3), max(hs3) - min(hs3)
                                    ratio_ok3 = sw3 > 5 and sh3 > 5 and 0.3 < (sw3/max(sh3,1)) / (w3/max(h3,1)) < 3.0
                                    if ratio_ok3 and not any(cx < ux2 and cx > ux1 and cy < uy2 and cy > uy1 for ux1, uy1, ux2, uy2 in used_regions):
                                        orb_score = min(len(good3) * 0.3 + 0.5, 2.0)
                                        if orb_score > best_score:
                                            best_score = orb_score
                                            best_pos = (cx, cy)
                                            best_method = f"orb_bg({len(good3)}matches)"
                except Exception:
                    pass

            # 方法4: 二值化多尺度模板匹配（背景中字符比碎片大1.3~1.8倍）
            try:
                piece_mask_bin, _ = self._remove_bg(piece_gray)
                bg_dark2 = cv2.threshold(bg_gray, 120, 255, cv2.THRESH_BINARY_INV)[1]
                ph2, pw2 = piece_mask_bin.shape
                for scale in [1.3, 1.4, 1.5, 1.6, 1.7, 1.8]:
                    new_w = max(10, int(pw2 * scale))
                    new_h = max(10, int(ph2 * scale))
                    if new_h > bg_h or new_w > bg_w:
                        continue
                    scaled_mask = cv2.resize(piece_mask_bin, (new_w, new_h))
                    res = cv2.matchTemplate(bg_dark2, scaled_mask, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    cx, cy = max_loc[0] + new_w // 2, max_loc[1] + new_h // 2
                    overlaps = any(max_loc[0] < ux2 and max_loc[0] + new_w > ux1 and max_loc[1] < uy2 and max_loc[1] + new_h > uy1 for ux1, uy1, ux2, uy2 in used_regions)
                    if not overlaps and 0 < cx < bg_w and 0 < cy < bg_h and max_val > best_score and max_val >= 0.55:
                        best_score = max_val
                        best_pos = (cx, cy)
                        best_method = f"bin_tm(scale={scale})"
                        log(f"  💡 碎片{i+1} 二值化匹配 (score={max_val:.3f}, scale={scale}) [特征点={piece_kp_count}]")
            except Exception as e:
                log(f"  ⚠️ 二值化匹配失败: {e}")

            if best_pos:
                positions.append(best_pos)
                used_regions.append((best_pos[0] - pw//2, best_pos[1] - ph//2, best_pos[0] + pw//2, best_pos[1] + ph//2))
                log(f"  ✅ 碎片{i+1}: {best_pos}, 相似度={best_score:.3f} [{best_method}]")
            else:
                log(f"  ❌ 碎片{i+1} 匹配失败")
                # 学习机制: 失败碎片自动入库，下次匹配成功率更高
                self._learn_piece(piece, i)

        return positions

    def solve(self, max_retries: int = CAPTCHA_RETRIES) -> Optional[Dict]:
        """使用图形库求解验证码（逻辑完全参照旧系统）"""
        log("\n🔍 开始 OCR 图形库求解验证码...")

        # 1. 获取验证码配置
        prehandle = self.get_prehandle()
        if not prehandle:
            log("  ❌ 无法获取验证码配置")
            return None

        sess_id = prehandle.get("sess", "")
        if not sess_id:
            log("  ❌ sess 为空")
            return None

        self.captcha_data = prehandle

        # 2. 下载 TDC JS 和 pow 配置
        tdc_path = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("tdc_path", "")
        pow_cfg = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("pow_cfg", {})
        pow_prefix = pow_cfg.get("prefix", "")
        pow_md5 = pow_cfg.get("md5", "")

        tdc_js = ""
        if tdc_path:
            tdc_raw = self.sess.download_img(tdc_path)
            if tdc_raw:
                tdc_js = tdc_raw.decode("utf-8", errors="replace")
                log(f"  📜 TDC JS 已下载 ({len(tdc_js)} 字符)")
            else:
                log("  ⚠️ TDC JS 下载失败")
        else:
            log("  ⚠️ 未找到 TDC JS 路径")

        log(f"  🔐 pow_prefix={pow_prefix}  pow_md5={pow_md5}")

        # 3. 下载图片
        bg, sprite = self.download_images(prehandle)
        if bg is None or sprite is None:
            return None

        # 4. 分割碎片
        ins_elem_cfg = prehandle.get("data", {}).get("dyn_show_info", {}).get("ins_elem_cfg")
        pieces = self.split_sprite(sprite, ins_elem_cfg)
        log(f"  🔪 已分割为 {len(pieces)} 个碎片")

        # 5. 匹配位置（带完整重试逻辑）
        num_pieces = 3
        debug_dir = os.path.join(SCRIPT_DIR, "debug_output")
        for attempt in range(1, max_retries + 1):
            # 每次重试都重新获取配置和图片
            prehandle = self.get_prehandle()
            if not prehandle:
                log(f"  ❌ 第{attempt}次: 无法获取验证码配置")
                continue

            sess_id = prehandle.get("sess", "")
            tdc_path = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("tdc_path", "")
            pow_cfg = prehandle.get("data", {}).get("comm_captcha_cfg", {}).get("pow_cfg", {})
            pow_prefix = pow_cfg.get("prefix", "")
            pow_md5 = pow_cfg.get("md5", "")

            tdc_js = ""
            if tdc_path:
                tdc_raw = self.sess.download_img(tdc_path)
                if tdc_raw:
                    tdc_js = tdc_raw.decode("utf-8", errors="replace")

            log(f"  🔐 已刷新 pow_prefix={pow_prefix}  pow_md5={pow_md5}")

            # 下载图片
            bg, sprite = self.download_images(prehandle)
            if bg is None or sprite is None:
                log(f"  ❌ 第{attempt}次: 图片下载失败")
                continue

            # 分割碎片
            ins_elem_cfg = prehandle.get("data", {}).get("dyn_show_info", {}).get("ins_elem_cfg")
            pieces = self.split_sprite(sprite, ins_elem_cfg)
            log(f"  🔪 已分割为 {len(pieces)} 个碎片")

            # 模拟用户阅读
            time.sleep(random.uniform(0.5, 1.0))

            # 使用图形库+模板匹配定位
            positions = self.find_positions_with_library(bg, pieces)

            # 保存调试图片
            self.save_debug_images(bg, pieces, positions, debug_dir)

            log(f"  🎯 第{attempt}次匹配: 找到 {len(positions)}/{num_pieces} 个图标")

            if len(positions) == num_pieces and num_pieces > 0:
                # 按碎片顺序点击（elem_id 1/2/3 对应碎片提示顺序，不能按坐标排序）
                sorted_pos = positions
                self.coords = sorted_pos
                log(f"  📍 点击序列: {' | '.join(f'({x},{y})' for x,y in sorted_pos)}")

                # 提交前模拟人工延迟（0.8~1.5秒）
                time.sleep(random.uniform(0.8, 1.5))

                # 7. 提交验证
                result = self.submit_captcha(sorted_pos, sess_id, tdc_js, pow_prefix, pow_md5)
                if result and int(result.get("errorCode", -1)) == 0:
                    return {
                        "clicks": "|".join(f"{x},{y}" for x, y in sorted_pos),
                        "ticket": self.ticket,
                        "randstr": self.randstr,
                    }
                else:
                    err_code = result.get("errorCode", "?") if result else "?"
                    log(f"  🔄 验证码校验失败 (errorCode={err_code})，刷新重试...")
                    # 循环会自动重新获取配置和图片
                    continue
            else:
                log(f"  🔄 匹配不完整 ({len(positions)}/{num_pieces})，重新匹配...")
                continue

        return None


# ============================================================
# 签到主流程
# ============================================================

class RainyunSigner:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.sess     = Session(username=username)
        self.logged_in = False

    # ---------- 登录 ----------

    def login(self) -> bool:
        log("\n" + "=" * 50)
        log("🔐 登录雨云")
        log("=" * 50)

        # 尝试使用已有 cookies
        if self.sess.load_cookies():
            info = self.sess.get("/user/")
            if info and info.get("code") == 200:
                log("  ✅ 使用缓存会话登录成功")
                self.logged_in = True
                return True

        # 执行登录
        payload = {"field": self.username, "password": self.password}
        result = self.sess.post("/user/login", data=payload)
        if not result:
            log("  ❌ 登录请求失败")
            return False

        code = result.get("code")
        if code == 200:
            log("  ✅ 登录成功！")
            self.sess.save_cookies()
            self.logged_in = True
            return True
        elif code == 10001:
            log("  ⚠️ 需要验证码，请检查是否需要手动过验")
            return False
        else:
            log(f"  ❌ 登录失败: {result.get('message', '未知错误')}")
            return False

    # ---------- CSRF Token ----------

    def fetch_csrf(self) -> bool:
        """获取 CSRF token 并更新请求头"""
        try:
            result = self.sess.get("/user/csrf")
            if result and result.get("code") == 200:
                token = result.get("data", "")
                if token:
                    self.sess.csrf_token = token
                    self.sess.s.headers["X-CSRF-Token"] = token
                    log(f"  🔑 CSRF Token 已获取")
                    return True
            log("  ⚠️ CSRF Token 获取失败或为空")
            return False
        except Exception as e:
            log(f"  ⚠️ CSRF 请求异常: {e}")
            return False

    # ---------- 签到状态 ----------

    def check_sign_status(self) -> bool:
        """检查今日签到状态"""
        log("\n📊 检查签到状态...")
        result = self.sess.get("/user/reward/tasks")
        if not result:
            log("  ❌ 查询失败")
            return False

        tasks = result.get("data", [])
        for task in tasks:
            if task.get("Name") == "每日签到" and task.get("Status") == 2:
                log("  ✅ 今日已签到")
                return True

        log("  📌 今日未签到")
        return False

    # ---------- 积分查询 ----------

    def get_points(self) -> int:
        """获取当前积分"""
        result = self.sess.get("/user/")
        if result and result.get("code") == 200:
            points = result.get("data", {}).get("Points", 0)
            log(f"  💰 当前积分: {points}（约 ¥{points/2000:.2f}）")
            return points
        return 0

    # ---------- 自动提现 ----------

    def withdraw(self) -> Tuple[bool, int]:
        """按 WITHDRAW_POINTS 配置自动提现
        接口: POST /user/reward/withdraw  body: {"points": 60000, "target": "alipay"}
        返回: (是否成功, 实际提现积分数)
        """
        log("\n" + "=" * 50)
        log("💸 检查提现条件")
        log("=" * 50)

        points = self.get_points()

        # 解析提现配置
        cfg = str(WITHDRAW_POINTS).strip().lower()
        if cfg == "all":
            # 提现全部：需达到最低提现门槛
            if points < WITHDRAW_MIN_POINTS:
                log(f"  📌 当前积分 {points} 未达到最低提现门槛 {WITHDRAW_MIN_POINTS}，跳过提现")
                return False, 0
            amount = points
        else:
            try:
                amount = int(cfg)
            except ValueError:
                log(f"  ❌ WITHDRAW_POINTS 配置无效: {WITHDRAW_POINTS}（应为数字或 all）")
                return False, 0
            if amount <= 0:
                log(f"  ❌ WITHDRAW_POINTS 配置无效: {WITHDRAW_POINTS}（必须大于 0）")
                return False, 0
            if points < amount:
                log(f"  📌 当前积分 {points} 未达到提现积分 {amount}，跳过提现")
                return False, 0

        # 提现前刷新 CSRF Token
        if not self.fetch_csrf():
            log("  ⚠️ CSRF Token 获取失败，继续尝试提现...")

        log(f"  💰 当前积分 {points}，申请提现 {amount} 积分到 {WITHDRAW_TARGET}")
        result = self.sess.post(
            "/user/reward/withdraw",
            data={"points": amount, "target": WITHDRAW_TARGET},
            extra_headers={"X-CSRF-Token": self.sess.csrf_token},
        )

        if not result:
            log("  ❌ 提现请求失败")
            return False, 0

        code = result.get("code")
        log(f"  📮 提现响应: {json.dumps(result, ensure_ascii=False)}")

        if code == 200:
            log(f"  ✅ 提现成功！已申请 {amount} 积分提现到 {WITHDRAW_TARGET}")
            # 提现后查询最新积分
            self.get_points()
            return True, amount

        log(f"  ❌ 提现失败: {result.get('message', '未知错误')}")
        return False, 0

    # ---------- 执行签到 ----------

    def sign_in(self, vticket: str = "", vrandstr: str = "") -> bool:
        """执行签到。验证码需在浏览器完成后传入 vticket / vrandstr。"""
        log("\n" + "=" * 50)
        log("📅 执行签到")
        log("=" * 50)

        if not self.fetch_csrf():
            log("  ⚠️ CSRF Token 获取失败，继续尝试...")

        payload = {
            "task_name": "每日签到",
            "verifyCode": "",
            "vticket": vticket,
            "vrandstr": vrandstr,
        }
        result = self.sess.post("/user/reward/tasks", data=payload)

        if not result:
            log("  ❌ 签到请求失败")
            return False

        code = result.get("code")
        msg = result.get("message", "")
        log(f"  📮 签到响应: code={code}, msg={msg}")

        if code == 200:
            log("  ✅ 签到成功！")
            return True

        if code in (10004, 40003) or "验证" in str(msg):
            log("  ❌ 需要先在浏览器完成验证码，并把 vticket / vrandstr 传给脚本")
        else:
            log(f"  ⚠️ 签到返回: {json.dumps(result, ensure_ascii=False)}")
        return False

    # ---------- 自动过验证码并签到 ----------

    def sign_in_with_captcha(self) -> bool:
        """自动求解验证码并完成签到（优先通过 API，回退到本地求解）"""
        log("\n" + "=" * 50)
        log("📅 执行签到（自动过验证码）")
        log("=" * 50)

        # 获取 CSRF Token
        if not self.fetch_csrf():
            log("  ⚠️ CSRF Token 获取失败，继续尝试...")

        ticket = ""
        randstr = ""

        # 优先通过远程 API 求解验证码
        if CAPTCHA_API_URL:
            if not OCR_TOKEN:
                log("  ❌ 未配置验证码识别 Token，请在青龙环境变量中添加 OCR_TOKEN")
                return False
            log(f"  🌐 通过 API 求解验证码: {CAPTCHA_API_URL}")
            try:
                # 提取当前 session 的 cookies 传给求解服务
                cookies_dict = {c.name: c.value for c in self.sess.s.cookies}
                # Token 同时放到 query / header / body，兼容不同服务端读取方式
                api_data = requests.post(
                    f"{CAPTCHA_API_URL}/solve",
                    params={"token": OCR_TOKEN},
                    headers={"Authorization": f"Bearer {OCR_TOKEN}"},
                    json={
                        "token": OCR_TOKEN,
                        "api_token": OCR_TOKEN,
                        "cookies": cookies_dict,
                        "aid": CAPTCHA_AID,
                        "method": os.environ.get("CAPTCHA_METHOD", "auto"),
                    },
                    timeout=180,
                ).json()
                log(f"  🔍 API 原始响应: {api_data}")
                if api_data.get("success"):
                    ticket = api_data.get("ticket", "")
                    randstr = api_data.get("randstr", "")
                    log(f"  ✅ API 验证码求解成功  ticket={ticket[:20]}...  randstr={randstr}")
                else:
                    log(f"  ❌ API 验证码求解失败: {api_data.get('error', '')}")
                    return False
            except Exception as e:
                log(f"  ❌ API 调用异常: {e}")
                return False
        else:
            # 本地求解：优先使用 OCR 图形库，失败自动降级到 SIFT
            solver = OCRExpertSolver(self.sess)
            log("  🔍 开始 OCR 图形库求解验证码...")
            captcha_result = solver.solve()
            if not captcha_result:
                log("  ❌ 验证码求解失败")
                return False
            ticket = solver.ticket
            randstr = solver.randstr
            clicks = captcha_result.get("clicks", "")
            log(f"  ✅ 验证码求解成功  ticket={ticket[:20]}...  randstr={randstr[:20]}...")
            log(f"  📍 点击序列: {clicks}")

        # 提交签到（带验证码凭证）
        log(f"  🔑 签到凭证: ticket={ticket[:40]}...  randstr={randstr}")
        payload = {
            "task_name": "每日签到",
            "verifyCode": "",
            "vticket": ticket,
            "vrandstr": randstr,
        }
        # 验证码求解后等待几秒，避免行为异常被风控
        time.sleep(random.uniform(5, 8))
        # 签到前先检查 session 是否有效
        try:
            check = self.sess.s.get(API_BASE + "/user/", timeout=10, verify=False)
            check_json = check.json()
            if check_json.get("code") != 200:
                log(f"  ⚠️ Session 已失效，尝试重新登录...")
                self.login()
                if not self.fetch_csrf():
                    log("  ❌ 重新登录失败")
                    return False
        except Exception as e:
            log(f"  ⚠️ Session 检查异常: {e}")
        # 使用直接 POST 请求，JSON 格式（与 rainyun API 期望一致）
        try:
            resp = self.sess.s.post(
                API_BASE + "/user/reward/tasks",
                json=payload,
                headers={
                    **self.sess.s.headers,
                },
                timeout=30,
                verify=False,
            )
            log(f"  🔍 签到响应状态: {resp.status_code}  headers={dict(resp.headers)}")
            result = resp.json()
            log(f"  🔍 签到响应内容: {result}")
        except Exception as e:
            log(f"  ❌ 签到请求异常: {e}")
            return False

        if not result:
            log("  ❌ 签到请求失败")
            return False

        code = result.get("code")
        msg = result.get("message", "")
        log(f"  📮 签到响应: code={code}, msg={msg}")

        if code == 200:
            log("  ✅ 签到成功！")
            return True

        log(f"  ❌ 签到失败: {msg}")
        return False

    # ---------- 入口 ----------

    def do_checkin(self) -> Tuple[bool, str]:
        """直接签到（不带验证码），参考 V1 逻辑。返回 (是否成功, 消息)"""
        if not self.fetch_csrf():
            log("  ⚠️ CSRF Token 获取失败，继续尝试...")

        payload = {
            "task_name": "每日签到",
            "verifyCode": "",
            "vticket": "",
            "vrandstr": "",
        }
        # 直接用 session 发请求（不走 raise_for_status，保留 400 响应体）
        try:
            resp = self.sess.s.post(
                API_BASE + "/user/reward/tasks",
                json=payload,
                timeout=20,
            )
            result = resp.json()
        except Exception as e:
            log(f"  ❌ 签到请求异常: {e}")
            return False, "签到请求失败"

        code = result.get("code")
        msg = result.get("message", "")
        log(f"  📮 签到响应: code={code}, msg={msg}")

        if code == 200:
            return True, "签到成功"
        elif code == 10004:
            return False, "需要验证码"
        elif code == 30002:
            return False, "登录已失效"
        else:
            return False, msg or str(result)

    def run(self) -> Dict:
        """完整入口：先执行签到流程，登录成功后再检查是否满足自动提现条件"""
        result = self._run_sign_flow()

        # 登录成功后检查提现（提现与签到状态无关，仅看总积分）
        if result["login_ok"]:
            ok, amount = self.withdraw()
            result["withdraw_ok"] = ok
            result["withdraw_amount"] = amount
            if ok:
                result["points"] = self.get_points()

        return result

    def _run_sign_flow(self) -> Dict:
        """执行完整签到流程：先直接签到，需要验证码时再自动求解"""
        result = {
            "username": self.username,
            "login_ok": False,
            "sign_ok":  False,
            "points":   0,
            "withdraw_ok": False,
            "error":    "",
        }

        # 1. 登录
        if not self.login():
            result["error"] = "登录失败"
            return result
        result["login_ok"] = True

        time.sleep(random.uniform(1, 3))

        # 2. 签到前积分
        points_before = self.get_points()

        # 3. 检查是否已签到
        if self.check_sign_status():
            result["sign_ok"] = True
            result["points"] = points_before
            log("\n✅ 今日已签到，无需重复操作")
            return result

        # 4. 先直接尝试签到（不带验证码）
        log("\n" + "=" * 50)
        log("📅 执行签到")
        log("=" * 50)

        time.sleep(random.uniform(1, 3))
        ok, msg = self.do_checkin()

        if ok:
            log("  ✅ 签到成功！")
            result["sign_ok"] = True
            result["points"] = self.get_points()
            log("\n" + "=" * 50)
            log("✅ 签到流程完成！")
            log("=" * 50)
            return result

        # 5. 需要验证码时，走自动验证码求解流程
        if "验证码" in msg or "captcha" in msg.lower():
            log("  🔄 需要验证码，启动自动求解...")
            if self.sign_in_with_captcha():
                result["sign_ok"] = True
                result["points"] = self.get_points()
                log("\n" + "=" * 50)
                log("✅ 签到流程完成！")
                log("=" * 50)
                return result
            else:
                result["error"] = "验证码求解失败，签到未完成"
                result["points"] = self.get_points()
                return result

        # 6. 其他错误
        result["error"] = f"签到失败: {msg}"
        result["points"] = self.get_points()
        return result


# ============================================================
# 入口
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="雨云自动签到脚本（支持多账号）")
    parser.add_argument("-u", "--username", help="登录账号（单账号模式）")
    parser.add_argument("-p", "--password", help="登录密码（单账号模式）")
    parser.add_argument("--config", help="配置文件路径（JSON）")
    parser.add_argument("--status", action="store_true", help="仅查询签到状态")
    parser.add_argument("--cookies", action="store_true", help="仅测试 cookies")
    args = parser.parse_args()

    ensure_dirs()

    # 确定账号列表: [(备注名, 用户名, 密码), ...]
    account_list = []
    if args.username and args.password:
        # 命令行指定单账号
        account_list = [(args.username, args.username, args.password)]
    elif os.environ.get("RAINYUN"):
        # 推荐格式：RAINYUN=账号#密码@账号#密码（单账户不需要 @）
        try:
            account_list = parse_rainyun_accounts(os.environ["RAINYUN"])
        except ValueError as e:
            print(f"环境变量配置错误：{e}")
            sys.exit(1)
    elif args.config or os.environ.get("RAINYUN_CONFIG"):
        # 配置文件
        config_path = args.config or os.environ.get("RAINYUN_CONFIG")
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "accounts" in cfg:
                for a in cfg["accounts"]:
                    account_list.append((a.get("name", a["username"]), a["username"], a["password"]))
            elif "username" in cfg:
                account_list.append((cfg.get("name", cfg["username"]), cfg["username"], cfg["password"]))
    elif os.environ.get("RAINYUN_USER") and os.environ.get("RAINYUN_PASS"):
        # 环境变量
        account_list = [(os.environ["RAINYUN_USER"], os.environ["RAINYUN_USER"], os.environ["RAINYUN_PASS"])]
    else:
        # 使用脚本内置 ACCOUNTS
        account_list = list(ACCOUNTS)

    # 示例模板不能作为真实账号执行，否则会产生误导性的 API 请求和结果。
    account_list = [
        item for item in account_list
        if not (
            str(item[1]).startswith("用户名")
            or "密码" in str(item[2])
            or str(item[1]).strip().lower() in {"username", "your_username"}
            or str(item[2]).strip().lower() in {"password", "your_password"}
        )
    ]

    if not account_list:
        print("未配置有效账号！请设置 RAINYUN=账号#密码（多账户用 @ 分隔），或使用兼容配置 RAINYUN_USER/RAINYUN_PASS")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"雨云签到 - 共 {len(account_list)} 个账号")
    print(f"{'='*50}")

    all_results = []
    status_failed = False
    cookies_failed = False
    for idx, (name, username, password) in enumerate(account_list, 1):
        print(f"\n{'─'*40}")
        print(f"[{idx}/{len(account_list)}] {name} ({username})")
        print(f"{'─'*40}")

        if idx > 1:
            delay = random.uniform(15, 25)
            print(f"账号间延迟 {delay:.1f}s...")
            time.sleep(delay)

        signer = RainyunSigner(username, password)

        if args.status:
            if not signer.login():
                status_failed = True
                continue
            signer.check_sign_status()
            signer.get_points()
        elif args.cookies:
            if signer.sess.load_cookies():
                info = signer.sess.get("/user/")
                if info and info.get("code") == 200:
                    print("✅ Cookies 有效")
                    signer.get_points()
                else:
                    print("❌ Cookies 已失效，请重新登录")
                    cookies_failed = True
            else:
                print("❌ 未找到 Cookies 文件，请先运行签到")
                cookies_failed = True
        else:
            result = signer.run()
            result["name"] = name
            all_results.append(result)
            print(f"\n  {name} ({result['username']})")
            print(f"  登录: {'✅' if result['login_ok'] else '❌'}")
            print(f"  签到: {'✅' if result['sign_ok']  else '❌'}")
            print(f"  积分: {result['points']}")
            if result.get('withdraw_ok'):
                print(f"  提现: ✅ 已申请 {result.get('withdraw_amount', 0)} 积分提现到 {WITHDRAW_TARGET}")
            if result['error']:
                print(f"  错误: {result['error']}")

    # 汇总
    if all_results:
        print(f"\n{'='*50}")
        print("签到汇总")
        print(f"{'='*50}")
        for r in all_results:
            status = "✅" if r["sign_ok"] else "❌"
            withdraw_flag = "💸已提现" if r.get("withdraw_ok") else ""
            print(f"  {status} {r['name']}  积分:{r['points']} {withdraw_flag}  {r.get('error','')}")
        print(f"{'='*50}")

        notification_lines = []
        for r in all_results:
            login_text = "成功" if r["login_ok"] else "失败"
            sign_text = "成功/已签" if r["sign_ok"] else "失败"
            line = f"{r['name']}｜登录：{login_text}｜签到：{sign_text}｜积分：{r['points']}"
            if r.get("withdraw_ok"):
                line += f"｜提现：{r.get('withdraw_amount', 0)} 积分"
            elif r.get("error"):
                line += f"｜错误：{r['error']}"
            notification_lines.append(line)
        success_count = sum(1 for r in all_results if r["login_ok"] and r["sign_ok"])
        notification_lines.append(f"汇总：成功 {success_count} / 总计 {len(all_results)}")
        send_qinglong_notification("雨云自动签到", "\n".join(notification_lines))

    if status_failed or cookies_failed or any(not r["login_ok"] or not r["sign_ok"] for r in all_results):
        sys.exit(1)


if __name__ == "__main__":
    main()
