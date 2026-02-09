#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 多账号自动登录和续期脚本
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

# 浏览器配置
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = 15000     # 超时时间
PAGE_LOAD_DELAY = 3      # 页面加载延迟

# 代理配置
PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

# 目标地址 (XServer Game Panel 独立登录页)
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame/game/"

# 全局默认 TG 配置 (如果单行账号没填，就用这个)
DEFAULT_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
DEFAULT_TG_CHATID = os.getenv("TELEGRAM_CHAT_ID") or ""

# 截图目录
SCREENSHOT_DIR = "screenshots"
if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

# =====================================================================
#                        账号解析模块
# =====================================================================

def parse_accounts():
    """
    解析环境变量 XSERVER_BATCH
    格式: LoginID,Password,IP,Token(选填),ChatID(选填)
    """
    accounts = []
    raw_data = os.getenv("XSERVER_BATCH")
    
    if not raw_data:
        # 兼容旧的单账号模式
        sid = os.getenv("XSERVER_LOGIN_ID")
        spw = os.getenv("XSERVER_PASSWORD")
        sip = os.getenv("XSERVER_IP")
        if sid and spw and sip:
            print("📋 检测到单账号环境变量模式")
            accounts.append({
                "id": sid, "pass": spw, "ip": sip,
                "tg_token": DEFAULT_TG_TOKEN, "tg_chat": DEFAULT_TG_CHATID
            })
        return accounts

    print("📋 检测到 XSERVER_BATCH 批量模式")
    for line in raw_data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        # 支持逗号或空格分割
        parts = [p.strip() for p in line.replace("，", ",").split(",")]
        
        if len(parts) >= 3:
            acc = {
                "id": parts[0],
                "pass": parts[1],
                "ip": parts[2],
                # 如果没填专属TG，就用全局默认
                "tg_token": parts[3] if len(parts) >= 5 else DEFAULT_TG_TOKEN,
                "tg_chat": parts[4] if len(parts) >= 5 else DEFAULT_TG_CHATID
            }
            accounts.append(acc)
        else:
            print(f"⚠️ 跳过格式错误行: {line}")
            
    return accounts

# =====================================================================
#                        Telegram 通知类
# =====================================================================

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send_message(self, message):
        if not self.enabled: return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"❌ TG发送失败: {e}")

    def send_result(self, login_id, ip, status, old_time, new_time):
        if not self.enabled: return
        
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        
        # ID 脱敏
        safe_id = login_id[:2] + "***" + login_id[-2:] if len(login_id) > 4 else login_id

        msg = f"<b>🎮 XServer 续期通知</b>\n"
        msg += f"🆔 账号: <code>{safe_id}</code>\n"
        msg += f"🖥 IP: <code>{ip}</code>\n"
        msg += f"⏰ 时间: {timestamp}\n\n"
        
        if status == "Success":
            msg += f"✅ <b>续期成功</b>\n📅 旧: {old_time}\n📅 新: {new_time}"
        elif status == "Unexpired":
            msg += f"ℹ️ <b>无需续期</b>\n📅 到期: {old_time}\n💡 剩余 > 24小时"
        elif status == "Failed":
            msg += f"❌ <b>执行失败</b>\n📅 到期: {old_time or '未知'}"
        else:
            msg += f"❓ 状态未知"
            
        self.send_message(msg)

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
        """启动浏览器"""
        p = await async_playwright().start()
        args = ['--no-sandbox', '--disable-blink-features=AutomationControlled']
        if USE_PROXY and PROXY_SERVER: args.append(f'--proxy-server={PROXY_SERVER}')
        
        self.browser = await p.chromium.launch(headless=USE_HEADLESS, args=args)
        
        ctx_opts = {'locale': 'ja-JP', 'viewport': {'width': 1920, 'height': 1080}}
        self.context = await self.browser.new_context(**ctx_opts)
        self.page = await self.context.new_page()
        await stealth_async(self.page)

    async def close(self):
        """关闭资源"""
        if self.context: await self.context.close()
        if self.browser: await self.browser.close()

    async def save_shot(self, name):
        """截图"""
        try:
            self.screenshot_idx += 1
            path = f"{SCREENSHOT_DIR}/{self.login_id}_{self.screenshot_idx}_{name}.png"
            await self.page.screenshot(path=path)
        except: pass

    async def run_task(self):
        """执行单个账号的任务流程"""
        try:
            await self.start()
            print(f"🚀 [{self.login_id}] 开始处理 IP: {self.login_ip}")
            
            # 1. 登录
            await self.page.goto(TARGET_URL, wait_until='load', timeout=60000)
            await self.page.wait_for_selector("input[type='password']", timeout=WAIT_TIMEOUT)
            
            # 填写表单 (ID, Pass, IP)
            # 这里的定位逻辑是按顺序填写 input[type=text/password]
            inputs = await self.page.locator("input:not([type='hidden']):not([type='submit'])").all()
            
            if len(inputs) >= 3:
                await inputs[0].fill(self.login_id)
                await inputs[1].fill(self.password) # 假设第二个框是密码
                await inputs[2].fill(self.login_ip) # 假设第三个框是IP
            else:
                # 备用：按类型查找
                await self.page.locator("input[type='text']").nth(0).fill(self.login_id)
                await self.page.locator("input[type='password']").fill(self.password)
                await self.page.locator("input[type='text']").nth(1).fill(self.login_ip)

            await self.page.click("input[value='ログインする'], button:has-text('ログインする')")
            await self.page.wait_for_load_state('networkidle')
            
            # 验证登录
            if "xmgame/game/index" not in self.page.url:
                print(f"❌ [{self.login_id}] 登录失败，当前URL: {self.page.url}")
                self.status = "Failed"
                await self.save_shot("login_fail")
                return

            print(f"✅ [{self.login_id}] 登录成功")
            await self.save_shot("login_success")

            # 2. 获取信息
            await self.check_and_renew()

        except Exception as e:
            print(f"❌ [{self.login_id}] 异常: {e}")
            self.status = "Failed"
        finally:
            # 发送通知并关闭
            self.notifier.send_result(self.login_id, self.login_ip, self.status, self.old_expiry, self.new_expiry)
            await self.close()

    async def check_and_renew(self):
        """获取时间并续期"""
        try:
            # 提取剩余时间文本
            elements = await self.page.locator("text=/残り.*時間/").all()
            for el in elements:
                txt = await el.text_content()
                if "残り" in txt:
                    # 提取日期 (YYYY-MM-DD)
                    match = re.search(r'\((\d{4}-\d{2}-\d{2})まで\)', txt)
                    if match:
                        self.old_expiry = match.group(1)
                        print(f"📅 [{self.login_id}] 当前到期: {self.old_expiry}")
                    break
            
            # 查找续期按钮
            renew_btn = self.page.locator("a:has-text('アップグレード・期限延長')")
            if not await renew_btn.count():
                print(f"⚠️ [{self.login_id}] 未找到续期按钮")
                self.status = "Failed" # 或者 Unexpired，视情况而定
                return

            await renew_btn.click()
            await self.page.wait_for_load_state('networkidle')

            # 检查24小时限制
            if "残り契約時間が24時間を切るまで" in await self.page.content():
                print(f"ℹ️ [{self.login_id}] 未满足续期条件 (>24h)")
                self.status = "Unexpired"
                return

            # 执行续期流程
            print(f"🔄 [{self.login_id}] 开始续期操作...")
            await self.page.click("a:has-text('期限を延長する')")
            await self.page.wait_for_load_state('networkidle')
            
            await self.page.click("button:has-text('確認画面に進む')")
            await self.page.wait_for_load_state('networkidle')
            
            # 抓取新日期预览
            try:
                self.new_expiry = await self.page.locator("tr:has(th:has-text('延長後の期限')) td").first.text_content()
                self.new_expiry = self.new_expiry.strip()
            except: pass

            # 最终确认
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
#                        主程序入口
# =====================================================================

async def main():
    print("=" * 60)
    print("XServer 多账号批量续期脚本 (支持随机延迟)")
    print("=" * 60)

    accounts = parse_accounts()
    if not accounts:
        print("❌ 未找到有效账号配置，请检查 XSERVER_BATCH 环境变量")
        exit(1)

    print(f"📋 共加载 {len(accounts)} 个账号\n")

    for i, acc in enumerate(accounts):
        bot = XServerBot(acc)
        await bot.run_task()
        
        # 如果不是最后一个账号，则进行随机等待
        if i < len(accounts) - 1:
            delay = random.randint(1, 100)
            print(f"\n⏳ 等待 {delay} 秒后处理下一个账号...\n")
            await asyncio.sleep(delay)

    print("\n✅ 所有账号处理完毕")

if __name__ == "__main__":
    asyncio.run(main())
