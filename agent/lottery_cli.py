# -*- coding: utf-8 -*-
"""彩票小软件 agent 版：命令行工具（lottery_cli.py）。

给智能体（agent）用的版本：没有界面，一条命令跑完就退，不常驻后台。
智能体靠自己的定时任务到点调它，定时任务由智能体平台管理，重启了还在；
进程每次都是新的，不怕被杀后台。

配置和界面版通用同一套文件（settings.json / tickets.json），两边可以混用：
界面版里加的号，命令行版一样认；命令行版里加的号，界面版一样认。

只用 Python 标准库。时间一律北京时间（固定 UTC+8，不用 ZoneInfo）。

用法：
  python lottery_cli.py check                        # 抓最新一期、核对、推送（定时任务调这个）
  python lottery_cli.py remind                       # 推送到点的购彩提醒（定时任务调这个）
  python lottery_cli.py test --key-name 我的手机     # 给指定密钥发一条测试推送（配好后必须跑一次验货）
  python lottery_cli.py add-key --name 我的手机 --key <Bark key>
  python lottery_cli.py add-ticket --game 双色球 --main 09,15,18,25,26,28 --back 09 --key-name 我的手机
  python lottery_cli.py add-reminder --text 记得买彩票 --key-name 我的手机 --weekdays 0,2,5 --time 18:00
  python lottery_cli.py list                         # 列出密钥（打码）和彩票
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_FULLWIDTH_MAP = {}
for _c in range(0xFF01, 0xFF5F):
    _FULLWIDTH_MAP[chr(_c)] = chr(_c - 0xFEE0)
_FULLWIDTH_MAP["　"] = " "
_FULLWIDTH_MAP["、"] = ","
_FULLWIDTH_MAP["，"] = ","



CERT_HUMAN = "证书验证失败，消息没发出去，先检查电脑时间是否准确、网络是否拦截https，密钥对不对要等这一步过了才能知道"

def is_cert_error(exc):
    text = str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    low = text.lower()
    return "certificate verify failed" in low or "certificate_verify_failed" in low or "unable to get issuer certificate" in low

BJ = timezone(timedelta(hours=8))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TICKETS_FILE = os.path.join(BASE_DIR, "tickets.json")

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

PENDING_FILE = os.path.join(BASE_DIR, "pending_tasks.json")

LOG_FILE = os.path.join(BASE_DIR, "彩票小软件日志.txt")

GAMES = {
    "双色球": {"main_count": 6, "main_lo": 1, "main_hi": 33,
               "back_count": 1, "back_lo": 1, "back_hi": 16,
               "main_name": "红球", "back_name": "蓝球", "org": "福彩",
               "draw_weekdays": [1, 3, 6]},  # 周二、四、日，Python weekday 周一=0
    "大乐透": {"main_count": 5, "main_lo": 1, "main_hi": 35,
               "back_count": 2, "back_lo": 1, "back_hi": 12,
               "main_name": "前区", "back_name": "后区", "org": "体彩",
               "draw_weekdays": [0, 2, 5]},  # 周一、三、六
}

PUSH_TIME_OPTIONS = ["开奖后抓到就推", "21:30", "22:00", "22:30", "23:00", "自定义"]

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

MONITOR_LABELS = {"always": "一直守着", "count": "只对x期"}

MODE_LABELS = {"check": "核对模式", "only": "仅推开奖号模式"}

MONITOR_FROM_LABEL = {v: k for k, v in MONITOR_LABELS.items()}

MODE_FROM_LABEL = {v: k for k, v in MODE_LABELS.items()}

def migrate_ticket_to_count(t):
    """把旧“只对一期”票迁成“只对x期”票；新票和已迁票原样规范化。"""
    if t.get("monitor") == "one":
        pushed = [str(x) for x in (t.get("pushed") or [])]
        issue = str(t.get("issue", ""))
        done = bool(issue and issue in pushed)
        t["monitor"] = "count"
        t["count"] = 1
        t["remaining"] = 0 if done else 1
        if done:
            t["status"] = "stopped"
        t.pop("issue", None)
    elif t.get("monitor") == "count":
        try:
            count = int(t.get("count") or 1)
        except Exception:
            count = 1
        count = max(1, min(10, count))
        t["count"] = count
        if t.get("remaining") is None:
            t["remaining"] = count
        else:
            try:
                remaining = int(t["remaining"])
            except Exception:
                remaining = count
            t["remaining"] = max(0, min(count, remaining))
    return t


def migrate_keys_push_time(settings):
    """已有密钥没有推送时间时，用旧全局推送时间作默认值迁移。"""
    fallback = settings.get("push_time") or "开奖后抓到就推"
    for key in settings.get("bark_keys", []):
        if not key.get("push_time"):
            key["push_time"] = fallback
    return settings

def now_bj():
    return datetime.now(BJ)

def fmt_time(dt=None):
    return (dt or now_bj()).strftime("%Y-%m-%d %H:%M:%S")

def fmt_nums(nums):
    return " ".join("%02d" % int(x) for x in nums)

def normalize_text(text):
    if text is None:
        return ""
    out = []
    for ch in str(text):
        out.append(_FULLWIDTH_MAP.get(ch, ch))
    return "".join(out)

def parse_number_list(text, count, lo, hi, label):
    """返回排序后的 int 列表；填错时 raise ValueError，指明哪一项错了。"""
    norm = normalize_text(text)
    parts = [p.strip() for p in re.split(r"[,\s;；]+", norm) if p.strip() != ""]
    if len(parts) != count:
        raise ValueError("%s应填 %d 个号码，你填了 %d 个：%s" % (label, count, len(parts), text))
    nums = []
    for p in parts:
        if not re.fullmatch(r"\d+", p):
            raise ValueError("%s里有不是数字的内容：%s，请只填数字" % (label, p))
        v = int(p)
        if v < lo or v > hi:
            raise ValueError("%s里的 %d 超出范围，应在 %d-%d 之间" % (label, v, lo, hi))
        nums.append(v)
    if len(set(nums)) != len(nums):
        raise ValueError("%s里有重复号码：%s" % (label, text))
    return sorted(nums)

def validate_ticket(game, main_text, back_text):
    cfg = GAMES[game]
    main = parse_number_list(main_text, cfg["main_count"], cfg["main_lo"], cfg["main_hi"], cfg["main_name"])
    back = parse_number_list(back_text, cfg["back_count"], cfg["back_lo"], cfg["back_hi"], cfg["back_name"])
    return main, back

def check_ssq(ticket_main, ticket_back, draw_main, draw_back):
    rm = len(set(ticket_main) & set(draw_main))
    bm = 1 if ticket_back and draw_back and ticket_back[0] == draw_back[0] else 0
    tier = None
    if rm == 6 and bm == 1:
        tier = "一等奖"
    elif rm == 6 and bm == 0:
        tier = "二等奖"
    elif rm == 5 and bm == 1:
        tier = "三等奖"
    elif (rm == 5 and bm == 0) or (rm == 4 and bm == 1):
        tier = "四等奖"
    elif (rm == 4 and bm == 0) or (rm == 3 and bm == 1):
        tier = "五等奖"
    elif (rm == 2 and bm == 1) or (rm == 1 and bm == 1) or (rm == 0 and bm == 1):
        tier = "六等奖"
    return tier, rm, bm

def check_dlt(ticket_main, ticket_back, draw_main, draw_back):
    fm = len(set(ticket_main) & set(draw_main))
    bm = len(set(ticket_back) & set(draw_back))
    tier = None
    if fm == 5 and bm == 2:
        tier = "一等奖"
    elif fm == 5 and bm == 1:
        tier = "二等奖"
    elif (fm == 5 and bm == 0) or (fm == 4 and bm == 2):
        tier = "三等奖"
    elif fm == 4 and bm == 1:
        tier = "四等奖"
    elif (fm == 4 and bm == 0) or (fm == 3 and bm == 2):
        tier = "五等奖"
    elif (fm == 3 and bm == 1) or (fm == 2 and bm == 2):
        tier = "六等奖"
    elif (fm == 3 and bm == 0) or (fm == 2 and bm == 1) or (fm == 1 and bm == 2) or (fm == 0 and bm == 2):
        tier = "七等奖"
    return tier, fm, bm

def check_ticket(game, ticket, draw):
    if game == "双色球":
        return check_ssq(ticket["main"], ticket["back"], draw["main"], draw["back"])
    return check_dlt(ticket["main"], ticket["back"], draw["main"], draw["back"])

def draw_text(game, draw):
    cfg = GAMES[game]
    return "%s%s，%s%s" % (cfg["main_name"], fmt_nums(draw["main"]),
                           cfg["back_name"], fmt_nums(draw["back"]))

def draw_when_label(draw_date):
    """开奖日期 YYYY-MM-DD -> '11月15日（周日）'；没有日期返回 ''。
    推送语里带上它，用户一眼就知道这是哪一天开的奖，不会把周一的当成周三的。"""
    if not draw_date:
        return ""
    try:
        dt = datetime.strptime(draw_date, "%Y-%m-%d")
    except Exception:
        return ""
    return "%d月%d日（%s）" % (dt.month, dt.day, WEEKDAY_NAMES[dt.weekday()])

def draw_prefix(game, issue, draw_date):
    """推送标题/正文共用的前缀：'11月15日（周日）福彩双色球第2026128期'；没日期时不带日期。"""
    org = GAMES[game].get("org", "")
    when = draw_when_label(draw_date)
    if when:
        return "%s%s%s第%s期" % (when, org, game, issue)
    return "%s%s第%s期" % (org, game, issue)

def build_check_texts(game, issue, draw_str, results, draw_date=None):
    """results: 按组内顺序的 (tier, ...) 列表。返回 (文本, 是否有中奖)。
    v1.7 用户定的趣味文案：没中就俩字，中了就喊三遍。
    v1.9 推送语加详细说明：前面带上开奖日期+周几+体/福彩，说明这是哪一期的结果。"""
    prefix = draw_prefix(game, issue, draw_date)
    winners = [r[0] for r in results if r[0]]
    if not winners:
        return "%s：没中。开奖号：%s。" % (prefix, draw_str), False
    return "%s：中奖了中奖了中奖了。" % prefix, True

def build_only_draw_text(game, issue, draw_str, draw_date=None):
    prefix = draw_prefix(game, issue, draw_date)
    return "%s开奖号是：%s。" % (prefix, draw_str)

def _norm_draw_date(value):
    """把官方接口里各种写法的开奖日期归一成 YYYY-MM-DD；没有或认不出返回 None。
    接受 '2026-11-07'、'2026-11-07 21:15:00'、'2026/11/07' 等。"""
    if not value:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(value))
    if not m:
        return None
    return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))

def _item_draw_date(it, *keys):
    """从一条开奖记录里按顺序找日期字段，哪个有就用哪个；都没有返回 None。"""
    for k in keys:
        d = _norm_draw_date(it.get(k))
        if d:
            return d
    return None

def parse_cwl_ssq(data):
    """福彩官网 findDrawNotice 返回示例的解析。返回 (期号, main, back, 开奖日期或None) 或 None。"""
    try:
        items = None
        if isinstance(data, dict):
            items = data.get("result") or data.get("data") or data.get("list")
            if isinstance(items, dict):
                items = items.get("result") or items.get("list")
        if not items:
            return None
        it = items[0]
        issue = str(it.get("code") or it.get("issue") or it.get("lotteryDrawNum") or "")
        red_raw = it.get("red") or it.get("redBall") or ""
        blue_raw = it.get("blue") or it.get("blueBall") or ""
        if isinstance(red_raw, (list, tuple)):
            main = [int(x) for x in red_raw]
        else:
            main = [int(x) for x in re.split(r"[,\s]+", str(red_raw)) if x.strip()]
        if isinstance(blue_raw, (list, tuple)):
            back = [int(x) for x in blue_raw]
        else:
            back = [int(x) for x in re.split(r"[,\s]+", str(blue_raw)) if x.strip()]
        if len(main) != 6 or len(back) != 1 or not issue:
            return None
        return issue, sorted(main), sorted(back), _item_draw_date(
            it, "date", "lotteryDrawDate", "drawDate", "lotteryDrawTime", "openDate")
    except Exception:
        return None

def parse_sporttery_dlt(data):
    """体彩 webapi getHistoryPageListV1 返回示例的解析。返回 (期号, main, back, 开奖日期或None) 或 None。"""
    try:
        items = None
        if isinstance(data, dict):
            v = data.get("value") or {}
            items = v.get("list") or data.get("list")
        if not items:
            return None
        it = items[0]
        issue = str(it.get("lotteryDrawNum") or it.get("issue") or it.get("code") or "")
        raw = it.get("lotteryDrawResult") or it.get("result") or ""
        if isinstance(raw, (list, tuple)):
            nums = [int(x) for x in raw]
        else:
            nums = [int(x) for x in re.split(r"[,\s]+", str(raw)) if x.strip()]
        if len(nums) != 7 or not issue:
            return None
        return issue, sorted(nums[:5]), sorted(nums[5:]), _item_draw_date(
            it, "lotteryDrawTime", "lotteryDrawDate", "date", "drawDate")
    except Exception:
        return None

def fetch_url_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LotteryApp/1.0",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)

def fetch_url_html(url, timeout=15):
    """500.com 历史页是 GBK 编码的 HTML，必须按 GBK 解，不能按 UTF-8 解。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LotteryApp/1.0",
        "Accept": "text/html, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("gbk", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")

def parse_500(html, game):
    """解析 500.com 历史页第一列到第七列：期号 + 7 个球。
    双色球：前 6 个是红球、第 7 个是蓝球；大乐透：前 5 个是前区、后 2 个是后区。
    只认号码范围和个数都对的行，期号最大的那行就是最新一期；都不对返回 None。
    每行末尾还有一列“开奖日期”（YYYY-MM-DD），一起返回；没抓到日期时返回 None，
    调用方按旧规则处理。返回 (期号, main, back, 开奖日期或None) 或 None。
    500.com 是非官方转载，程序只把它当官方源失败后的备用，见 fetch_latest_*。"""
    try:
        text = re.sub(r"<[^>]+>", " ", html or "")
        text = re.sub(r"\s+", " ", text)
        pattern = re.compile(
            r"\b(\d{5})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+"
            r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\b")
        best = None
        for m in pattern.finditer(text):
            issue = m.group(1)
            balls = [int(m.group(i)) for i in range(2, 9)]
            if game == "双色球":
                main, back = balls[:6], balls[6:]
                if len(set(main)) != 6 or any(n < 1 or n > 33 for n in main):
                    continue
                if any(n < 1 or n > 16 for n in back):
                    continue
            else:
                main, back = balls[:5], balls[5:]
                if len(set(main)) != 5 or any(n < 1 or n > 35 for n in main):
                    continue
                if len(set(back)) != 2 or any(n < 1 or n > 12 for n in back):
                    continue
            # 开奖日期在该行末尾：从这 7 个球往后找，200 字内第一个 YYYY-MM-DD；
            # 若先撞上下一行的期号（5 位数字），说明本行没日期，记 None，别张冠李戴。
            tail = text[m.end():m.end() + 200]
            date_m = re.search(r"\d{4}-\d{2}-\d{2}", tail)
            issue_m = re.search(r"\b\d{5}\b", tail)
            draw_date = None
            if date_m and (issue_m is None or date_m.start() < issue_m.start()):
                draw_date = date_m.group(0)
            if best is None or issue > best[0]:
                best = (issue, sorted(main), sorted(back), draw_date)
        if best is None:
            return None
        return best[0], best[1], best[2], best[3]
    except Exception:
        return None

def fetch_latest_ssq():
    """主源福彩官网，备用源同站另一参数；官方都失败才换 500.com 备用；都失败则 raise。"""
    urls = [
        "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice?name=ssq&pageNo=1&pageSize=1",
        "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice?name=ssq&issueCount=1&pageNo=1&pageSize=1",
    ]
    last_err = None
    for i, url in enumerate(urls):
        try:
            data = fetch_url_json(url)
            parsed = parse_cwl_ssq(data)
            if parsed:
                return parsed, ("福彩官网" if i == 0 else "福彩官网备用")
            last_err = "解析不到开奖号"
        except Exception as e:
            last_err = str(e)
    try:
        parsed = parse_500(
            fetch_url_html("https://datachart.500.com/ssq/history/history.shtml"),
            "双色球")
        if parsed:
            return parsed, "500.com备用"
        last_err = "500.com备用解析不到开奖号"
    except Exception as e:
        last_err = str(e)
    raise RuntimeError("双色球抓取失败：%s" % last_err)

def fetch_latest_dlt():
    urls = [
        "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=85&provinceId=0&pageSize=1&pageNo=1",
        "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry?gameNo=85&provinceId=0&pageSize=5&pageNo=1",
    ]
    last_err = None
    for i, url in enumerate(urls):
        try:
            data = fetch_url_json(url)
            parsed = parse_sporttery_dlt(data)
            if parsed:
                return parsed, ("体彩官网" if i == 0 else "体彩官网备用")
            last_err = "解析不到开奖号"
        except Exception as e:
            last_err = str(e)
    try:
        parsed = parse_500(
            fetch_url_html("https://datachart.500.com/dlt/history/history.shtml"),
            "大乐透")
        if parsed:
            return parsed, "500.com备用"
        last_err = "500.com备用解析不到开奖号"
    except Exception as e:
        last_err = str(e)
    raise RuntimeError("大乐透抓取失败：%s" % last_err)

def fetch_latest(game):
    if game == "双色球":
        return fetch_latest_ssq()
    return fetch_latest_dlt()

def _bark_check_response(text):
    """Bark 返回 JSON 时 code 须为 200/"200"/None，否则抛返回异常；非 JSON 按成功处理。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if data.get("code") not in (200, "200", None):
        raise ValueError(f"Bark 返回异常：{text[:200]}")

def _bark_get(key, title, body, timeout=10, context=None):
    title_e = urllib.parse.quote(title, safe="")
    body_e = urllib.parse.quote(body, safe="")
    url = f"https://api.day.app/{key}/{title_e}/{body_e}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    _bark_check_response(text)
    return True

def _bark_post(key, title, body, timeout=10, context=None):
    payload = json.dumps({"device_key": key, "title": title, "body": body}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.day.app/push", data=payload,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    _bark_check_response(text)
    return True

def _bark_unwrap(exc):
    if isinstance(exc, urllib.error.URLError) and getattr(exc, "reason", None) is not None:
        return exc.reason
    return exc

def bark_push(key, title, body, timeout=10):
    """Bark 推送。第一招 GET 跟股价预警器 v5.4 的 send_bark 一模一样；只有当 GET
    抛证书验证失败或 Bark 返回异常时，才自动再试 POST JSON；GET/POST 都失败后，
    若本机装了 certifi，再用其 cacert 试一次 GET、再试一次 POST。

    成功时 bark_push.last_method 为 "GET"/"POST"/"certifi-GET"/"certifi-POST"，
    bark_push.last_errors 记录各招的原始错误（不含 key）；全部失败时抛原始 GET 错误。
    key 为空返回 False（不算失败，只是没填）。成功返回 True，失败抛异常。证书验证绝不关闭。
    """
    bark_push.last_method = ""
    bark_push.last_errors = {}
    if key is None:
        return False
    key = str(key).strip()
    key = unicodedata.normalize("NFKC", key)
    key = key.strip()
    if not key:
        return False
    if re.search(r"\s", key):
        raise ValueError("Bark key 里有空格，请检查是不是多复制了空格")
    # 第一招：GET（跟 5.4 一样，普通默认上下文）
    try:
        _bark_get(key, title, body, timeout=timeout)
        bark_push.last_method = "GET"
        return True
    except Exception as get_err:
        bark_push.last_errors["GET"] = str(_bark_unwrap(get_err))
        cause = _bark_unwrap(get_err)
        trigger_post = is_cert_error(cause) or "Bark 返回异常" in str(get_err)
        if not trigger_post:
            raise
        # 第二招：POST JSON（普通默认上下文，key 在请求体里，不在网址里）
        try:
            _bark_post(key, title, body, timeout=timeout)
            bark_push.last_method = "POST"
            return True
        except Exception as post_err:
            bark_push.last_errors["POST"] = str(_bark_unwrap(post_err))
        # 第三招：certifi 的 cacert（只在装了 certifi 时才试，装不上就跳过、不报错）
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception as certifi_err:
            bark_push.last_errors["certifi"] = str(certifi_err)
            raise get_err
        try:
            _bark_get(key, title, body, timeout=timeout, context=ctx)
            bark_push.last_method = "certifi-GET"
            return True
        except Exception as certifi_get_err:
            bark_push.last_errors["certifi-GET"] = str(_bark_unwrap(certifi_get_err))
        try:
            _bark_post(key, title, body, timeout=timeout, context=ctx)
            bark_push.last_method = "certifi-POST"
            return True
        except Exception as certifi_post_err:
            bark_push.last_errors["certifi-POST"] = str(_bark_unwrap(certifi_post_err))
        # 全部失败：抛原始 GET 错误，不要把 POST 的错误覆盖掉它
        raise get_err

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

bark_push.last_method = ""
bark_push.last_errors = {}


# ---------- 命令行版专用 ----------
def mask_key(key):
    if not key:
        return ""
    if len(key) <= 6:
        return "***"
    return key[:3] + "***" + key[-3:]


def log(msg):
    line = "%s %s" % (fmt_time(), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_settings():
    s = load_json(SETTINGS_FILE, {"bark_keys": [], "push_time": "开奖后抓到就推"})
    if "bark_keys" not in s:
        s["bark_keys"] = []
    if "buy_reminders" not in s:
        s["buy_reminders"] = []
    migrate_keys_push_time(s)
    return s


def load_tickets():
    return [migrate_ticket_to_count(t) for t in load_json(TICKETS_FILE, [])]


def find_key(settings, key_name=None, key_id=None):
    for k in settings.get("bark_keys", []):
        if key_name is not None and k["name"] == key_name:
            return k
        if key_id is not None and k["id"] == key_id:
            return k
    return None


def do_push(key, key_name, title, text, what):
    try:
        ok = bark_push(key, title, text)
        method = getattr(bark_push, "last_method", "")
        log("Bark 推送：%s，推送到=%s，结果=%s%s，文本=%s。" % (what, key_name, "成功" if ok else "失败", ("（%s通了）" % method) if method else "", text))
        return ok
    except Exception as e:
        cause = e.reason if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None) is not None else e
        if is_cert_error(cause):
            log("Bark 推送失败：%s，推送到=%s，%s。" % (what, key_name, CERT_HUMAN))
        else:
            log("Bark 推送失败：%s，推送到=%s，原因：%s。" % (what, key_name, cause))
        return False


def cmd_check(args):
    """抓最新一期、核对、推送。推过的期号不重复推；只推给监控中的票。"""
    settings = load_settings()
    tickets = load_tickets()
    date_str = now_bj().strftime("%Y-%m-%d")
    pushed_any = False
    for game in GAMES:
        try:
            (issue, main, back, draw_date), source = fetch_latest(game)
        except Exception as e:
            log("抓取失败：%s，原因：%s。" % (game, e))
            continue
        # 日期校验：有开奖日期且不是今天，就是上一期——但命令行版是“打开/到点就查”，
        # 上一期没推过也要推（用户可能几天没跑），所以这里不拦，只记日志说明。
        if draw_date and draw_date != date_str:
            log("抓取：%s第%s期开奖日期是%s，不是今天（%s），按上一期处理，没推过就推。" % (game, issue, draw_date, date_str))
        draw = {"main": main, "back": back}
        dstr = draw_text(game, draw)
        log("抓取：%s第%s期%s，开奖号：%s，来源：%s。" % (game, issue, "（开奖日期%s）" % draw_date if draw_date else "", dstr, source))
        groups = {}
        order = []
        for t in tickets:
            if t["game"] != game or t["status"] != "active":
                continue
            if t["monitor"] == "count" and t.get("remaining") is not None and int(t["remaining"]) <= 0:
                t["status"] = "stopped"
                continue
            if issue in t.get("pushed", []):
                continue
            if t["key_id"] not in groups:
                groups[t["key_id"]] = []
                order.append(t["key_id"])
            groups[t["key_id"]].append(t)
        if not groups:
            log("核对：%s第%s期，没有监控中的票需要推送（可能已推过）。" % (game, issue))
            continue
        for key_id in order:
            tickets_g = groups[key_id]
            k = find_key(settings, key_id=key_id)
            if not k:
                log("推送跳过：%s第%s期，密钥已不在，%d 注未推。" % (game, issue, len(tickets_g)))
                continue
            key_name = k["name"]
            check_tickets = [t for t in tickets_g if t["mode"] == "check"]
            only_tickets = [t for t in tickets_g if t["mode"] == "only"]
            if check_tickets:
                results = []
                for t in check_tickets:
                    tier, _a, _b = check_ticket(game, t, draw)
                    results.append((tier,))
                    t["last_result"] = "%s期：%s" % (issue, tier if tier else "没中")
                    if t["monitor"] == "count":
                        remaining = max(0, int(t.get("remaining") if t.get("remaining") is not None else (t.get("count") or 1)) - 1)
                        t["remaining"] = remaining
                        if remaining == 0:
                            t["status"] = "stopped"
                    t.setdefault("pushed", []).append(issue)
                    log("核对结果：%s %s + %s，%s期，%s。" % (game, fmt_nums(t["main"]), fmt_nums(t["back"]), issue, tier if tier else "没中"))
                text, _win = build_check_texts(game, issue, dstr, results, draw_date)
                title = "彩票开奖：%s" % draw_prefix(game, issue, draw_date)
                do_push(k["key"], key_name, title, text, "%s第%s期" % (game, issue))
                pushed_any = True
            if only_tickets:
                for t in only_tickets:
                    t["last_result"] = "%s期：已推开奖号" % issue
                    if t["monitor"] == "count":
                        remaining = max(0, int(t.get("remaining") if t.get("remaining") is not None else (t.get("count") or 1)) - 1)
                        t["remaining"] = remaining
                        if remaining == 0:
                            t["status"] = "stopped"
                    t.setdefault("pushed", []).append(issue)
                text = build_only_draw_text(game, issue, dstr, draw_date)
                title = "彩票开奖：%s" % draw_prefix(game, issue, draw_date)
                do_push(k["key"], key_name, title, text, "%s第%s期" % (game, issue))
                pushed_any = True
    save_json(TICKETS_FILE, tickets)
    save_json(SETTINGS_FILE, settings)
    if not pushed_any:
        log("本次没有新推送。")
    return 0


def cmd_remind(args):
    """推送到点的购彩提醒：周几对上、时间对上、开关开着、今天没推过，才推。"""
    settings = load_settings()
    now = now_bj()
    date_str = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    for r in settings.get("buy_reminders", []):
        if not r.get("enabled", True):
            continue
        if now.weekday() not in (r.get("weekdays") or []):
            continue
        # 定时任务每 5 分钟跑一次，不一定正好踩在设定分钟上，所以用 5 分钟窗口：
        # 现在处于 [设定点, 设定点+5分钟) 内、且今天没推过，就推。
        try:
            rh, rm = r.get("time", "").split(":")
            rem_min = int(rh) * 60 + int(rm)
        except Exception:
            continue
        if not (0 <= now_min - rem_min < 5):
            continue
        if r.get("last_pushed") == date_str:
            continue
        k = find_key(settings, key_id=r.get("key_id", ""))
        if not k:
            log("购彩提醒跳过：%s，密钥已不在。" % r.get("text", ""))
            continue
        r["last_pushed"] = date_str
        do_push(k["key"], k["name"], "购彩提醒", r.get("text", ""), "购彩提醒")
    save_json(SETTINGS_FILE, settings)
    return 0


def cmd_test(args):
    settings = load_settings()
    k = find_key(settings, key_name=args.key_name)
    if not k:
        print("没有找到叫“%s”的密钥，先用 add-key 加一个。" % args.key_name)
        return 1
    ok = do_push(k["key"], k["name"], "彩票小软件测试", "Bark密钥测试成功，密钥名称：%s" % k["name"], "测试推送")
    if ok:
        print("测试推送已发出，手机应该收到一条。如果没收到，检查密钥对不对、手机 Bark 装好没。")
        return 0
    print("测试推送失败，看上面日志里的原因。")
    return 1


def cmd_add_key(args):
    settings = load_settings()
    if any(k["name"] == args.name for k in settings["bark_keys"]):
        print("已经有叫“%s”的密钥了，换个名称。" % args.name)
        return 1
    settings["bark_keys"].append({"id": str(int(time.time() * 1000)), "name": args.name,
                                  "key": args.key, "push_time": "开奖后抓到就推"})
    save_json(SETTINGS_FILE, settings)
    print("密钥“%s”已保存。下一步必须跑一次 test 验货：手机收到测试推送，才算链路真的通了。" % args.name)
    return 0


def cmd_add_ticket(args):
    if args.game not in GAMES:
        print("彩种只能是双色球或大乐透。")
        return 1
    settings = load_settings()
    k = find_key(settings, key_name=args.key_name)
    if not k:
        print("没有找到叫“%s”的密钥，先用 add-key 加一个。" % args.key_name)
        return 1
    try:
        main, back = validate_ticket(args.game, args.main, args.back)
    except ValueError as e:
        print("号码填错了：%s" % e)
        return 1
    tickets = load_tickets()
    monitor = args.monitor
    count = max(1, min(10, args.count))
    tickets.append({"id": int(time.time() * 1000), "game": args.game, "main": main, "back": back,
                    "monitor": monitor, "count": count if monitor == "count" else 0,
                    "remaining": count if monitor == "count" else None,
                    "mode": args.mode, "key_id": k["id"], "status": "active",
                    "last_result": "", "pushed": []})
    save_json(TICKETS_FILE, tickets)
    print("已加：%s %s + %s，推送到=%s。" % (args.game, fmt_nums(main), fmt_nums(back), args.key_name))
    return 0


def cmd_add_reminder(args):
    settings = load_settings()
    k = find_key(settings, key_name=args.key_name)
    if not k:
        print("没有找到叫“%s”的密钥，先用 add-key 加一个。" % args.key_name)
        return 1
    try:
        weekdays = sorted(int(x) for x in args.weekdays.split(",") if x.strip() != "")
    except ValueError:
        print("weekdays 填法：0=周一 … 6=周日，逗号隔开，比如 0,2,5。")
        return 1
    if not weekdays or any(d < 0 or d > 6 for d in weekdays):
        print("weekdays 至少填一天，0=周一 … 6=周日。")
        return 1
    if not re.fullmatch(r"\d{2}:\d{2}", args.time or ""):
        print("时间填法：HH:MM，比如 18:00。")
        return 1
    settings["buy_reminders"].append({"id": int(time.time() * 1000), "text": args.text,
                                      "key_id": k["id"], "weekdays": weekdays,
                                      "time": args.time, "enabled": True})
    save_json(SETTINGS_FILE, settings)
    wd_txt = "、".join(WEEKDAY_NAMES[d] for d in weekdays)
    print("已加购彩提醒：%s，推送到=%s，%s %s。" % (args.text, args.key_name, wd_txt, args.time))
    return 0


def cmd_list(args):
    settings = load_settings()
    tickets = load_tickets()
    print("== 密钥（半暗文） ==")
    if not settings["bark_keys"]:
        print("还没有密钥。")
    for k in settings["bark_keys"]:
        print("- %s：%s" % (k["name"], mask_key(k["key"])))
    print("== 彩票 ==")
    if not tickets:
        print("还没有彩票。")
    for t in tickets:
        cfg = GAMES[t["game"]]
        k = find_key(settings, key_id=t["key_id"])
        print("- %s %s + %s，%s，推送到=%s，%s，上次：%s" % (
            t["game"], fmt_nums(t["main"]), fmt_nums(t["back"]),
            "一直守着" if t["monitor"] == "always" else "只对%d期（还剩%d次）" % (t.get("count") or 1, t.get("remaining") or 0),
            k["name"] if k else "（密钥已不在）",
            "监控中" if t["status"] == "active" else "已停止",
            t.get("last_result", "")))
    print("== 购彩提醒 ==")
    rems = settings.get("buy_reminders", [])
    if not rems:
        print("还没有提醒。")
    for r in rems:
        k = find_key(settings, key_id=r.get("key_id", ""))
        wd_txt = "、".join(WEEKDAY_NAMES[d] for d in sorted(r.get("weekdays", [])))
        print("- %s，推送到=%s，%s %s，开关=%s" % (r.get("text", ""), k["name"] if k else "（密钥已不在）",
                                                wd_txt, r.get("time", ""), "开" if r.get("enabled", True) else "关"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="lottery_cli.py", description="彩票小软件 agent 版：抓取、核对、推送，一条命令跑完就退。")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="抓最新一期、核对、推送（定时任务调这个）")
    sub.add_parser("remind", help="推送到点的购彩提醒（定时任务调这个）")
    t = sub.add_parser("test", help="给指定密钥发一条测试推送")
    t.add_argument("--key-name", required=True, help="密钥名称")
    a = sub.add_parser("add-key", help="添加一个 Bark 密钥")
    a.add_argument("--name", required=True, help="名称，比如：我的手机")
    a.add_argument("--key", required=True, help="Bark key")
    at = sub.add_parser("add-ticket", help="添加一注彩票")
    at.add_argument("--game", required=True, choices=["双色球", "大乐透"])
    at.add_argument("--main", required=True, help="主号，逗号隔开，比如 09,15,18,25,26,28")
    at.add_argument("--back", required=True, help="后号，逗号隔开，比如 09")
    at.add_argument("--key-name", required=True, help="推送到哪个密钥")
    at.add_argument("--mode", default="check", choices=["check", "only"], help="check=核对模式，only=仅推开奖号")
    at.add_argument("--monitor", default="always", choices=["always", "count"], help="always=一直守着，count=只对x期")
    at.add_argument("--count", type=int, default=1, help="只对x期时的期数（1-10）")
    ar = sub.add_parser("add-reminder", help="添加一条购彩提醒")
    ar.add_argument("--text", required=True, help="提醒文字")
    ar.add_argument("--key-name", required=True, help="推送到哪个密钥")
    ar.add_argument("--weekdays", required=True, help="周几，0=周一…6=周日，逗号隔开，比如 0,2,5")
    ar.add_argument("--time", required=True, help="准确时间点，HH:MM，比如 18:00")
    sub.add_parser("list", help="列出密钥（打码）和彩票")
    args = p.parse_args(argv)
    cmds = {"check": cmd_check, "remind": cmd_remind, "test": cmd_test,
            "add-key": cmd_add_key, "add-ticket": cmd_add_ticket,
            "add-reminder": cmd_add_reminder, "list": cmd_list}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
