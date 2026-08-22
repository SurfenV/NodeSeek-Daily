#!/usr/bin/env python3
"""把浏览器导出的 Cookie 更新到 GitHub Secret NS_COOKIE。

NodeSeek 的登录态 30 天到期且不会滑动续期，所以每月需要跑一次这个脚本。

用法：
    # 从浏览器扩展（EditThisCookie / Cookie-Editor）导出 JSON 后
    python3 scripts/update_cookie.py cookies.json

    # 或直接粘贴 DevTools 里的 Cookie 请求头
    python3 scripts/update_cookie.py --raw "session=xxx; pjwt=yyy; smac=zzz"

    # 只想看看解析结果，不写入
    python3 scripts/update_cookie.py cookies.json --dry-run
"""
import argparse
import base64
import json
import subprocess
import sys
import time

REPO = "SurfenV/NodeSeek-Daily"
REQUIRED = ("session", "pjwt", "smac")
SESSION_TTL_DAYS = 30


def parse(path, raw):
    if raw:
        pairs = []
        for item in raw.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                pairs.append((name.strip(), value.strip()))
        return pairs

    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    text = text.strip()

    if text.startswith("["):
        return [(c["name"], c["value"]) for c in json.loads(text)]
    if text.startswith("{"):
        return list(json.loads(text).items())
    # 当成原始 Cookie 头处理
    return parse(None, text)


def expiry_of(pairs):
    """从 pjwt 的 payload 推算登录态到期时间。"""
    for name, value in pairs:
        if name != "pjwt":
            continue
        payload = value.split(".")[0]
        payload += "=" * (-len(payload) % 4)
        issued = json.loads(base64.urlsafe_b64decode(payload)).get("ts")
        if issued:
            return issued + SESSION_TTL_DAYS * 86400
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default="-",
                    help="Cookie JSON 文件路径，- 表示从 stdin 读取")
    ap.add_argument("--raw", help="直接给出 name=value; ... 形式的 Cookie 串")
    ap.add_argument("--repo", default=REPO, help=f"目标仓库，默认 {REPO}")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写入")
    args = ap.parse_args()

    try:
        pairs = parse(args.path, args.raw)
    except Exception as e:
        sys.exit(f"解析失败: {e}")

    if not pairs:
        sys.exit("没有解析到任何 Cookie")

    names = [n for n, _ in pairs]
    missing = [k for k in REQUIRED if k not in names]
    if missing:
        sys.exit(f"缺少必需的 Cookie: {', '.join(missing)}\n"
                 f"请确认导出时处于登录状态，且没有过滤掉 httpOnly 的项")

    print(f"解析到 {len(pairs)} 个 Cookie: {', '.join(names)}")

    expiry = expiry_of(pairs)
    if expiry:
        days = (expiry - time.time()) / 86400
        stamp = time.strftime("%Y-%m-%d", time.gmtime(expiry))
        print(f"登录态到期: {stamp} UTC（{days:.1f} 天后）")
        if days < 0:
            sys.exit("这份 Cookie 已经过期了，请重新登录后再导出")
        if days < 7:
            print(f"注意：这份 Cookie 只剩 {days:.1f} 天，建议重新登录换一份新的")

    value = "; ".join(f"{n}={v}" for n, v in pairs)

    if args.dry_run:
        print(f"\n[dry-run] 未写入。Cookie 串长度 {len(value)} 字符")
        return

    try:
        subprocess.run(["gh", "secret", "set", "NS_COOKIE", "-R", args.repo],
                       input=value, text=True, check=True)
    except FileNotFoundError:
        sys.exit("找不到 gh 命令，请先安装 GitHub CLI")
    except subprocess.CalledProcessError as e:
        sys.exit(f"写入 Secret 失败: {e}")

    print(f"✅ 已更新 {args.repo} 的 NS_COOKIE")
    print("   可以跑一次验证：gh workflow run daily.yml -R " + args.repo + " -f comment_count=0")


if __name__ == "__main__":
    main()
