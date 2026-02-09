#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本 (独立面板登录版)
"""

# =====================================================================
#                          导入依赖
# =====================================================================

import asyncio
import time
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import requests
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from playwright_stealth import stealth_async

# =====================================================================
#                          配置区域
# =====================================================================

# 浏览器配置
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = 10000     # 页面元素等待超时时间(毫秒)
PAGE_LOAD_DELAY = 3      # 页面加载延迟时间(秒)

# 代理配置 - 可选
PROXY_SERVER = os.getenv("PROXY_SERVER") or ""
USE_PROXY = bool(PROXY_SERVER)

# --- XServer Game Panel 登录配置 (已更新) ---
# 登录页面: https://secure.xserver.ne.jp/xapanel/login/xmgame/game/
LOGIN_ID = os.getenv("XSERVER_LOGIN_ID") or "xm60591967"
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD") or "te0yd9k2bx9a"
LOGIN_IP = os.getenv("XSERVER_IP") or "210.131.217.237"

TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame/game/"
EXPECTED_INDEX_URL = "https://secure.xserver.ne.jp/xmgame/game/index"

# Telegram配置
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8550805872:AAEiDpg6QlHrQannn9z_HGz7DmcEFlD30tI"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "7707990981"

# =====================================================================
#                        Telegram 推送模块
# =====================================================================

class TelegramNotifier:
    """Telegram 通知推送类"""
    
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            print("ℹ️ Telegram 推送未启用(缺少 BOT_TOKEN 或 CHAT_ID)")
    
    def send_message(self, message, parse_mode="HTML"):
        """发送 Telegram 消息"""
        if not self.enabled:
            print("⚠️ Telegram 推送未启用,跳过发送")
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            if result.get("ok"):
                print("✅ Telegram 消息发送成功")
                return True
            else:
                print(f"❌ Telegram 消息发送失败: {result.get('description')}")
                return False
                
        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
            return False
    
    def send_renewal_result(self, status, old_time, new_time=None, run_time=None):
        """发送续期结果通知"""
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = run_time or beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 构建消息
        message = f"<b>🎮 XServer GAME 续期通知</b>\n\n"
        message += f"🕐 运行时间: <code>{timestamp}</code>\n"
        message += f"🖥 服务器IP: <code>{LOGIN_IP}</code>\n\n"
        
        if status == "Success":
            message += f"📊 续期结果: <b>✅ 成功</b>\n"
            message += f"🕛 旧到期: <code>{old_time}</code>\n"
            message += f"🕡 新到期: <code>{new_time}</code>\n"
        elif status == "Unexpired":
            message += f"📊 续期结果: <b>ℹ️ 未到期</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"💡 提示: 剩余时间超过24小时,无需续期\n"
        elif status == "Failed":
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"⚠️ 请检查日志或手动续期\n"
        else:
            message += f"📊 续期结果: <b>❓ 未知</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
        
        return self.send_message(message)

# =====================================================================
#                        XServer 自动登录类
# =====================================================================

class XServerAutoLogin:
    """XServer GAME 自动登录主类 - Playwright版本"""
    
    def __init__(self):
        """初始化"""
        self.browser = None
        self.context = None
        self.page = None
        self.headless = USE_HEADLESS
        # 使用新的配置变量
        self.login_id = LOGIN_ID
        self.password = LOGIN_PASSWORD
        self.login_ip = LOGIN_IP
        
        self.target_url = TARGET_URL
        self.wait_timeout = WAIT_TIMEOUT
        self.page_load_delay = PAGE_LOAD_DELAY
        self.screenshot_count = 0
        
        # 续期状态跟踪
        self.old_expiry_time = None
        self.new_expiry_time = None
        self.renewal_status = "Unknown"
        
        self.telegram = TelegramNotifier()
    
    
    # =================================================================
    #                        1. 浏览器管理模块
    # =================================================================
        
    async def setup_browser(self):
        """设置并启动 Playwright 浏览器"""
        try:
            playwright = await async_playwright().start()
            
            browser_args = [
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                '--disable-notifications', '--window-size=1920,1080',
                '--lang=ja-JP', '--accept-lang=ja-JP,ja,en-US,en'
            ]
            
            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理: {PROXY_SERVER}")
                browser_args.append(f'--proxy-server={PROXY_SERVER}')
            
            self.browser = await playwright.chromium.launch(
                headless=self.headless,
                args=browser_args
            )
            
            context_options = {
                'viewport': {'width': 1920, 'height': 1080},
                'locale': 'ja-JP',
                'timezone_id': 'Asia/Tokyo',
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            if USE_PROXY and PROXY_SERVER:
                context_options['proxy'] = {'server': PROXY_SERVER}
            
            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()
            
            await stealth_async(self.page)
            print("✅ Stealth 插件已应用")
            
            return True
            
        except Exception as e:
            print(f"❌ Playwright 浏览器初始化失败: {e}")
            return False
    
    async def take_screenshot(self, step_name=""):
        """截图功能"""
        try:
            if self.page:
                self.screenshot_count += 1
                beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
                timestamp = beijing_time.strftime("%H%M%S")
                filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
                filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                await self.page.screenshot(path=filename, full_page=True)
                print(f"📸 截图已保存: {filename}")
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")
    
    def validate_config(self):
        """验证配置信息"""
        if not self.login_id or not self.password or not self.login_ip:
            print("❌ 登录信息不完整! 请检查 ID, 密码和 IP 设置。")
            return False
        print("✅ 配置信息验证通过")
        return True
    
    async def cleanup(self):
        """清理资源"""
        try:
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            print("🧹 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 清理资源时出错: {e}")
    
    # =================================================================
    #                        2. 页面导航与登录模块 (已重写)
    # =================================================================
    
    async def navigate_to_login(self):
        """导航到登录页面"""
        try:
            print(f"🌐 正在访问: {self.target_url}")
            await self.page.goto(self.target_url, wait_until='load')
            await self.page.wait_for_selector("body", timeout=self.wait_timeout)
            print("✅ 登录页面加载成功")
            await self.take_screenshot("login_page_loaded")
            return True
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False
    
    async def perform_login(self):
        """执行游戏面板登录操作"""
        try:
            print("🎯 开始执行登录操作 (游戏面板独立登录)...")
            await asyncio.sleep(self.page_load_delay)
            
            # 针对新的3个输入框的登录界面进行定位
            # 1. 登录ID (Login ID)
            # 2. 游戏面板密码 (Game Panel Password)
            # 3. 域名或IP (Domain or IP Address)
            
            print("📝 正在填写登录信息...")
            
            # 使用更通用的定位方式，防止name属性变化，按照输入框顺序或类型定位
            # 通常 XServer 的 name 属性: login_id, password, server_name (或类似)
            
            # --- 填写 ID ---
            # 尝试通过 placeholder 或 label 关联，或者简单的 input[type=text] 顺序
            # 截图显示第一个框是 ID
            id_input = self.page.locator("input[type='text']").nth(0) 
            # 备用方案: input[name='login_id']
            if not await id_input.is_visible():
                id_input = self.page.locator("input[name='login_id']")
            
            await id_input.fill(self.login_id)
            print("✅ ID 已填写")
            await asyncio.sleep(0.5)

            # --- 填写 密码 ---
            password_input = self.page.locator("input[type='password']")
            await password_input.fill(self.password)
            print("✅ 密码已填写")
            await asyncio.sleep(0.5)

            # --- 填写 IP ---
            # 截图显示第三个框是 IP，通常是页面上第二个 type='text' 的框 (ID是第一个)
            ip_input = self.page.locator("input[type='text']").nth(1)
            # 备用方案: input[name='server_name']
            if not await ip_input.is_visible():
                ip_input = self.page.locator("input[name='server_name']")
                
            await ip_input.fill(self.login_ip)
            print("✅ IP 已填写")
            await asyncio.sleep(1.0)
            
            # --- 点击登录 ---
            login_button = self.page.locator("input[type='submit'][value='ログインする'], button:has-text('ログインする')")
            print("🖱️ 点击登录按钮...")
            
            # 并发处理点击和导航等待
            async with self.page.expect_navigation(timeout=30000):
                await login_button.click()
            
            print("✅ 登录表单提交完成")
            return True
            
        except Exception as e:
            print(f"❌ 登录操作失败: {e}")
            await self.take_screenshot("login_error")
            return False
    
    async def handle_login_result(self):
        """处理登录结果 - 这种登录方式通常直接跳转到 Index"""
        try:
            print("🔍 正在检查登录结果...")
            await asyncio.sleep(3)
            
            current_url = self.page.url
            print(f"🔍 当前URL: {current_url}")
            
            # 检查是否包含预期的 index 路径
            if "xmgame/game/index" in current_url:
                print("✅ 登录成功! 已到达游戏管理页面")
                await self.take_screenshot("game_page_loaded")
                
                # 直接获取时间信息，不需要再点击"游戏管理"按钮
                await self.get_server_time_info()
                return True
            else:
                print(f"❌ 登录可能失败，未到达预期页面。")
                print(f"   预期包含: xmgame/game/index")
                await self.take_screenshot("login_failed_url")
                return False
                
        except Exception as e:
            print(f"❌ 检查登录结果时出错: {e}")
            return False
            
    # =================================================================
    #                        3. 续期逻辑模块 (保持不变)
    # =================================================================
    
    async def get_server_time_info(self):
        """获取服务器时间信息"""
        try:
            print("🕒 正在获取服务器时间信息...")
            await asyncio.sleep(3)
            
            # 使用已验证有效的选择器
            try:
                elements = await self.page.locator("text=/残り\\d+時間\\d+分/").all()
                
                for element in elements:
                    element_text = await element.text_content()
                    element_text = element_text.strip() if element_text else ""
                    
                    if element_text and len(element_text) < 200 and "残り" in element_text and "時間" in element_text:
                        print(f"✅ 找到时间元素: {element_text}")
                        
                        remaining_match = re.search(r'残り(\d+時間\d+分)', element_text)
                        if remaining_match:
                            print(f"⏰ 剩余时间: {remaining_match.group(1)}")
                        
                        expiry_match = re.search(r'\((\d{4}-\d{2}-\d{2})まで\)', element_text)
                        if expiry_match:
                            self.old_expiry_time = expiry_match.group(1)
                            print(f"📅 到期时间: {self.old_expiry_time}")
                        
                        break
            except Exception as e:
                print(f"❌ 获取时间元素出错: {e}")
            
            # 继续执行升级逻辑
            await self.click_upgrade_button()
            
        except Exception as e:
            print(f"❌ 获取服务器时间信息流程失败: {e}")
    
    async def click_upgrade_button(self):
        """点击升级延长按钮"""
        try:
            print("📄 正在查找アップグレード・期限延長按钮...")
            upgrade_selector = "a:has-text('アップグレード・期限延長')"
            
            try:
                await self.page.wait_for_selector(upgrade_selector, timeout=5000)
                await self.page.click(upgrade_selector)
                print("✅ 已点击アップグレード・期限延長按钮")
                await asyncio.sleep(5)
                await self.verify_upgrade_page()
            except Exception:
                print("⚠️ 未找到续期按钮，可能页面布局不同或已到期。")
                
        except Exception as e:
            print(f"❌ 点击升级按钮失败: {e}")
    
    async def verify_upgrade_page(self):
        """验证升级页面并检查限制"""
        try:
            if "freeplan/extend/index" in self.page.url:
                print("✅ 成功跳转到升级页面")
                await self.check_extension_restriction()
            else:
                print(f"❌ 升级页面跳转失败: {self.page.url}")
        except Exception as e:
            print(f"❌ 验证升级页面失败: {e}")
    
    async def check_extension_restriction(self):
        """检查期限延长限制信息"""
        try:
            print("🔍 正在检测期限延长限制提示...")
            restriction_selector = "text=/残り契約時間が24時間を切るまで、期限の延長は行えません/"
            
            try:
                element = await self.page.wait_for_selector(restriction_selector, timeout=5000)
                print(f"✅ 找到期限延长限制信息 (剩余时间 > 24小时)")
                self.renewal_status = "Unexpired"
                return True
            except Exception:
                print("ℹ️ 未找到限制信息, 可以进行延长操作")
                await self.perform_extension_operation()
                return False
                
        except Exception as e:
            print(f"❌ 检测限制失败: {e}")
            return True
    
    async def perform_extension_operation(self):
        """执行期限延长操作"""
        try:
            print("📄 开始执行期限延长操作...")
            
            # 1. 点击 "期限を延長する"
            extension_selector = "a:has-text('期限を延長する')"
            await self.page.click(extension_selector)
            await asyncio.sleep(5)
            
            # 2. 点击 "確認画面に進む"
            if "freeplan/extend/input" in self.page.url:
                print("✅ 已进入输入页，点击确认...")
                confirm_btn = "button[type='submit']:has-text('確認画面に進む')"
                await self.page.click(confirm_btn)
                await asyncio.sleep(5)
                
                # 3. 最终确认页
                if "freeplan/extend/conf" in self.page.url:
                    print("✅ 已进入确认页，获取新期限并提交...")
                    # 尝试记录新时间
                    try:
                        time_el = await self.page.query_selector("tr:has(th:has-text('延長後の期限')) td")
                        if time_el:
                            self.new_expiry_time = (await time_el.text_content()).strip()
                            print(f"📅 预计新期限: {self.new_expiry_time}")
                    except: pass
                    
                    final_btn = "button[type='submit']:has-text('期限を延長する')"
                    await self.page.click(final_btn)
                    await asyncio.sleep(5)
                    await self.verify_extension_success()
                else:
                    print("❌ 未进入确认页面")
            else:
                print("❌ 未进入续期输入页面")
                
        except Exception as e:
            print(f"❌ 执行期限延长操作失败: {e}")
    
    async def verify_extension_success(self):
        """验证是否成功"""
        try:
            if "freeplan/extend/do" in self.page.url:
                print("🎉 续期操作成功! (URL验证)")
                self.renewal_status = "Success"
                await self.take_screenshot("extension_success")
            else:
                # 检查文本
                try:
                    await self.page.wait_for_selector("p:has-text('期限を延長しました。')", timeout=5000)
                    print("🎉 续期操作成功! (文本验证)")
                    self.renewal_status = "Success"
                except:
                    print("❌ 未检测到成功信号")
                    self.renewal_status = "Failed"
                    await self.take_screenshot("extension_failed")
        except Exception as e:
            print(f"❌ 验证结果失败: {e}")
            self.renewal_status = "Failed"

    # =================================================================
    #                        4. 报告生成模块
    # =================================================================

    def generate_report_notify(self):
        """生成报告并推送"""
        try:
            print("📝 正在生成报告...")
            # 简单生成文件，主要依赖 Telegram 推送
            with open("report-notify.md", "w", encoding="utf-8
