#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 多账号自动登录脚本 (Matrix 分身版 + 剩余时间显示)
"""

import asyncio
import random
import re
import datetime
from datetime import timezone, timedelta
import os
import requests
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# =====================================================================
#                          配置区域
# =====================================================================

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = 15000
PAGE_LOAD_DELAY = 3

PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame/game/"

DEFAULT_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
DEFAULT_TG_CHATID = os.getenv("TELEGRAM_CHAT_ID") or ""

SCREENSHOT_DIR = "screenshots"
if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

# =====================================================================
#                        账号解析模块
# =====================================================================

def parse_accounts():
    """
    解析环境变量 XSERVER_BATCH
    """
    accounts = []
    raw_data = os.getenv("XSERVER_BATCH")
    
    if not raw_data:
        # 兼容旧单账号
        sid = os.getenv("XSERVER_LOGIN_ID")
        spw = os.getenv("XSERVER_PASSWORD")
        sip = os.getenv("XSERVER_IP")
        if sid and spw and sip:
            accounts.append({
                "id": sid, "pass": spw, "ip": sip,
                "tg_token": DEFAULT_TG_TOKEN, "tg_chat": DEFAULT_TG_CHATID
            })
        return accounts

    # 批量解析
    for line in raw_data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        parts = [p.strip() for p in line.replace("，", ",").split(",")]
        
        if len(parts) >= 3:
            acc = {
                "id": parts[0],
                "pass": parts[1],
                "ip": parts[2],
                "tg_token": parts[3] if len(parts) >= 5 else DEFAULT_TG_TOKEN,
                "tg_chat": parts[4] if len(parts) >= 5 else DEFAULT_TG_CHATID
            }
            accounts.append(acc)
            
    return accounts

# =====================================================================
#                        Telegram 通知类
# =====================================================================

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def calculate_remaining(self, expiry_date_str):
        """
        计算剩余时间
        输入格式: YYYY-MM-DD
        返回: "X天 Y小时"
        """
        if not expiry_date_str:
            return "未知"
            
        try:
            # XServer 的到期时间通常是当天的 23:59:59 或者 00:00:00
            # 这里假设是日本时间 (JST, UTC+9) 的当天结束
            # 为了简化，我们按北京时间对比
            
            # 解析日期字符串
            expiry_date = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
            
            # 获取当前日期 (UTC+9 日本时间，因为服务器在日本)
            jst_now = datetime.datetime.now(timezone(timedelta(hours=9)))
            today = jst_now.date()
            
            delta = expiry_date - today
            days = delta.days
            
            # 如果是当天到期
            if days < 0:
                return "已过期"
            elif days == 0:
                return "今天到期 (紧急)"
            else:
                return f"{days} 天"
                
        except Exception as e:
            print(f"⚠️ 日期计算错误: {e}")
            return "计算错误"

    def send_result(self, login_id, ip, status, old_time, new_time):
        if not self.enabled: return
        
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        safe_id = login_id[:2] + "***" + login_id[-2:] if len(login_id) > 4 else login_id

        # 计算剩余天数 (基于 old_time)
        remaining_str = self.calculate_remaining(old_time)

        msg = f"<b>🎮 XServer 续期通知</b>\n"
        msg += f"🆔 账号: <code>{safe_id}</code>\n"
        msg += f"🖥 IP: <code>{ip}</code>\n"
        msg += f"⏰ 时间: {timestamp}\n\n"
        
        if status == "Success":
            msg += f"✅ <b>续期成功</b>\n"
            msg += f"📅 旧: {old_time}\n"
            msg += f"📅 新: {new_time}\n"
        elif status == "Unexpired":
            msg += f"ℹ️ <b>无需续期</b>\n"
            msg += f"📅 到期: {old_time}\n"
            msg += f"⏳ 剩余: <b>{remaining_str}</b>\n"
            msg += f"💡 提示: 剩余 > 24小时\n"
        elif status == "Failed":
            msg += f"❌ <b>执行失败</b>\n"
            msg += f"📅 到期: {old_time or '未知'}\n"
        
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# =====================================================================
#                        自动化核心类
# =====================================================================

class XServerBot:
    def __init__(self, account):
        self.account = account
        self.login_id = account["id"]
        self.password = account["pass"]
        self.login_ip = account["ip"]
        self.notifier = TelegramNotifier(account["tg_token"], account["tg_chat"])
        
        self.browser = None
        self.context = None
        self.page = None
        self.old_expiry = None
        self.new_expiry = None
        self.status = "Unknown"
        self.screenshot_idx = 0

    async def start(self):
        p = await async_playwright().start()
        args = ['--no-sandbox', '--disable-blink-features=AutomationControlled']
        if USE_PROXY and PROXY_SERVER: args.append(f'--proxy-server={PROXY_SERVER}')
        
        self.browser = await p.chromium.launch(headless=USE_HEADLESS, args=args)
        self.context = await self.browser.new_context(locale='ja-JP', viewport={'width': 1920, 'height': 1080})
        self.page = await self.context.new_page()
        await stealth_async(self.page)

    async def close(self):
        if self.context: await self.context.close()
        if self.browser: await self.browser.close()

    async def save_shot(self, name):
        try:
            self.screenshot_idx += 1
            path = f"{SCREENSHOT_DIR}/{self.login_id}_{self.screenshot_idx}_{name}.png"
            await self.page.screenshot(path=path)
        except: pass

    async def run_task(self):
        try:
            await self.start()
            print(f"🚀 [{self.login_id}] 启动独立任务...")
            
            await self.page.goto(TARGET_URL, wait_until='load', timeout=60000)
            await self.page.wait_for_selector("input[type='password']", timeout=WAIT_TIMEOUT)
            
            # 填写表单
            inputs = await self.page.locator("input:not([type='hidden']):not([type='submit'])").all()
            if len(inputs) >= 3:
                await inputs[0].fill(self.login_id)
                await inputs[1].fill(self.password)
                await inputs[2].fill(self.login_ip)
            else:
                await self.page.locator("input[type='text']").nth(0).fill(self.login_id)
                await self.page.locator("input[type='password']").fill(self.password)
                await self.page.locator("input[type='text']").nth(1).fill(self.login_ip)

            await self.page.click("input[value='ログインする'], button:has-text('ログインする')")
            await self.page.wait_for_load_state('networkidle')
            
            if "xmgame/game/index" not in self.page.url:
                print(f"❌ [{self.login_id}] 登录失败，URL: {self.page.url}")
                self.status = "Failed"
                await self.save_shot("login_fail")
                return

            print(f"✅ [{self.login_id}] 登录成功")
            await self.check_and_renew()

        except Exception as e:
            print(f"❌ [{self.login_id}] 异常: {e}")
            self.status = "Failed"
        finally:
            self.notifier.send_result(self.login_id, self.login_ip, self.status, self.old_expiry, self.new_expiry)
            await self.close()

    async def check_and_renew(self):
        try:
            elements = await self.page.locator("text=/残り.*時間/").all()
            for el in elements:
                txt = await el.text_content()
                if "残り" in txt:
                    match = re.search(r'\((\d{4}-\d{2}-\d{2})まで\)', txt)
                    if match: self.old_expiry = match.group(1)
                    break
            
            renew_btn = self.page.locator("a:has-text('アップグレード・期限延長')")
            if not await renew_btn.count():
                print(f"⚠️ [{self.login_id}] 未找到续期按钮")
                self.status = "Failed"
                return

            await renew_btn.click()
            await self.page.wait_for_load_state('networkidle')

            if "残り契約時間が24時間を切るまで" in await self.page.content():
                print(f"ℹ️ [{self.login_id}] 未满足续期条件 (>24h)")
                self.status = "Unexpired"
                return

            print(f"🔄 [{self.login_id}] 执行续期...")
            await self.page.click("a:has-text('期限を延長する')")
            await self.page.wait_for_load_state('networkidle')
            await self.page.click("button:has-text('確認画面に進む')")
            await self.page.wait_for_load_state('networkidle')
            
            try:
                self.new_expiry = await self.page.locator("tr:has(th:has-text('延長後の期限')) td").first.text_content()
                self.new_expiry = self.new_expiry.strip()
            except: pass

            await self.page.click("button[type='submit']:has-text('期限を延長する')")
            await self.page.wait_for_load_state('networkidle')

            if "期限を延長しました" in await self.page.content():
                print(f"🎉 [{self.login_id}] 续期成功！")
                self.status = "Success"
                await self.save_shot("renew_success")
            else:
                self.status = "Unknown"

        except Exception as e:
            print(f"❌ [{self.login_id}] 续期出错: {e}")
            self.status = "Failed"

# =====================================================================
#                        主程序入口 (Matrix 修改版)
# =====================================================================

async def main():
    print("=" * 60)
    print("XServer 独立 IP 分身版")
    print("=" * 60)

    accounts = parse_accounts()
    if not accounts:
        print("❌ 未找到账号配置 XSERVER_BATCH")
        exit(1)

    # 👇👇👇 核心逻辑：检查是否指定了运行索引 👇👇👇
    target_index_str = os.getenv("TARGET_INDEX")
    
    if target_index_str is not None:
        try:
            idx = int(target_index_str)
            if 0 <= idx < len(accounts):
                # 🎯 矩阵模式：只运行指定的这一个账号
                print(f"🎯 [Matrix Mode] 本次任务只运行第 {idx + 1} 个账号")
                acc = accounts[idx]
                bot = XServerBot(acc)
                await bot.run_task()
            else:
                print(f"⚠️ 索引 {idx} 超出范围 (总账号数: {len(accounts)})，本任务跳过。")
        except ValueError:
            print("❌ TARGET_INDEX 格式错误")
    else:
        # 🔄 兼容模式：如果没有指定索引，就像以前一样循环跑所有
        print("⚠️ 未指定 TARGET_INDEX，进入循环模式 (IP可能相同)")
        for i, acc in enumerate(accounts):
            bot = XServerBot(acc)
            await bot.run_task()
            if i < len(accounts) - 1:
                delay = random.randint(1, 100)
                print(f"\n⏳ 等待 {delay} 秒...\n")
                await asyncio.sleep(delay)

if __name__ == "__main__":
    asyncio.run(main())
