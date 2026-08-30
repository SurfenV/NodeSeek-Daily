# -- coding: utf-8 --
"""
Copyright (c) 2024 [Hosea]
Licensed under the MIT License.
See LICENSE file in the project root for full license information.
"""
import os
import json
import base64
import urllib.request
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

# 分批评论：每 NS_COMMENT_BATCH 个之后停 NS_COMMENT_INTERVAL 秒。
# 连续快速刷评论既容易触发站点风控，也更容易被人看出是机器人。
comment_batch = env_int("NS_COMMENT_BATCH", 3) or 3
comment_interval = env_int("NS_COMMENT_INTERVAL", 300)

# 每天可免费投喂（加鸡腿）的次数，站点当前额度是 2
chicken_count = env_int("NS_CHICKEN_COUNT", 2)

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


SESSION_TTL_DAYS = 30

# 签到返回值：区分「这次没成功，可以重试」和「凭证废了，重试也没用」
CREDENTIAL_INVALID = "credential_invalid"

# 一次运行的结果汇总，跑完后组装成一条 Bark 通知
summary = {
    "sign": None,          # True / False / CREDENTIAL_INVALID
    "sign_message": "",
    "gain": None,
    "current": None,
    "comment_done": 0,
    "comment_target": 0,
    "chicken": False,
    "chicken_done": 0,
    "cookie_days": None,
}


def bark_notify(title, body, url=None):
    """推送到 Bark。失败只记日志，绝不影响签到主流程。"""
    key = os.environ.get("BARK_KEY", "").strip()
    if not key:
        print("未配置 BARK_KEY，跳过推送")
        return

    server = os.environ.get("BARK_SERVER", "https://api.day.app").strip().rstrip("/")
    payload = {
        "title": title,
        "body": body,
        "group": os.environ.get("BARK_GROUP", "NodeSeek").strip() or "NodeSeek",
    }
    if url:
        payload["url"] = url

    # 用 POST + JSON，避免正文里的换行和斜杠被 URL 路径截断
    req = urllib.request.Request(
        f"{server}/{key}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = json.loads(r.read().decode("utf-8", "replace")).get("code") == 200
        print("Bark 推送成功" if ok else "Bark 推送返回异常")
    except Exception as e:
        print(f"Bark 推送失败: {type(e).__name__}: {str(e)}")


def send_summary_notification():
    """把本次运行的结果组装成一条通知。"""
    sign = summary["sign"]
    if sign is True:
        title = "✅ NodeSeek 签到成功"
    elif sign is CREDENTIAL_INVALID:
        title = "🔑 NodeSeek Cookie 已失效"
    else:
        title = "❌ NodeSeek 签到失败"

    lines = [summary["sign_message"] or "(无返回信息)"]

    if sign is True and summary["current"] is not None:
        gain = summary["gain"]
        lines[0] = (f"签到 +{gain}，当前 {summary['current']} 个鸡腿"
                    if gain is not None else f"当前 {summary['current']} 个鸡腿")

    if summary["comment_target"]:
        lines.append(f"评论 {summary['comment_done']}/{summary['comment_target']}"
                     f" · 投喂 {summary['chicken_done']}/{chicken_count}")

    if sign is CREDENTIAL_INVALID:
        lines.append("需重新登录 NodeSeek 并更新 Secret NS_COOKIE")
    elif summary["cookie_days"] is not None:
        days = summary["cookie_days"]
        lines.append(f"Cookie 剩 {days:.0f} 天"
                     + ("（请尽快更新）" if days < 7 else ""))

    # 点通知直接跳到本次运行的日志页
    url = None
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        url = f"{server}/{repo}/actions/runs/{run_id}"

    bark_notify(title, "\n".join(lines), url)


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
        summary.update(sign=True, sign_message=message, gain=gain, current=current)
        return True

    if any(k in message for k in ("今天已完成签到", "已完成签到", "已签到", "请勿重复")):
        print(f"✅ 今日已签到，无需重复: {message}")
        summary.update(sign=True, sign_message=message)
        return True

    print(f"❌ 签到失败（HTTP {resp.get('status')}）: {message or body[:300]}")

    # 凭证失效是不会自愈的，重试只是白白多跑几分钟。用单独的返回值让
    # 上层直接跳过重试。退出登录、改密码、或超过 30 天都会走到这里。
    upper = (message or body).upper()
    if any(k in upper for k in ("USER NOT FOUND", "NOT LOGIN", "UNAUTHORIZED")) \
            or any(k in message for k in ("未登录", "请先登录", "登录已失效")):
        print("!! Cookie 已失效（可能是退出登录、改过密码，或已超过 30 天有效期）")
        print("!! 请重新登录 NodeSeek 并更新 Secret NS_COOKIE，重试无意义")
        summary.update(sign=CREDENTIAL_INVALID, sign_message=message or "USER NOT FOUND")
        return CREDENTIAL_INVALID

    summary.update(sign=False, sign_message=message or body[:200])
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

def collect_candidate_posts(driver, need):
    """收集可能可以评论的帖子链接。

    实测交易区第一页约 49 个非置顶帖，但其中相当一部分评论不了（老帖、
    已关闭回复、需要更高权限）。所以候选要按目标数的数倍来备，不够就翻页，
    否则凑不满想要的评论数。
    """
    urls = []
    seen = set()
    page = 1

    while len(urls) < need and page <= 5:
        url = 'https://www.nodeseek.com/categories/trade'
        if page > 1:
            url += f'?page={page}'
        print(f"正在读取交易区第 {page} 页...")
        driver.get(url)

        try:
            posts = WebDriverWait(driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, '.post-list-item'))
            )
        except Exception:
            print(f"第 {page} 页没读到帖子列表，停止翻页")
            break

        added = 0
        for post in posts:
            # 跳过置顶帖
            if post.find_elements(By.CSS_SELECTOR, '.pined'):
                continue
            try:
                link = post.find_element(By.CSS_SELECTOR, '.post-title a').get_attribute('href')
            except Exception:
                continue
            if link and link not in seen:
                seen.add(link)
                urls.append(link)
                added += 1

        print(f"   第 {page} 页新增 {added} 个候选，累计 {len(urls)} 个")
        if added == 0:
            break
        page += 1

    # 打乱顺序，避免每天都从同几个最新帖子开始
    random.shuffle(urls)
    return urls


def comment_one_post(driver, post_url):
    """在单个帖子下发一条评论。成功返回 True，不可评论或出错返回 False。"""
    # 记录当前进行到哪一步。Selenium 的 TimeoutException 消息是空的，
    # 不标注的话日志里只有一句「处理帖子时出错: Message:」，无从排查。
    stage = "打开帖子"
    try:
        driver.get(post_url)

        # 等待 CodeMirror 编辑器加载。20 秒还没出来基本就是这个帖子
        # 不让评论了，继续等只是白白拖长运行时间。
        stage = "等待评论编辑器"
        editor = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.CodeMirror'))
        )

        # 点击编辑器区域获取焦点。编辑器有时会被外层的
        # #code-mirror-editor 盖住，原生点击会被拦，退回 JS 点击。
        stage = "聚焦编辑器"
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", editor)
        time.sleep(0.3)
        try:
            editor.click()
        except Exception:
            driver.execute_script("arguments[0].click();", editor)
        time.sleep(0.5)

        input_text = random.choice(randomInputStr)

        # 模拟输入
        stage = "输入评论内容"
        actions = ActionChains(driver)
        for char in input_text:
            actions.send_keys(char)
            actions.pause(random.uniform(0.1, 0.3))
        actions.perform()

        # 等待一下确保内容已经输入
        time.sleep(2)

        # 使用更精确的选择器定位提交按钮
        stage = "等待发布按钮"
        submit_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'submit') and contains(@class, 'btn') and contains(text(), '发布评论')]"))
        )

        # 必须用 block:'center'。scrollIntoView(true) 会把元素对齐到
        # 视口顶端，而 NodeSeek 的 #nsk-head 是 sticky 头部，会把按钮盖住，
        # 导致 element click intercepted。
        stage = "点击发布"
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_button)
        time.sleep(0.5)
        try:
            submit_button.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_button)

        time.sleep(random.uniform(2, 4))
        print(f"   ✅ 已评论: {post_url}")
        return True

    except Exception as e:
        # TimeoutException 的 str() 往往是空的，只能靠 stage 和类型名定位
        detail = (str(e).strip().splitlines() or [""])[0]
        print(f"   ✗ 跳过 [{stage}] {post_url}")
        print(f"     {type(e).__name__}: {detail or '(无错误信息，通常是等待超时)'}")
        if stage == "等待评论编辑器":
            print("     该帖大概率不接受评论（已关闭回复或需要更高权限）")
        return False


def nodeseek_comment(driver):
    """评论若干帖子并顺带投喂鸡腿。

    分批进行：每完成 comment_batch 条就停 comment_interval 秒。连续快速刷
    评论既容易触发站点风控，也更容易被人一眼看出是机器人。
    """
    target = comment_count
    summary["comment_target"] = target
    if target <= 0:
        return 0

    try:
        # 按目标的 3 倍备候选，覆盖掉那些评论不了的帖子
        urls = collect_candidate_posts(driver, max(target * 3, 20))
        if not urls:
            print("没有拿到任何候选帖子")
            return 0
        print(f"共 {len(urls)} 个候选帖子，目标评论 {target} 条，"
              f"每 {comment_batch} 条休息 {comment_interval} 秒")

        done = 0
        chicken_done = 0
        chicken_tries = 0
        last_pause_at = 0

        for post_url in urls:
            if done >= target:
                break

            # 每满一批就歇一会儿。按成功数分批，失败跳过的不计入。
            if done > 0 and done % comment_batch == 0 and last_pause_at != done:
                last_pause_at = done
                remaining = target - done
                print(f"── 已完成 {done}/{target} 条，休息 {comment_interval} 秒"
                      f"（还剩 {remaining} 条）──")
                time.sleep(comment_interval)

            print(f"[{done + 1}/{target}] 尝试 {post_url}")
            driver.get(post_url)

            # 顺带投喂鸡腿。站点每天有免费额度（当前是 2 次），
            # 原来只投一次，白白浪费剩下的额度。
            if chicken_done < chicken_count and chicken_tries < chicken_count * 4:
                chicken_tries += 1
                if click_chicken_leg(driver):
                    chicken_done += 1
                    summary["chicken"] = True
                    print(f"   已投喂 {chicken_done}/{chicken_count}")

            if comment_one_post(driver, post_url):
                done += 1

        summary["comment_done"] = done
        summary["chicken_done"] = chicken_done
        print(f"NodeSeek评论任务完成，成功 {done}/{target} 条，投喂 {chicken_done}/{chicken_count} 次")
        if done < target:
            print(f"注意：候选帖子用完了，只完成 {done} 条。可以调大候选范围或降低目标")
        return done

    except Exception as e:
        print(f"NodeSeek评论出错: {str(e)}")
        print("详细错误信息:")
        print(traceback.format_exc())
        summary["comment_done"] = summary.get("comment_done", 0)
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
    summary["cookie_days"] = days
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
        if signed is not True or debug_mode:
            save_debug_artifacts(driver, "sign")

        # 只有签到成功才评论。签到失败会以非 0 退出触发 CI 重试，
        # 若此处照常评论，重试一次就把评论重复发一轮。
        if signed is not True:
            print("签到未成功，跳过评论环节（避免重试时重复评论）")
        elif comment_count > 0:
            nodeseek_comment(driver)
        else:
            print("NS_COMMENT_COUNT=0，跳过评论环节")

        report_cookie_status(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # 通知策略：成功、以及凭证失效（不会再重试）都立即推送；普通失败只在
    # CI 的最后一次尝试才推，否则重试 3 次会连发 3 条。
    if signed is True or signed is CREDENTIAL_INVALID or env_bool("NS_NOTIFY_FAILURE"):
        send_summary_notification()
    else:
        print("本次失败还会重试，暂不推送通知")

    print("脚本执行完成")
    if signed is CREDENTIAL_INVALID:
        # 退出码 2：凭证失效，CI 会跳过剩余重试
        exit(2)
    # 签到失败时以非 0 退出，让 CI 的重试机制生效。
    exit(0 if signed else 1)

