# name: 中通快递
# cron: 1 8,13 * * *

"""
name:  中通快递
入口: 微信小程序
功能: 签到
变量: YYB_SERVER (YYB-Go服务地址@微信账号标识，多账号用换行分割)
    PROXY_API_URL (代理api，返回一条txt文本，内容为代理ip:端口)
定时: 一天两次
cron: 1 8,13 * * *
------------更新日志------------
# name: 中通快递
2025/6/25   V1.0    初始化脚本
2025/7/7    V1.1    适配更多协议
2025/7/21   V1.2    适配更多协议
2025/7/22   V1.3    修改协议适配器导入方式
2025/7/28   V1.4    修改头部注释，以便拉库
2026/8/17   V2.0    移除 getCode 依赖，改用 YYB-Go-Enhanced 获取微信 code，更新登录协议
2026/9/23   V2.1    增加签到前后积分查询，并清晰显示积分变化
2026/9/23   V2.2    美化多账号通知，增加账号结果、积分变化和执行汇总
"""

import random
import time
import requests
import os
import sys
import logging
import traceback
from datetime import datetime

MULTI_ACCOUNT_PROXY = False # 是否使用多账号代理，默认不使用，True则使用多账号代理
NOTIFY = os.getenv("LY_NOTIFY") or False # 是否推送日志，默认不推送，True则推送

class AutoTask:
    def __init__(self, site_name):
        """
        初始化自动任务类
        :param site_name: 站点名称，用于日志显示
        """
        self.site_name = site_name
        self.proxy_url = os.getenv("PROXY_API_URL") # 代理api，返回一条txt文本，内容为代理ip:端口
        self.wx_appid = "wx7ddec43d9d27276a" # 微信小程序id
        self.log_msgs = []
        self.account_results = []
        self.host = "hdgateway.zto.com"
        self.user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.75(0x18004b21) NetType/WIFI Language/zh_CN"

    def log(self, msg, level="info"):
        formatted = f"[{level.upper()}] {msg}"
        print(formatted)
        self.log_msgs.append(formatted)

    def get_wx_code(self, server, ref):
        try:
            host = server.rstrip("/")
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
            response = requests.post(
                f"{host}/wxapp/getCode",
                json={"ref": ref, "app_id": self.wx_appid},
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            code = ((body.get("data") or {}).get("result") or {}).get("code")
            if body.get("code") != 0 or not code:
                self.log(f"[获取 code]失败，YYB-Go 响应码: {body.get('code')}", level="error")
                return None
            self.log("[获取 code]成功")
            return code
        except Exception as e:
            self.log(f"获取 code 失败: {e}", level="error")
            return None

    def get_proxy(self):
        """
        获取代理
        :return: 代理
        """
        if not self.proxy_url:
            self.log("[获取代理]没有找到环境变量PROXY_API_URL，不使用代理", level="warning")
            return None
        url = self.proxy_url
        response = requests.get(url)
        proxy = response.text
        self.log(f"[获取代理]: {proxy}")
        return proxy

    def check_proxy(self, proxy, session):
        """
        检查代理
        :param proxy: 代理
        :param session: session
        :return: 是否可用
        """
        try:
            url = f"http://{self.host}/getApolloConfig"
            session.headers["X-Token"] = ""
            payload = {"keys":["serverTime"]}
            response = session.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                self.log(f"[检查代理]: {proxy} 应该可用")
                return True
            else:
                self.log(f"[检查代理]: {response.text}")
                return False
        except Exception as e:
            return False


    def check_env(self):
        """
        检查环境变量
        :return: 环境变量字符串
        """
        try:
            yyb_server = os.getenv("YYB_SERVER", "")
            if not yyb_server.strip():
                self.log("[检查环境变量]没有找到 YYB_SERVER，请按 地址@微信账号标识 配置", level="error")
                return

            for line_no, raw in enumerate(yyb_server.splitlines(), 1):
                raw = raw.strip()
                if not raw:
                    continue
                if "@" not in raw:
                    self.log(f"[检查环境变量]YYB_SERVER 第{line_no}行格式错误，已跳过", level="error")
                    continue
                server, ref = raw.rsplit("@", 1)
                if not server.strip() or not ref.strip():
                    self.log(f"[检查环境变量]YYB_SERVER 第{line_no}行地址或账号标识为空，已跳过", level="error")
                    continue
                yield server.strip(), ref.strip()
        except Exception as e:
            self.log(f"[检查环境变量]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            raise

    def wxlogin(self, session, code):
        """
        登录
        :param session: session
        :param code: 微信code
        :return: 登录结果
        """
        try:
            url = f"https://{self.host}/auth_wechatMini_authByCode"
            payload = {
                "code": code
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log(f"[登录]: {response_json['message']}")
                token = (response_json.get('result') or {}).get('token')
                if not token:
                    self.log("[登录]响应缺少 token", level="error")
                    return False
                session.headers["X-Token"] = token
                return True
            else:
                self.log(f"[登录]发生错误: {response_json['message']}", level="error")
                return False
        except requests.RequestException as e:
            self.log(f"[登录]发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"[登录]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False


    def sign_in(self, session):
        """
        签到
        :param session: session
        :return: 签到结果
        """
        try:
            url = f"https://membergateway.zto.com/member/activity/signIn"
            payload = {
                "signType": "TODAY_SIGN",
                "signDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "supplementaryScene": "null"
            }
            response = session.post(url, json=payload, timeout=20)
            response.raise_for_status()
            response_json = response.json()
            if response_json['status'] == True:
                self.log("[签到]: 成功")
                return True, "签到成功"
            else:
                message = response_json.get("message") or "签到失败"
                self.log(f"[签到]: {message}", level="warning")
                return False, message
        except Exception as e:
            self.log(f"[签到]发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False, "请求异常"

    def get_points(self, session):
        """查询当前会员积分，失败时返回 None。"""
        try:
            response = session.post(
                "https://membergateway.zto.com/member/getMemberPoints",
                json={},
                timeout=20,
            )
            response.raise_for_status()
            response_json = response.json()
            points = (response_json.get("data") or {}).get("totalPoint")
            if response_json.get("success") is True and isinstance(points, (int, float)):
                return int(points)
            self.log("[积分]查询失败：响应中没有有效积分", level="warning")
        except Exception as e:
            self.log(f"[积分]查询失败: {e}", level="warning")
        return None

    def log_points_change(self, before, after):
        """用一条清晰日志显示签到前后的积分。"""
        if before is None or after is None:
            before_text = "查询失败" if before is None else str(before)
            after_text = "查询失败" if after is None else str(after)
            self.log(f"[积分]: 初始积分 {before_text} → 完成后积分 {after_text}", level="warning")
            return
        self.log(f"[积分]: 初始积分 {before} → 完成后积分 {after}（变化 {after - before:+d}）")

    def build_notification(self, elapsed_seconds):
        """生成适合青龙通知展示的账号明细和执行汇总。"""
        normal_count = sum(1 for item in self.account_results if item["normal"])
        abnormal_count = len(self.account_results) - normal_count
        total_delta = sum(
            item["final_points"] - item["initial_points"]
            for item in self.account_results
            if item["initial_points"] is not None and item["final_points"] is not None
        )
        title = (
            f"📦 {self.site_name}签到｜正常 {normal_count}｜"
            f"异常 {abnormal_count}｜积分 {total_delta:+d}"
        )
        sections = [f"📦 {self.site_name}签到", ""]
        for item in self.account_results:
            sections.extend(["━━━━━━━━━━━━━━", f"👤 账号 {item['index']}"])
            login_icon = "✅" if item["login"] == "认证成功" else "❌"
            sections.append(f"{login_icon} 登录：{item['login']}")
            if item["sign"] == "签到成功":
                sign_icon = "✅"
            elif item["sign"] == "今日已签到":
                sign_icon = "ℹ️"
            else:
                sign_icon = "❌"
            sections.append(f"{sign_icon} 签到：{item['sign']}")
            before = item["initial_points"]
            after = item["final_points"]
            if before is None or after is None:
                sections.append("⚠️ 积分：查询失败")
            else:
                sections.append(f"💰 积分：{before} → {after}（{after - before:+d}）")
            sections.append("")
        sections.extend([
            "━━━━━━━━━━━━━━",
            "📊 执行汇总",
            f"账号总数：{len(self.account_results)}",
            f"✅ 正常：{normal_count}",
            f"⚠️ 异常：{abnormal_count}",
            f"💰 本次增加：{total_delta} 积分",
            f"⏱️ 执行耗时：{elapsed_seconds} 秒",
        ])
        return title, "\n".join(sections)

    def run(self):
        """
        运行任务
        """
        started_at = time.monotonic()
        notify = None
        try:
            # 如果notify模块不存在，从远程下载至本地
            if not os.path.exists("notify.py"):
                url = "https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py"
                response = requests.get(url)
                with open("notify.py", "w", encoding="utf-8") as f:
                    f.write(response.text)
                import notify
            else:
                import notify

            self.log(f"【{self.site_name}】开始执行任务")

            # 检查环境变量
            for index, (server, ref) in enumerate(self.check_env(), 1):
                self.log("")
                self.log(f"------ 【账号{index}】开始执行任务 ------")

                if MULTI_ACCOUNT_PROXY:
                    proxy = self.get_proxy()
                    if proxy:
                        session = requests.Session()
                        session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                        # 检查代理，不可用重新获取
                        while not self.check_proxy(proxy, session):
                            proxy = self.get_proxy()
                            session.proxies.update({"http": f"http://{proxy}", "https": f"http://{proxy}"})
                    else:
                        session = requests.Session()
                else:
                    session = requests.Session()

                session.headers.update({
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                    "Referer": f"https://servicewechat.com/{self.wx_appid}/693/page-frame.html",
                    "x-sv-v": "0.22.0",
                    "x-version": "V8.160.1",
                    "x-clientCode": "wechatMiniZtoHelper",
                })

                # 执行微信授权
                code = self.get_wx_code(server, ref)
                if not code:
                    self.account_results.append({
                        "index": index,
                        "login": "获取微信 code 失败",
                        "sign": "未执行",
                        "initial_points": None,
                        "final_points": None,
                        "normal": False,
                    })
                    self.log(f"------ 【账号{index}】执行任务完成 ------")
                    continue

                login_result = self.wxlogin(session, code)
                time.sleep(random.randint(1, 3))
                if not login_result:
                    self.account_results.append({
                        "index": index,
                        "login": "认证失败",
                        "sign": "未执行",
                        "initial_points": None,
                        "final_points": None,
                        "normal": False,
                    })
                    self.log(f"------ 【账号{index}】执行任务完成 ------")
                    continue

                initial_points = self.get_points(session)
                sign_success, sign_message = self.sign_in(session)
                time.sleep(random.randint(1, 3))
                final_points = self.get_points(session)
                self.log_points_change(initial_points, final_points)
                self.account_results.append({
                    "index": index,
                    "login": "认证成功",
                    "sign": sign_message,
                    "initial_points": initial_points,
                    "final_points": final_points,
                    "normal": sign_success or sign_message == "今日已签到",
                })

                self.log(f"------ 【账号{index}】执行任务完成 ------")
        except Exception as e:
            self.log(f"【{self.site_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
        finally:
            elapsed_seconds = round(time.monotonic() - started_at)
            title, content = self.build_notification(elapsed_seconds)
            print(f"\n{content}")
            if notify is None:
                self.log("[通知]通知模块加载失败，未发送通知", level="warning")
            else:
                try:
                    notify.send(title, content)
                except Exception as e:
                    self.log(f"[通知]发送失败: {e}", level="warning")

if __name__ == "__main__":
    auto_task = AutoTask("中通快递")
    auto_task.run()
