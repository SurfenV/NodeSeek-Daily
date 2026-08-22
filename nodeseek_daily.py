# -- coding: utf-8 --
"""
Copyright (c) 2024 [Hosea]
Licensed under the MIT License.
See LICENSE file in the project root for full license information.
"""
import os
import json
import base64
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random
import time
import traceback
import undetected_chromedriver as uc
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

# undetected-chromedriver 3.5.5 在解释器退出时的 __del__ 里会尝试再次
# 关闭已退出的进程，抛出无意义的异常并污染 CI 日志/退出码，这里屏蔽掉。
uc.Chrome.__del__ = lambda self: None

def env_bool(name, default="false"):
    """把环境变量解析成布尔值。注意 os.environ.get(name,"false") 返回的是
    非空字符串 "false"，直接 if 判断永远为真，原实现在此处有 bug。"""
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


ns_random = env_bool("NS_RANDOM")
cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
# 通过环境变量控制是否使用无头模式，默认为 True（无头模式）
headless = env_bool("HEADLESS", "true")

# 每次运行评论多少个帖子。刷屏式评论极易被举报禁言，默认收敛到 3 个；
# 设为 0 可完全关闭评论，只保留签到与加鸡腿。
comment_count = env_int("NS_COMMENT_COUNT", 3)

# 打开后每一步都会保存截图和页面源码，用于排查选择器失效
debug_mode = env_bool("NS_DEBUG")

randomInputStr = ["帮顶", "帮顶一个", "顶一下", "支持一下", "支持", "蹲一个", "祝出货顺利"]

# 在已经通过 Cloudflare 的页面上下文里发起请求，自动携带 cookie 与
# 正确的 TLS/JA3 指纹，比在外部用 requests 直连可靠得多。
_FETCH_JS = """
const cb = arguments[arguments.length - 1];
fetch(arguments[0], {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include'
}).then(r => r.text().then(t => cb(JSON.stringify({status: r.status, body: t}))))
  .catch(e => cb(JSON.stringify({error: String(e)})));
"""


def click_sign_icon(driver):
    """
    执行每日签到。返回 True 表示今日签到已落实（含「今天已经签过」）。

    直接调用站点自己的签到接口，不再去点导航栏那个图标：那个 span 位于
    sticky 头部内，原生点击会被 #nsk-head 拦截，而 JS 点击虽然能生效却
    没有任何可观测的反馈（页面不跳转也不弹窗），无法判断成败。
    接口会明确返回 success 与 message，这是唯一可靠的判定依据。
    """
    path = "/api/attendance?random=%s" % ("true" if ns_random else "false")
    print(f"正在签到: POST {path}")

    try:
        driver.set_script_timeout(45)
        raw = driver.execute_async_script(_FETCH_JS, path)
        resp = json.loads(raw)
    except Exception as e:
        print(f"签到请求发送失败: {type(e).__name__}: {str(e)}")
        traceback.print_exc()
        return False

    if "error" in resp:
        print(f"签到请求出错: {resp['error']}")
        return False

    # 注意：重复签到时接口返回的是 HTTP 500，但 body 是有意义的 JSON，
    # 所以只能按 body 判断，不能看状态码。
    body = resp.get("body", "")
    try:
        data = json.loads(body)
    except ValueError:
        print(f"签到响应不是 JSON（HTTP {resp.get('status')}）: {body[:300]}")
        return False

    message = data.get("message", "")

    if data.get("success"):
        gain = data.get("gain")
        current = data.get("current")
        print(f"✅ 签到成功: {message}"
              + (f"（+{gain}，当前 {current}）" if gain is not None else ""))
        return True

    if any(k in message for k in ("今天已完成签到", "已完成签到", "已签到", "请勿重复")):
        print(f"✅ 今日已签到，无需重复: {message}")
        return True

    print(f"❌ 签到失败（HTTP {resp.get('status')}）: {message or body[:300]}")
    if "未登录" in message or "登录" in message:
        print("!! Cookie 可能已失效，请更新 NS_COOKIE")
    return False


def setup_driver_and_cookies():
    """
    初始化浏览器并设置cookie的通用方法
    返回: 设置好cookie的driver实例
    """
    try:
        cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
        headless = os.environ.get("HEADLESS", "true").lower() == "true"
        
        if not cookie:
            print("未找到cookie配置")
            return None
            
        print("开始初始化浏览器...")
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        # 有头模式下（CI 里跑在 Xvfb 上）同样需要足够大的视口，
        # 否则部分按钮会落在可视区域外导致点击失败。
        options.add_argument('--window-size=1920,1080')
        
        if headless:
            print("启用无头模式...")
            options.add_argument('--headless')
            # 添加以下参数来绕过 Cloudflare 检测
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument('--disable-gpu')
            options.add_argument('--window-size=1920,1080')
            # 设置 User-Agent
            options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        print("正在启动Chrome...")
        uc_kwargs = {"options": options}
        # CI 上把 runner 实际的 Chrome 主版本传进来，避免 uc 自动探测失败
        # 后下载到不匹配的 chromedriver。
        main_version = os.environ.get("CHROME_MAIN_VERSION", "").strip()
        if main_version.isdigit():
            uc_kwargs["version_main"] = int(main_version)
            print(f"指定 Chrome 主版本: {main_version}")
        driver = uc.Chrome(**uc_kwargs)
        
        if headless:
            # 执行 JavaScript 来修改 webdriver 标记
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            driver.set_window_size(1920, 1080)
        
        print("Chrome启动成功")
        
        print("正在设置cookie...")
        driver.get('https://www.nodeseek.com')
        
        # 等待页面加载完成
        time.sleep(5)
        
        for cookie_item in cookie.split(';'):
            try:
                name, value = cookie_item.strip().split('=', 1)
                driver.add_cookie({
                    'name': name, 
                    'value': value, 
                    'domain': '.nodeseek.com',
                    'path': '/'
                })
            except Exception as e:
                print(f"设置cookie出错: {str(e)}")
                continue
        
        print("刷新页面...")
        driver.refresh()
        time.sleep(5)  # 增加等待时间
        
        return driver
        
    except Exception as e:
        print(f"设置浏览器和Cookie时出错: {str(e)}")
        print("详细错误信息:")
        print(traceback.format_exc())
        return None

def nodeseek_comment(driver):
    try:
        print("正在访问交易区...")
        target_url = 'https://www.nodeseek.com/categories/trade'
        driver.get(target_url)
        print("等待页面加载...")
        
        # 获取初始帖子列表
        posts = WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, '.post-list-item'))
        )
        print(f"成功获取到 {len(posts)} 个帖子")
        
        # 过滤掉置顶帖
        valid_posts = [post for post in posts if not post.find_elements(By.CSS_SELECTOR, '.pined')]
        selected_posts = random.sample(valid_posts, min(comment_count, len(valid_posts)))
        
        # 存储已选择的帖子URL
        selected_urls = []
        for post in selected_posts:
            try:
                post_link = post.find_element(By.CSS_SELECTOR, '.post-title a')
                selected_urls.append(post_link.get_attribute('href'))
            except:
                continue
        
        is_chicken_leg = False
        done = 0

        # 使用URL列表进行操作
        for i, post_url in enumerate(selected_urls):
            try:
                print(f"正在处理第 {i+1} 个帖子")
                driver.get(post_url)
                
                # 处理加鸡腿
                if is_chicken_leg is False:
                    is_chicken_leg = click_chicken_leg(driver)
                
                # 等待 CodeMirror 编辑器加载
                editor = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '.CodeMirror'))
                )
                
                # 点击编辑器区域获取焦点
                editor.click()
                time.sleep(0.5)
                input_text = random.choice(randomInputStr)

                # 模拟输入
                actions = ActionChains(driver)
                # 随机输入 randomInputStr
                for char in input_text:
                    actions.send_keys(char)
                    actions.pause(random.uniform(0.1, 0.3))
                actions.perform()
                
                # 等待一下确保内容已经输入
                time.sleep(2)
                
                # 使用更精确的选择器定位提交按钮
                submit_button = WebDriverWait(driver, 30).until(
                 EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'submit') and contains(@class, 'btn') and contains(text(), '发布评论')]"))
                )
                # 确保按钮可见并可点击
                # 必须用 block:'center'。scrollIntoView(true) 会把元素对齐到
                # 视口顶端，而 NodeSeek 的 #nsk-head 是 sticky 头部，会把按钮盖住，
                # 导致 element click intercepted。
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_button)
                time.sleep(0.5)
                try:
                    submit_button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", submit_button)
                
                done += 1
                print(f"已在帖子 {post_url} 中完成评论")
                
                # 返回交易区
                # driver.get(target_url)
                # time.sleep(2)  # 等待页面加载
                time.sleep(random.uniform(2,5))
                
            except Exception as e:
                print(f"处理帖子时出错: {str(e)}")
                continue
                
        print(f"NodeSeek评论任务完成，成功 {done} 个")
        return done

    except Exception as e:
        print(f"NodeSeek评论出错: {str(e)}")
        print("详细错误信息:")
        print(traceback.format_exc())
        return 0

def click_chicken_leg(driver):
    try:
        print("尝试点击加鸡腿按钮...")
        chicken_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, '//div[@class="nsk-post"]//div[@title="加鸡腿"][1]'))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", chicken_btn)
        time.sleep(0.5)
        chicken_btn.click()
        print("加鸡腿按钮点击成功")
        
        # 等待确认对话框出现
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.msc-confirm'))
        )
        
        # 检查是否是7天前的帖子
        try:
            error_title = driver.find_element(By.XPATH, "//h3[contains(text(), '该评论创建于7天前')]")
            if error_title:
                print("该帖子超过7天，无法加鸡腿")
                ok_btn = driver.find_element(By.CSS_SELECTOR, '.msc-confirm .msc-ok')
                ok_btn.click()
                return False
        except:
            ok_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '.msc-confirm .msc-ok'))
            )
            ok_btn.click()
            print("确认加鸡腿成功")
            
        # 等待确认对话框消失
        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.msc-overlay'))
        )
        time.sleep(1)  # 额外等待以确保对话框完全消失
        
        return True
        
    except Exception as e:
        print(f"加鸡腿操作失败: {str(e)}")
        return False

# NodeSeek 的登录态有效期为 30 天，且服务端不做滑动续期——每次运行后
# 浏览器都拿不到新的 Set-Cookie，所以到期只能重新登录换 Cookie。
SESSION_TTL_DAYS = 30


def report_cookie_status(driver):
    """算出登录态还能撑多久，临近过期时在 Actions 摘要里告警。

    优先用浏览器里的 expiry（万一哪天服务端开始续期，这里能自动跟上）；
    读不到就退回解析 pjwt —— 它是 JWT，payload 里的 ts 是签发时间戳。
    """
    KEYS = ("session", "pjwt", "smac")
    now = time.time()

    expiry = None
    source = ""
    try:
        found = {c["name"]: c["expiry"] for c in driver.get_cookies()
                 if c["name"] in KEYS and c.get("expiry")}
        if found:
            expiry, source = min(found.values()), "浏览器 Cookie"
    except Exception:
        pass

    if expiry is None:
        for item in (cookie or "").split(";"):
            item = item.strip()
            if not item.startswith("pjwt="):
                continue
            try:
                payload = item[len("pjwt="):].split(".")[0]
                payload += "=" * (-len(payload) % 4)
                issued = json.loads(base64.urlsafe_b64decode(payload)).get("ts")
                if issued:
                    expiry = issued + SESSION_TTL_DAYS * 86400
                    source = "pjwt 签发时间推算"
            except Exception as e:
                print(f"解析 pjwt 失败: {str(e)}")
            break

    if expiry is None:
        print("无法判断 Cookie 有效期")
        return

    days = (expiry - now) / 86400
    stamp = time.strftime("%Y-%m-%d", time.gmtime(expiry))
    print(f"登录态过期时间: {stamp} UTC（{days:.1f} 天后，依据：{source}）")

    if days < 0:
        print("::error::NS_COOKIE 已过期，请重新登录 NodeSeek 并更新 Secret")
    elif days < 7:
        print(f"::warning::NS_COOKIE 还有 {days:.1f} 天过期（{stamp}）。"
              f"请重新登录 NodeSeek，导出 Cookie 后更新仓库 Secret NS_COOKIE，"
              f"否则签到将开始失败")

    # 交给 workflow：GitHub 只在 job 失败时发邮件，摘要里的 warning 不会
    # 触达用户，所以临期时另开 Issue 通知。
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        try:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"cookie_days={days:.1f}\n")
                f.write(f"cookie_expiry={stamp}\n")
                f.write(f"cookie_expiring={'true' if days < 7 else 'false'}\n")
        except Exception as e:
            print(f"写入 GITHUB_OUTPUT 失败: {str(e)}")


def save_debug_artifacts(driver, tag):
    """出错时留下现场，方便在 Actions 里下载排查。"""
    try:
        driver.save_screenshot(f"debug-{tag}.png")
        with open(f"debug-{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"已保存现场: debug-{tag}.png / debug-{tag}.html")
    except Exception as e:
        print(f"保存现场失败: {str(e)}")


if __name__ == "__main__":
    print("开始执行NodeSeek脚本...")
    driver = setup_driver_and_cookies()
    if not driver:
        print("浏览器初始化失败")
        exit(1)

    signed = False
    try:
        # 先签到再评论：签到是主要收益，即使后续评论环节出问题也不影响它。
        signed = click_sign_icon(driver)
        if not signed or debug_mode:
            save_debug_artifacts(driver, "sign")

        if comment_count > 0:
            nodeseek_comment(driver)
        else:
            print("NS_COMMENT_COUNT=0，跳过评论环节")

        report_cookie_status(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print("脚本执行完成")
    # 签到失败时以非 0 退出，让 CI 的重试机制生效。
    exit(0 if signed else 1)

