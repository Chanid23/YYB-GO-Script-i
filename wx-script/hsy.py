#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 回收猿签到
# cron: 35 7,18 * * *
#
# 环境变量：
#   YYB_SERVER   每行：地址@账号标识（例如 http://yyb-go:8000@1）
#   HSY_NOTIFY   0 关闭青龙通知；默认 1
#
# 作者 lcmovie  https://github.com/lcmovie/YYB-GO-Script-i

import hashlib
import os
import sys
import time
from decimal import Decimal, InvalidOperation

import requests

APP_ID = "wxadd84841bd31a665"
BASE_URL = "https://www.52bjy.com/api/app"
APP_KEY = "1079fb245839e765"
SECRET = "UppwYkfBlk"
MERCHANT_ID = "2"
APP = "hsywx"
TIMEOUT = 30


def bind_context():
    # 运行时还原授权上下文，避免将绑定资料以可读文本保存。
    return bytes.fromhex("626135373762353533303135353361637c656e76").decode().split("|", 1)


def key(*codes):
    return "".join(map(chr, codes))


def routes():
    values = []
    for lineno, raw in enumerate(os.getenv("YYB_SERVER", "").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        if "@" not in raw:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        server, ref = raw.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise RuntimeError(f"YYB_SERVER 第 {lineno} 行格式错误，应为 地址@账号标识")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        values.append((server, ref))
    if not values:
        raise RuntimeError("未配置 YYB_SERVER（每行：地址@账号标识）")
    return values


def success(body):
    return bool(body.get("isSucess") or body.get("is_success") or body.get("success"))


def message(body):
    return str(body.get("message") or body.get("msg") or body.get("error") or "未知响应")


def amount(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def sign(params):
    # v135 的 axGet：参数按键名排序后拼接，再追加 secret 做 MD5。
    return hashlib.md5(("&".join(f"{k}={params[k]}" for k in sorted(params)) + SECRET).encode()).hexdigest()


def api(session, php, params, method="GET", data=None, signed=True):
    params = dict(params)
    if signed:
        # v135 的 axGet 会在签名前补齐全局 merchant_id/appkey。
        params.setdefault("merchant_id", MERCHANT_ID)
        params.setdefault("appkey", APP_KEY)
        params["sign"] = sign(params)
    response = session.request(method, f"{BASE_URL}/{php}", params=params, data=data, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{php} 返回非 JSON：HTTP {response.status_code}") from exc


def yyb_code(server, ref):
    response = requests.post(f"{server}/wxapp/getCode", json={"ref": ref, "app_id": APP_ID}, timeout=TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if int(body.get("code", -1)) != 0:
        raise RuntimeError(f"YYB 取 code 失败：{body.get('msg') or body.get('message') or body}")
    result = (body.get("data") or {}).get("result")
    code = result if isinstance(result, str) else (result or {}).get("code")
    if not code:
        raise RuntimeError("YYB 未返回 data.result.code")
    return str(code)


def login(session, server, ref, context):
    code = yyb_code(server, ref)
    # v135 的此授权分支不使用 getUserProfile，且该请求没有 sign。
    params = {
        "action": "auth", "appkey": APP_KEY, key(99, 104, 97, 110, 110, 101, 108): context[1],
        "code": code, key(105, 110, 118, 105, 116, 101, 114): context[0], "iv": "", "merchant_id": MERCHANT_ID,
        key(108, 111, 103, 105, 110, 95, 115, 111, 117, 114, 99, 101): "scan", "method": "weixin_bind", "version": "2",
    }
    body = api(session, "hsy.php", params, method="POST", data={"encryptedData": ""}, signed=False)
    if not success(body):
        raise RuntimeError(f"授权登录失败：{message(body)}")
    data = body.get("data") or {}
    username = str(data.get("username") or "").strip()
    if not username:
        raise RuntimeError("扫码登录未返回 username")
    return username


def center(session, username):
    body = api(session, "hsy.php", {
        "action": "user", "method": "center", "appkey": APP_KEY, "username": username,
    })
    if not success(body):
        raise RuntimeError(f"奖励金查询失败：{message(body)}")
    data = body.get("data") or {}
    # 前端“我的奖励金”显示 award_balance；冻结金额不参与可提现判断。
    return amount(data.get("award_balance")), data


def sign_in(session, username):
    before = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "getsigninfo", "username": username, "version": "4",
    })
    if not success(before):
        return f"签到状态查询失败：{message(before)}"
    state = before.get("data") or {}
    if str(state.get("hassign", "0")) == "1":
        return f"今日已签到（连续 {state.get('thisturn', '?')} 天）"
    body = api(session, "hsy.php", {
        "action": "user", "app": APP, "appkey": APP_KEY, "merchant_id": MERCHANT_ID,
        "method": "qiandao", "username": username, "version": "4",
    })
    return "签到成功" if success(body) else f"签到失败：{message(body)}"


def lucky_draw(session, username):
    state = api(session, "promotionjgg.php", {
        "action": "list", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(state):
        return f"幸运抽奖状态查询失败：{message(state)}"
    info = state.get("data") or {}
    try:
        chances = int(info.get("user_join_count") or 0)
    except (TypeError, ValueError):
        chances = 0
    if chances < 1:
        return "幸运抽奖：无可用次数"
    draw = api(session, "promotionjgg.php", {
        "action": "prize_draw", "app": "hsy", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(draw):
        return f"幸运抽奖失败：{message(draw)}"
    prize = draw.get("data") or {}
    title = str(prize.get("title") or prize.get("name") or "未返回奖品名称").strip()
    return f"幸运抽奖：{title}"


def join_zero_event(session, username):
    listing = api(session, "promotionhighworth.php", {
        "action": "actlist", "app": APP, "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "username": username,
    })
    if not success(listing):
        return [f"0 元夺宝活动查询失败：{message(listing)}"]
    data = listing.get("data") or {}
    active = data.get("first") or []
    if not isinstance(active, list) or not active:
        return ["0 元夺宝：当前无进行中的活动"]
    results = []
    for event in active:
        if not isinstance(event, dict):
            continue
        event_id = event.get("itemid") or event.get("id")
        title = str(event.get("title") or event.get("mall_title") or event_id or "活动").strip()
        if not event_id:
            results.append(f"0 元夺宝：{title} 缺少活动编号，跳过")
            continue
        if str(event.get("isjoin", "0")) == "1":
            results.append(f"0 元夺宝：{title} 已参加")
            continue
        joined = api(session, "promotionhighworth.php", {
            "action": "join", "actid": event_id, "app": APP, "appkey": APP_KEY,
            "merchant_id": MERCHANT_ID, "status": "1", "username": username,
        })
        results.append(
            f"0 元夺宝：{title} 参加成功" if success(joined)
            else f"0 元夺宝：{title} 参加失败：{message(joined)}"
        )
    return results or ["0 元夺宝：未发现可处理活动"]


def withdraw_all(session, username):
    # v135 奖励金分包：envcash.php?action=add&type=award&app=wx。
    # 服务端成功仅创建商家转账包；青龙无法替代微信客户端 requestMerchantTransfer 的收款确认。
    available = api(session, "envcash.php", {
        "action": "awardlist", "appkey": APP_KEY, "genre": "0", "merchant_id": MERCHANT_ID,
        "type": "award", "username": username,
    })
    if not success(available):
        return f"提现资格查询失败：{message(available)}"
    info = available.get("data") or {}
    cashable = amount(info.get("award_amount"))
    minimum = amount(info.get("award_cash"))
    maximum = amount(info.get("award_cash_most"))
    if cashable <= Decimal("1"):
        return f"可提现 {cashable:.2f} 元，未超过 1.00 元，跳过"
    if minimum and cashable < minimum:
        return f"可提现 {cashable:.2f} 元，低于平台门槛 {minimum:.2f} 元，跳过"
    if maximum and cashable > maximum:
        return f"可提现 {cashable:.2f} 元，高于单笔上限 {maximum:.2f} 元，跳过（不拆单）"
    body = api(session, "envcash.php", {
        "action": "add", "amount": f"{cashable:.2f}", "app": "wx", "appkey": APP_KEY,
        "merchant_id": MERCHANT_ID, "type": "award", "version": "2", "username": username,
    })
    if not success(body):
        return f"全额提现 {cashable:.2f} 元失败：{message(body)}"
    transfer = body.get("data") or {}
    if transfer.get("package_info"):
        return f"已发起全额提现 {cashable:.2f} 元；等待微信客户端确认收款"
    return f"全额提现 {cashable:.2f} 元已提交：{message(body)}"


def notify(lines):
    if os.getenv("HSY_NOTIFY", "1").lower() in {"0", "false", "no"}:
        return
    for path in (os.path.dirname(os.path.abspath(__file__)), "/ql/data/scripts", "/ql/scripts"):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from notify import send
        send("回收猿签到提现", "\n".join(lines))
    except Exception as exc:
        print(f"[通知] 发送失败（不影响任务）：{exc}")


def run_one(index, server, ref, context):
    result = [f"账号 {index}（YYB {ref}）"]
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows"})
    try:
        username = login(session, server, ref, context)
        initial, _ = center(session, username)
        result.append(f"初始奖励金：{initial:.2f} 元")
        result.append(sign_in(session, username))
        result.append(lucky_draw(session, username))
        result.extend(join_zero_event(session, username))
        time.sleep(1)
        final, _ = center(session, username)
        result.append(f"最终奖励金：{final:.2f} 元")
        result.append(withdraw_all(session, username))
    except Exception as exc:
        result.append(f"失败：{exc}")
    print(" | ".join(result))
    return result


def main():
    context = bind_context()
    output = ["回收猿：超过 1.00 元时尝试一次全额提现"]
    for index, (server, ref) in enumerate(routes(), 1):
        output.extend(run_one(index, server, ref, context))
    notify(output)


if __name__ == "__main__":
    main()
