# -*- coding: utf-8 -*-
"""彩票小软件 v1.9（个人版）
双色球 + 大乐透，监控自己买的号，开奖后用 Bark 推送。
个人版和服务器版是同一套代码，只差一点：个人版打开软件就自动抓一次，
抓到没推过的新一期就推，适合装在自己电脑上、早上上班/下班打开看一眼就关；
服务器版靠后台轮询，不用打开也能推。
只用 Python 标准库。时间一律北京时间（固定 UTC+8，不用 ZoneInfo）。
奖级说明（不报金额，只报奖级）：
- 双色球：6+1一等、6+0二等、5+1三等、5+0或4+1四等、4+0或3+1五等、
  2+1或1+1或0+1六等，取最高一等。
- 大乐透：按 2026 年 2 月 1 日起实施的新规，9 个奖级合并为 7 个，
  13 个中奖条件不变：5+2一等、5+1二等、5+0或4+2三等、4+1四等、
  4+0或3+2五等、3+1或2+2六等、3+0或2+1或1+2或0+2七等，取最高一等。
  以官方公告为准，如官方再改规则，请以官方公告为准、并联系作者更新。
"""
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CERT_HUMAN = "证书验证失败，消息没发出去，先检查电脑时间是否准确、网络是否拦截https，密钥对不对要等这一步过了才能知道"


def is_cert_error(exc):
    text = str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    low = text.lower()
    return "certificate verify failed" in low or "certificate_verify_failed" in low or "unable to get issuer certificate" in low


try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # 供无图形环境的单元测试导入核心逻辑时使用
    tk = None
    messagebox = None
    ttk = None

BJ = timezone(timedelta(hours=8))
# PyInstaller 打包成 exe 后 __file__ 指向临时解压目录，配置会存丢；
# 冻结运行时改按 exe 所在目录存，脚本运行时行为不变。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
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


def key_push_time_display(push_time):
    return push_time or "开奖后抓到就推"


def is_custom_push_time(value):
    """判断已保存的推送时间是否为自定义的 HH:MM（不在固定选项里）。"""
    if not value or value in PUSH_TIME_OPTIONS:
        return False
    return bool(re.fullmatch(r"\d{2}:\d{2}", str(value)))


def split_custom_push_time(value):
    if is_custom_push_time(value):
        hh, mm = str(value).split(":")
        return hh, mm
    return "21", "30"


def now_bj():
    return datetime.now(BJ)


def fmt_time(dt=None):
    return (dt or now_bj()).strftime("%Y-%m-%d %H:%M:%S")


def fmt_nums(nums):
    return " ".join("%02d" % int(x) for x in nums)


# ---------- 全角归一化与号码校验 ----------
_FULLWIDTH_MAP = {}
for _c in range(0xFF01, 0xFF5F):
    _FULLWIDTH_MAP[chr(_c)] = chr(_c - 0xFEE0)
_FULLWIDTH_MAP["　"] = " "
_FULLWIDTH_MAP["、"] = ","
_FULLWIDTH_MAP["，"] = ","


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


# ---------- 小球点选（全程鼠标点击，不用打字填号） ----------
class BallPicker:
    """可点击的小球组：点一下选中、再点取消，选满后点别的号会提示已满。

    只在有图形界面时使用；核心校验仍走 parse_number_list / validate_ticket。
    """

    def __init__(self, parent, label, lo, hi, count, initial=()):
        self.lo = lo
        self.hi = hi
        self.count = count
        self.selected = set(int(x) for x in (initial or []) if lo <= int(x) <= hi)
        self.frame = ttk.Frame(parent)
        ttk.Label(self.frame, text=label).pack(anchor="w")
        self.count_label = ttk.Label(self.frame, text="")
        self.count_label.pack(anchor="w")
        btn_frame = ttk.Frame(self.frame)
        btn_frame.pack(fill="x")
        self.buttons = {}
        self.default_bg = None
        cols = 8
        for idx, num in enumerate(range(lo, hi + 1)):
            btn = tk.Button(btn_frame, text="%02d" % num, width=3,
                            command=lambda n=num: self.toggle(n))
            btn.grid(row=idx // cols, column=idx % cols, padx=2, pady=2)
            self.buttons[num] = btn
            if self.default_bg is None:
                try:
                    self.default_bg = btn.cget("bg")
                except Exception:
                    self.default_bg = ""
        self.refresh()

    def refresh(self):
        for num, btn in self.buttons.items():
            if num in self.selected:
                btn.configure(bg="#4caf50", fg="white", relief="sunken")
            else:
                btn.configure(bg=self.default_bg or "", fg="black", relief="raised")
        self.count_label.configure(text="已选 %d / %d 个" % (len(self.selected), self.count))

    def toggle(self, num):
        if num in self.selected:
            self.selected.discard(num)
        else:
            if len(self.selected) >= self.count:
                if messagebox is not None:
                    messagebox.showwarning("已选满", "已经选满 %d 个了，先点一下已选的号取消一个，再选别的号。" % self.count)
                return False
            self.selected.add(num)
        self.refresh()
        return True

    def get(self):
        return sorted(self.selected)

    def set_values(self, nums):
        self.selected = set(int(x) for x in (nums or []) if self.lo <= int(x) <= self.hi)
        self.refresh()

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)


# ---------- 核对逻辑 ----------
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


# ---------- 开奖数据解析（单元测试直接测这些函数，不依赖实时网络） ----------
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


bark_push.last_method = ""
bark_push.last_errors = {}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------- 界面 ----------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("彩票小软件（个人版）")
        self.root.geometry("980x640")
        self.tickets = load_json(TICKETS_FILE, [])
        self.settings = load_json(SETTINGS_FILE, {"bark_keys": [], "push_time": "开奖后抓到就推"})
        if "bark_keys" not in self.settings:
            self.settings["bark_keys"] = []
        if "push_time" not in self.settings:
            self.settings["push_time"] = "开奖后抓到就推"
        if "buy_reminders" not in self.settings:
            self.settings["buy_reminders"] = []
        migrate_keys_push_time(self.settings)
        self.tickets = [migrate_ticket_to_count(t) for t in self.tickets]
        self.pending = load_json(PENDING_FILE, [])
        self.processed = {}  # (game, issue) -> draw dict，已在本轮处理过，避免重复推
        # v1.6 自动抓取窗口状态（只记本轮内存，期号已推记录在 tickets.json 里，重启后也不会重复推）：
        # auto_done: (彩种, 北京时间日期) -> 当晚已抓到今日新一期，抓到就停，不再抓
        # auto_fail_notified: (彩种, 北京时间日期) -> 当晚 22:10 已推过失败提醒，不再重复推
        # auto_attempted: (彩种, 北京时间日期, 时:分) -> 本分钟已试过，避免同分钟内重复抓
        self.auto_done = set()
        self.auto_fail_notified = set()
        self.auto_attempted = set()
        self.reminder_pushed = set()  # (提醒id, 北京时间日期) -> 今天已推过，不重复推
        self.latest_draw = {}  # game -> (issue, draw, source)
        self.test_result_queue = queue.Queue()
        self.stop_flag = False
        self.build_ui()
        self.refresh_tickets()
        self.refresh_keys()
        self.refresh_reminders()
        _key_times = "、".join("%s：%s" % (k["name"], key_push_time_display(k.get("push_time"))) for k in self.settings["bark_keys"]) or "还没有密钥"
        self.log("彩票小软件已启动（北京时间）。推送时间按每个密钥走：%s。" % _key_times)
        self.log("提醒：本软件只在开着的时候推送；开奖时软件没开，那一期不补推。")
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.pending_loop, daemon=True).start()
        threading.Thread(target=self.reminder_loop, daemon=True).start()
        # 个人版：打开软件 1.5 秒后自动抓一次（后台线程，不卡界面），抓到没推过的新一期就推；
        # 推过的期号不会重复推，没开奖时抓到的是上一期、也不会乱推。
        self.log("个人版：打开后自动抓取一次，抓到没推过的新一期就推送。")
        try:
            self.root.after(1500, lambda: threading.Thread(target=self.manual_fetch, daemon=True).start())
        except Exception:
            pass
        try:
            self.root.after(500, self._poll_test_results)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _poll_test_results(self):
        # 主线程轮询：后台线程不能直接弹提示框（Tkinter 会报主线程不在主循环），
        # 所以测试结果先放进队列，由主线程在这里取出来再弹框、改状态行。
        try:
            while True:
                item = self.test_result_queue.get_nowait()
                if len(item) == 5:
                    name, ok, err, is_cert, method = item
                    self._test_key_done(name, ok, err, is_cert, method)
                else:
                    name, ok, err, is_cert = item
                    self._test_key_done(name, ok, err, is_cert)
        except queue.Empty:
            pass
        except Exception:
            pass
        if not self.stop_flag:
            try:
                self.root.after(500, self._poll_test_results)
            except Exception:
                pass

    # ----- 日志 -----
    def log(self, msg):
        line = "%s %s" % (fmt_time(), msg)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            lines = self.log_text.get("1.0", "end").splitlines()
            if len(lines) > 2000:
                self.log_text.delete("1.0", "%d.0" % (len(lines) - 2000))
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def log_file_only(self, msg):
        # 原始错误只写日志文件，不写界面面板、不弹给用户看。
        line = "%s %s" % (fmt_time(), msg)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def mask_key(self, key):
        if not key:
            return ""
        if len(key) <= 6:
            return "***"
        return key[:3] + "***" + key[-3:]

    def key_name(self, key_id):
        for k in self.settings["bark_keys"]:
            if k["id"] == key_id:
                return k["name"]
        return "（未选）"

    def key_value(self, key_id):
        for k in self.settings["bark_keys"]:
            if k["id"] == key_id:
                return k["key"]
        return ""

    def save_all(self):
        save_json(TICKETS_FILE, self.tickets)
        save_json(SETTINGS_FILE, self.settings)
        save_json(PENDING_FILE, self.pending)

    # ----- 界面搭建 -----
    def build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        tab1 = ttk.Frame(nb)
        tab2 = ttk.Frame(nb)
        tab_rem = ttk.Frame(nb)
        tab3 = ttk.Frame(nb)
        nb.add(tab1, text="全部彩票")
        nb.add(tab2, text="密钥与设置")
        nb.add(tab_rem, text="购彩提醒")
        nb.add(tab3, text="运行日志")

        # --- 全部彩票 ---
        btns = ttk.Frame(tab1)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="添加彩票", command=self.add_ticket_dialog).pack(side="left", padx=3)
        ttk.Button(btns, text="修改选中", command=self.edit_ticket_dialog).pack(side="left", padx=3)
        ttk.Button(btns, text="停止/恢复选中", command=self.toggle_ticket).pack(side="left", padx=3)
        ttk.Button(btns, text="删除选中", command=self.delete_ticket).pack(side="left", padx=3)
        ttk.Button(btns, text="手动抓取并核对最新一期", command=lambda: threading.Thread(target=self.manual_fetch, daemon=True).start()).pack(side="left", padx=3)
        ttk.Button(btns, text="手动输入开奖号核对", command=self.manual_draw_dialog).pack(side="left", padx=3)

        cols = ("game", "numbers", "monitor", "mode", "key", "status", "last")
        self.tree = ttk.Treeview(tab1, columns=cols, show="headings", height=20)
        heads = {"game": "彩种", "numbers": "号码", "monitor": "监控方式",
                 "mode": "模式", "key": "推送到哪个密钥", "status": "状态", "last": "上次结果"}
        widths = {"game": 70, "numbers": 260, "monitor": 150, "mode": 110, "key": 130, "status": 80, "last": 160}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c])
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.latest_label = ttk.Label(tab1, text="最新开奖：暂无（点“手动抓取”试一次，或等开奖日自动抓）", wraplength=950)
        self.latest_label.pack(fill="x", padx=8, pady=4)
        ttk.Label(tab1, text="说明：停止是暂停、可恢复；删除是真删掉。守号是一直守到你手动停；只对x期是跑满x次就自动停。仅推开奖号模式只推开奖号，不报中没中（给随机机打的票手动核对用）。",
                  wraplength=950).pack(fill="x", padx=8, pady=4)

        # --- 密钥与设置 ---
        ttk.Label(tab2, text="Bark 密钥管理（密钥只存在本软件同文件夹的设置文件里，日志里仍只记打码、不记完整 key）").pack(anchor="w", padx=8, pady=6)
        ttk.Label(tab2, text="密钥只存在你本机设置文件里，这里只显示前后各3位、中间打码，完整 key 不会在界面上显示出来。").pack(anchor="w", padx=8, pady=2)
        kcols = ("name", "fullkey", "push_time", "id")
        self.key_tree = ttk.Treeview(tab2, columns=kcols, show="headings", height=8)
        for c, t, w in [("name", "名称", 160), ("fullkey", "密钥", 220), ("push_time", "推送时间", 160), ("id", "编号", 180)]:
            self.key_tree.heading(c, text=t)
            self.key_tree.column(c, width=w)
        self.key_tree.pack(fill="x", padx=8, pady=4)
        kbtns = ttk.Frame(tab2)
        kbtns.pack(fill="x", padx=8, pady=4)
        ttk.Button(kbtns, text="添加密钥", command=self.add_key_dialog).pack(side="left", padx=3)
        ttk.Button(kbtns, text="修改选中密钥", command=self.edit_key_dialog).pack(side="left", padx=3)
        ttk.Button(kbtns, text="删除选中密钥", command=self.delete_key).pack(side="left", padx=3)
        ttk.Button(kbtns, text="测试选中密钥", command=self.on_test_selected_key).pack(side="left", padx=3)
        self.test_status_label = ttk.Label(tab2, text="测试状态：还没有测试过。点“测试选中密钥”后，这里会立刻显示结果，成功还会弹一个提示框。", wraplength=950)
        self.test_status_label.pack(fill="x", padx=8, pady=4)
        ttk.Label(tab2, text="推送时间说明：推送时间现在是每个密钥自带的，不再是全局一个时间。添加密钥或修改密钥时就要选好这个密钥几点推（北京时间，选完随时能改）：选“开奖后抓到就推”，抓到新一期就马上核对并推；选固定时间或自定义时间，软件会提前抓、核对完先做成待执行任务，到了该密钥的设定点再推；如果到了设定点数据还没出来，就继续等、继续抓，抓到了直接补推，不用等到第二天。",
                  wraplength=950, justify="left").pack(fill="x", padx=8, pady=6)

        # --- 购彩提醒 ---
        rbtns = ttk.Frame(tab_rem)
        rbtns.pack(fill="x", padx=8, pady=6)
        ttk.Button(rbtns, text="添加提醒", command=self.add_reminder_dialog).pack(side="left", padx=3)
        ttk.Button(rbtns, text="修改选中", command=self.edit_reminder_dialog).pack(side="left", padx=3)
        ttk.Button(rbtns, text="启用/停用选中", command=self.toggle_reminder).pack(side="left", padx=3)
        ttk.Button(rbtns, text="删除选中", command=self.delete_reminder).pack(side="left", padx=3)
        rcols = ("text", "key", "weekdays", "time", "status")
        self.rem_tree = ttk.Treeview(tab_rem, columns=rcols, show="headings", height=12)
        rheads = {"text": "提醒文字", "key": "推送到哪个密钥", "weekdays": "星期", "time": "时间", "status": "推送开关"}
        rwidths = {"text": 320, "key": 160, "weekdays": 200, "time": 80, "status": 90}
        for c in rcols:
            self.rem_tree.heading(c, text=rheads[c])
            self.rem_tree.column(c, width=rwidths[c])
        self.rem_tree.pack(fill="both", expand=True, padx=8, pady=4)
        ttk.Label(tab_rem, text="说明：每条提醒自选推送到哪个 Bark 密钥、勾选周几、设准确时间点（北京时间），到点推一次、当天不重复推。提醒只管到点推，不管你买没买。周五没有开奖，不想推就别勾周五。软件没开着的时候不推、不补推。",
                  wraplength=950, justify="left").pack(fill="x", padx=8, pady=4)

        # --- 日志 ---
        self.log_text = tk.Text(tab3, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    # ----- 列表刷新 -----
    def refresh_tickets(self):
        try:
            for row in self.tree.get_children():
                self.tree.delete(row)
            for t in self.tickets:
                cfg = GAMES[t["game"]]
                nums = "%s %s + %s %s" % (cfg["main_name"], fmt_nums(t["main"]), cfg["back_name"], fmt_nums(t["back"]))
                if t["monitor"] == "count":
                    monitor = "只对%d期（还剩%d次）" % (int(t.get("count") or 1), int(t.get("remaining") if t.get("remaining") is not None else (t.get("count") or 1)))
                else:
                    monitor = "一直守着"
                mode = "核对模式" if t["mode"] == "check" else "仅推开奖号"
                status = "监控中" if t["status"] == "active" else "已停止"
                self.tree.insert("", "end", iid=str(t["id"]), values=(t["game"], nums, monitor, mode, self.key_name(t["key_id"]), status, t.get("last_result", "")))
        except Exception:
            pass

    def refresh_keys(self):
        try:
            for row in self.key_tree.get_children():
                self.key_tree.delete(row)
            for k in self.settings["bark_keys"]:
                self.key_tree.insert("", "end", iid=k["id"], values=(k["name"], self.mask_key(k["key"]), key_push_time_display(k.get("push_time")), k["id"]))
        except Exception:
            pass

    def refresh_reminders(self):
        try:
            for row in self.rem_tree.get_children():
                self.rem_tree.delete(row)
            for r in self.settings.get("buy_reminders", []):
                wd = "、".join(WEEKDAY_NAMES[d] for d in sorted(r.get("weekdays", [])))
                status = "开" if r.get("enabled", True) else "关"
                self.rem_tree.insert("", "end", iid=str(r["id"]), values=(r.get("text", ""), self.key_name(r.get("key_id", "")), wd, r.get("time", ""), status))
        except Exception:
            pass

    def selected_reminder(self):
        sel = self.rem_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一条提醒。")
            return None
        rid = sel[0]
        for r in self.settings.get("buy_reminders", []):
            if str(r["id"]) == rid:
                return r
        return None

    # ----- 购彩提醒 -----
    def add_reminder_dialog(self, reminder=None):
        key_names = [k["name"] for k in self.settings["bark_keys"]]
        if not key_names and reminder is None:
            messagebox.showwarning("提示", "还没有 Bark 密钥，请先到“密钥与设置”页添加一个，再回来加提醒。")
            return
        win = tk.Toplevel(self.root)
        win.title("修改购彩提醒" if reminder else "添加购彩提醒")
        win.geometry("560x520")
        win.minsize(520, 420)
        text_var = tk.StringVar(value=reminder["text"] if reminder else "记得买彩票")
        key_var = tk.StringVar(value=self.key_name(reminder["key_id"]) if reminder else (key_names[0] if key_names else ""))
        _wd = reminder["weekdays"] if reminder else [0, 1, 2, 3, 5, 6]
        wd_vars = [tk.BooleanVar(value=(i in _wd)) for i in range(7)]
        _t = (reminder["time"] if reminder else "18:00").split(":")
        hour_var = tk.StringVar(value=_t[0] if len(_t) > 0 else "18")
        min_var = tk.StringVar(value=_t[1] if len(_t) > 1 else "00")
        btn_frame = ttk.Frame(win)
        btn_frame.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side="right", padx=3)
        ttk.Button(btn_frame, text="保存", command=lambda: on_save()).pack(side="right", padx=3)
        content = ttk.Frame(win)
        content.pack(side="top", fill="both", expand=True, padx=12, pady=8)
        ttk.Label(content, text="提醒文字（推到手机上的正文）").pack(anchor="w", pady=2)
        ttk.Entry(content, textvariable=text_var).pack(fill="x", pady=2)
        ttk.Label(content, text="推送到哪个密钥（鼠标点选）").pack(anchor="w", pady=6)
        ttk.Combobox(content, textvariable=key_var, values=key_names, state="readonly").pack(anchor="w", fill="x")
        ttk.Label(content, text="周几推送（勾选，周五没有开奖，不想推就别勾）").pack(anchor="w", pady=6)
        wd_frame = ttk.Frame(content)
        wd_frame.pack(anchor="w", pady=2)
        for i in range(7):
            ttk.Checkbutton(wd_frame, text=WEEKDAY_NAMES[i], variable=wd_vars[i]).pack(side="left", padx=4)
        ttk.Label(content, text="准确时间点（北京时间，鼠标下拉选数字）").pack(anchor="w", pady=6)
        time_frame = ttk.Frame(content)
        time_frame.pack(anchor="w", pady=2)
        ttk.Combobox(time_frame, textvariable=hour_var, values=["%02d" % i for i in range(24)], state="readonly", width=5).pack(side="left", padx=2)
        ttk.Label(time_frame, text="点").pack(side="left")
        ttk.Combobox(time_frame, textvariable=min_var, values=["%02d" % i for i in range(60)], state="readonly", width=5).pack(side="left", padx=2)
        ttk.Label(time_frame, text="分").pack(side="left")
        ttk.Label(content, text="到点推一次，当天不重复推。提醒只管到点推，不管你买没买。", justify="left").pack(anchor="w", pady=8)

        def on_save():
            text = text_var.get().strip()
            if not text:
                messagebox.showerror("填错了", "提醒文字不能为空。")
                return
            weekdays = [i for i in range(7) if wd_vars[i].get()]
            if not weekdays:
                messagebox.showerror("填错了", "周几至少要勾一天。")
                return
            key_id = ""
            for k in self.settings["bark_keys"]:
                if k["name"] == key_var.get():
                    key_id = k["id"]
                    break
            if not key_id:
                messagebox.showerror("填错了", "推送密钥：请先在“密钥与设置”页添加 Bark 密钥，再回来选。")
                return
            tstr = "%s:%s" % (hour_var.get(), min_var.get())
            wd_txt = "、".join(WEEKDAY_NAMES[d] for d in sorted(weekdays))
            if reminder is None:
                new_id = int(time.time() * 1000)
                self.settings["buy_reminders"].append({"id": new_id, "text": text, "key_id": key_id,
                                                       "weekdays": sorted(weekdays), "time": tstr, "enabled": True})
                self.log("添加购彩提醒：%s，推送到=%s，%s %s。" % (text, key_var.get(), wd_txt, tstr))
            else:
                reminder.update({"text": text, "key_id": key_id, "weekdays": sorted(weekdays), "time": tstr})
                self.log("修改购彩提醒：%s，推送到=%s，%s %s。" % (text, key_var.get(), wd_txt, tstr))
            self.save_all()
            self.refresh_reminders()
            win.destroy()

    def edit_reminder_dialog(self):
        r = self.selected_reminder()
        if r:
            self.add_reminder_dialog(r)

    def toggle_reminder(self):
        r = self.selected_reminder()
        if not r:
            return
        r["enabled"] = not r.get("enabled", True)
        self.log("购彩提醒开关：%s，改为%s。" % (r.get("text", ""), "开" if r["enabled"] else "关"))
        self.save_all()
        self.refresh_reminders()

    def delete_reminder(self):
        r = self.selected_reminder()
        if not r:
            return
        if not messagebox.askyesno("确认删除", "确定要删除这条提醒吗？"):
            return
        self.settings["buy_reminders"] = [x for x in self.settings.get("buy_reminders", []) if x["id"] != r["id"]]
        self.log("删除购彩提醒：%s。" % r.get("text", ""))
        self.save_all()
        self.refresh_reminders()

    def _do_reminder_push(self, key, key_name, text):
        try:
            ok = bark_push(key, "购彩提醒", text)
            method = getattr(bark_push, "last_method", "")
            method_txt = "（%s通了）" % method if method else ""
            self.log("购彩提醒推送：推送到=%s，结果=%s%s，文本=%s。" % (key_name, "成功" if ok else "失败", method_txt, text))
        except Exception as e:
            cause = e.reason if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None) is not None else e
            if is_cert_error(cause):
                self.log("购彩提醒推送失败：推送到=%s，%s。" % (key_name, CERT_HUMAN))
                self.log_file_only("购彩提醒推送原始证书错误（只记文件）：原始错误=%s，各招原始错误=%s。" % (cause, getattr(bark_push, "last_errors", {})))
            else:
                self.log("购彩提醒推送失败：推送到=%s，原因：%s。" % (key_name, cause))

    def reminder_loop(self):
        while not self.stop_flag:
            try:
                now = now_bj()
                date_str = now.strftime("%Y-%m-%d")
                hm = "%02d:%02d" % (now.hour, now.minute)
                for r in list(self.settings.get("buy_reminders", [])):
                    if not r.get("enabled", True):
                        continue
                    if now.weekday() not in (r.get("weekdays") or []):
                        continue
                    if r.get("time", "") != hm:
                        continue
                    pushed_key = (r["id"], date_str)
                    if pushed_key in self.reminder_pushed:
                        continue
                    self.reminder_pushed.add(pushed_key)
                    key = self.key_value(r.get("key_id", ""))
                    if not key:
                        self.log("购彩提醒跳过：%s，密钥已不在。" % r.get("text", ""))
                        continue
                    threading.Thread(target=self._do_reminder_push,
                                     args=(key, self.key_name(r.get("key_id", "")), r.get("text", "")), daemon=True).start()
            except Exception:
                pass
            for _ in range(30):
                if self.stop_flag:
                    return
                time.sleep(1)

    def selected_ticket(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一注。")
            return None
        tid = sel[0]
        for t in self.tickets:
            if str(t["id"]) == tid:
                return t
        return None

    # ----- 加票 / 改票 -----
    def add_ticket_dialog(self, ticket=None):
        win = tk.Toplevel(self.root)
        win.title("修改彩票" if ticket else "添加彩票")
        win.geometry("720x760")
        game_var = tk.StringVar(value=ticket["game"] if ticket else "双色球")
        monitor_var = tk.StringVar(value=MONITOR_LABELS.get(ticket["monitor"], "一直守着") if ticket else "一直守着")
        _old_count = ticket.get("count") if ticket else None
        count_var = tk.StringVar(value=str(int(_old_count) if _old_count else 1) if ticket else "1")
        mode_var = tk.StringVar(value=MODE_LABELS.get(ticket["mode"], "核对模式") if ticket else "核对模式")
        key_names = [k["name"] for k in self.settings["bark_keys"]]
        key_var = tk.StringVar(value=self.key_name(ticket["key_id"]) if ticket else (key_names[0] if key_names else ""))
        # 底部固定按钮框先放好，保证任何字体下按钮都完整可见
        btn_frame = ttk.Frame(win)
        btn_frame.pack(side="bottom", fill="x", padx=8, pady=8)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side="right", padx=3)
        ttk.Button(btn_frame, text="保存", command=lambda: on_save()).pack(side="right", padx=3)
        content = ttk.Frame(win)
        content.pack(side="top", fill="both", expand=True, padx=8, pady=4)
        ttk.Label(content, text="彩种（鼠标点选）").pack(anchor="w", pady=2)
        ttk.Combobox(content, textvariable=game_var, values=list(GAMES.keys()), state="readonly").pack(anchor="w", fill="x")
        picker_holder = ttk.Frame(content)
        picker_holder.pack(fill="both", expand=True)
        pickers = {}

        def rebuild_pickers():
            for child in picker_holder.winfo_children():
                child.destroy()
            game = game_var.get()
            cfg = GAMES[game]
            init_main = ticket["main"] if ticket and ticket["game"] == game else []
            init_back = ticket["back"] if ticket and ticket["game"] == game else []
            pickers["main"] = BallPicker(picker_holder, "%s（点一下选中，再点取消，选满后不能再选）" % cfg["main_name"], cfg["main_lo"], cfg["main_hi"], cfg["main_count"], init_main)
            pickers["main"].pack(fill="x", pady=4)
            pickers["back"] = BallPicker(picker_holder, "%s（点一下选中，再点取消，选满后不能再选）" % cfg["back_name"], cfg["back_lo"], cfg["back_hi"], cfg["back_count"], init_back)
            pickers["back"].pack(fill="x", pady=4)
            main_hint.configure(text="%s：%d-%d 选 %d 个" % (cfg["main_name"], cfg["main_lo"], cfg["main_hi"], cfg["main_count"]))
            back_hint.configure(text="%s：%d-%d 选 %d 个" % (cfg["back_name"], cfg["back_lo"], cfg["back_hi"], cfg["back_count"]))

        main_hint = ttk.Label(content, text="")
        back_hint = ttk.Label(content, text="")
        # 先建占位，再由 rebuild 填充；提示行放在选择器下方
        rebuild_pickers()
        main_hint.pack(anchor="w")
        back_hint.pack(anchor="w")
        game_combo = content.winfo_children()[1] if len(content.winfo_children()) > 1 else None
        # 彩种切换时重建小球（改票回显只在同彩种时保留）
        try:
            combo_widget = None
            for child in content.winfo_children():
                if isinstance(child, ttk.Combobox):
                    combo_widget = child
                    break
            if combo_widget is not None:
                combo_widget.bind("<<ComboboxSelected>>", lambda e: rebuild_pickers())
        except Exception:
            pass
        ttk.Label(content, text="监控方式（鼠标点选）").pack(anchor="w", pady=2)
        ttk.Combobox(content, textvariable=monitor_var, values=list(MONITOR_LABELS.values()), state="readonly").pack(anchor="w", fill="x")
        ttk.Label(content, text="只对几次（监控方式选“只对x期”时用，鼠标下拉点选1-10次，不用打字）").pack(anchor="w", pady=2)
        ttk.Combobox(content, textvariable=count_var, values=[str(i) for i in range(1, 11)], state="readonly").pack(anchor="w", fill="x")
        ttk.Label(content, text="模式（鼠标点选）").pack(anchor="w", pady=2)
        ttk.Combobox(content, textvariable=mode_var, values=list(MODE_LABELS.values()), state="readonly").pack(anchor="w", fill="x")
        ttk.Label(content, text="仅推开奖号模式是给随机机打的票手动核对用的，只推开奖号，不报中没中。").pack(anchor="w", pady=2)
        if not key_names:
            ttk.Label(content, text="还没有 Bark 密钥，请先到“密钥与设置”页添加一个，再回来加票。").pack(anchor="w", pady=6)
        else:
            ttk.Label(content, text="推送到哪个密钥（鼠标点选）").pack(anchor="w", pady=2)
            ttk.Combobox(content, textvariable=key_var, values=key_names, state="readonly").pack(anchor="w", fill="x")

        def on_save():
            game = game_var.get()
            if game not in GAMES:
                messagebox.showerror("填错了", "彩种：请选择双色球或大乐透。")
                return
            cfg = GAMES[game]
            main = pickers["main"].get()
            back = pickers["back"].get()
            if len(main) != cfg["main_count"]:
                messagebox.showerror("填错了", "%s应选 %d 个，你只选了 %d 个，还差 %d 个，请点小球补齐再保存，该条暂不保存。" % (cfg["main_name"], cfg["main_count"], len(main), cfg["main_count"] - len(main)))
                return
            if len(back) != cfg["back_count"]:
                messagebox.showerror("填错了", "%s应选 %d 个，你只选了 %d 个，还差 %d 个，请点小球补齐再保存，该条暂不保存。" % (cfg["back_name"], cfg["back_count"], len(back), cfg["back_count"] - len(back)))
                return
            monitor = MONITOR_FROM_LABEL.get(monitor_var.get(), "always")
            try:
                count = int(count_var.get())
            except Exception:
                count = 1
            count = max(1, min(10, count))
            if monitor == "count":
                if ticket is not None and ticket.get("monitor") == "count" and int(ticket.get("count") or 1) == count and ticket.get("remaining") is not None:
                    remaining = int(ticket["remaining"])
                else:
                    remaining = count
            else:
                remaining = None
            mode = MODE_FROM_LABEL.get(mode_var.get(), "check")
            key_id = ""
            for k in self.settings["bark_keys"]:
                if k["name"] == key_var.get():
                    key_id = k["id"]
                    break
            if not key_id:
                messagebox.showerror("填错了", "推送密钥：请先在“密钥与设置”页添加 Bark 密钥，再回来选。")
                return
            _monitor_text = MONITOR_LABELS[monitor] if monitor == "always" else "只对%d期（还剩%d次）" % (count, remaining)
            if ticket is None:
                new_id = int(time.time() * 1000)
                self.tickets.append({"id": new_id, "game": game, "main": main, "back": back,
                                     "monitor": monitor, "count": count if monitor == "count" else 0,
                                     "remaining": remaining, "mode": mode,
                                     "key_id": key_id, "status": "active", "last_result": "",
                                     "pushed": []})
                self.log("添加彩票：%s %s + %s，监控=%s，模式=%s，推送到=%s。" % (game, fmt_nums(main), fmt_nums(back), _monitor_text, MODE_LABELS[mode], key_var.get()))
            else:
                ticket.update({"game": game, "main": main, "back": back, "monitor": monitor,
                               "count": count if monitor == "count" else 0, "remaining": remaining,
                               "mode": mode, "key_id": key_id})
                ticket.pop("issue", None)
                self.log("修改彩票：%s %s + %s，监控=%s，模式=%s，推送到=%s。" % (game, fmt_nums(main), fmt_nums(back), _monitor_text, MODE_LABELS[mode], key_var.get()))
            self.save_all()
            self.refresh_tickets()
            win.destroy()

    def edit_ticket_dialog(self):
        t = self.selected_ticket()
        if t:
            self.add_ticket_dialog(t)

    def toggle_ticket(self):
        t = self.selected_ticket()
        if not t:
            return
        t["status"] = "stopped" if t["status"] == "active" else "active"
        self.log("%s彩票：%s %s，状态改为%s。" % (t["game"], fmt_nums(t["main"]), fmt_nums(t["back"]), "监控中" if t["status"] == "active" else "已停止"))
        self.save_all()
        self.refresh_tickets()

    def delete_ticket(self):
        t = self.selected_ticket()
        if not t:
            return
        if not messagebox.askyesno("确认删除", "确定要删除这一注吗？删除后就真没了，停止只是暂停、可恢复。"):
            return
        self.tickets = [x for x in self.tickets if x["id"] != t["id"]]
        self.log("删除彩票：%s %s。" % (t["game"], fmt_nums(t["main"])))
        self.save_all()
        self.refresh_tickets()

    # ----- 密钥 -----
    def add_key_dialog(self, key_entry=None):
        win = tk.Toplevel(self.root)
        win.title("修改 Bark 密钥" if key_entry else "添加 Bark 密钥")
        win.geometry("640x520")
        win.minsize(620, 420)
        name_var = tk.StringVar(value=key_entry["name"] if key_entry else "")
        key_var = tk.StringVar()
        _saved = str(key_entry.get("push_time") or "开奖后抓到就推") if key_entry else "开奖后抓到就推"
        _is_custom = is_custom_push_time(_saved)
        _ch, _cm = split_custom_push_time(_saved)
        push_time_var = tk.StringVar(value="自定义" if _is_custom else _saved)
        custom_hour_var = tk.StringVar(value=_ch)
        custom_min_var = tk.StringVar(value=_cm)
        btn_frame = ttk.Frame(win)
        btn_frame.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side="right", padx=3)
        ttk.Button(btn_frame, text="保存", command=lambda: on_save()).pack(side="right", padx=3)
        content = ttk.Frame(win)
        content.pack(side="top", fill="both", expand=True, padx=12, pady=8)
        ttk.Label(content, text="名称（比如：我的手机）").pack(anchor="w", pady=2)
        ttk.Entry(content, textvariable=name_var).pack(fill="x", pady=2)
        ttk.Label(content, text="密钥（只存在本机设置文件里，输完保存后列表里只显示前后各3位）").pack(anchor="w", pady=2)
        ttk.Entry(content, textvariable=key_var).pack(fill="x", pady=2)
        ttk.Label(content, text="密钥只存在你本机设置文件里，请不要把完整 key 发在聊天里。", wraplength=600).pack(anchor="w", pady=2)
        if key_entry:
            ttk.Label(content, text="修改时密钥留空就是不改密钥，只改名称和推送时间。").pack(anchor="w", pady=2)
        ttk.Label(content, text="这个密钥几点推送（北京时间，鼠标点选，以后在修改里随时能改）").pack(anchor="w", pady=6)
        push_combo = ttk.Combobox(content, textvariable=push_time_var, values=PUSH_TIME_OPTIONS, state="readonly")
        push_combo.pack(anchor="w", fill="x")
        custom_frame = ttk.Frame(content)
        ttk.Label(custom_frame, text="自定义时间：小时").pack(side="left")
        ttk.Combobox(custom_frame, textvariable=custom_hour_var, values=["%02d" % i for i in range(24)], state="readonly", width=5).pack(side="left", padx=2)
        ttk.Label(custom_frame, text="点，分钟").pack(side="left")
        ttk.Combobox(custom_frame, textvariable=custom_min_var, values=["%02d" % i for i in range(60)], state="readonly", width=5).pack(side="left", padx=2)
        ttk.Label(custom_frame, text="分（鼠标下拉选数字，不用打字）").pack(side="left")

        def refresh_custom():
            if push_time_var.get() == "自定义":
                custom_frame.pack(anchor="w", pady=4)
            else:
                custom_frame.pack_forget()
        push_combo.bind("<<ComboboxSelected>>", lambda e: refresh_custom())
        refresh_custom()
        ttk.Label(content, text="密钥绝不要发在聊天里。填在这里只存在你电脑上。\n加完后用后面的测试按钮推一条，看通不通。推送时间是这个密钥自己的，不同密钥可以不同时间推。",
                  justify="left").pack(anchor="w", pady=8)

        def on_save():
            name = name_var.get().strip()
            key = key_var.get().strip()
            if push_time_var.get() == "自定义":
                push_time = "%s:%s" % (custom_hour_var.get(), custom_min_var.get())
            else:
                push_time = push_time_var.get()
            if not name:
                messagebox.showerror("填错了", "名称不能为空。")
                return
            if key_entry is None and not key:
                messagebox.showerror("填错了", "名称和密钥都不能为空。")
                return
            if any(k["name"] == name and (key_entry is None or k["id"] != key_entry["id"]) for k in self.settings["bark_keys"]):
                messagebox.showerror("填错了", "这个名称已经用过了，请换一个名称。")
                return
            if key_entry is None:
                self.settings["bark_keys"].append({"id": str(int(time.time() * 1000)), "name": name, "key": key, "push_time": push_time})
                self.log("添加 Bark 密钥：名称=%s，推送时间=%s（密钥已打码，不记日志）。" % (name, push_time))
            else:
                key_entry["name"] = name
                key_entry["push_time"] = push_time
                if key:
                    key_entry["key"] = key
                self.log("修改 Bark 密钥：名称=%s，推送时间=%s（密钥已打码，不记日志）。" % (name, push_time))
            self.save_all()
            self.refresh_keys()
            win.destroy()

    def edit_key_dialog(self):
        sel = self.key_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一个密钥。")
            return
        for k in self.settings["bark_keys"]:
            if k["id"] == sel[0]:
                self.add_key_dialog(k)
                return

    def delete_key(self):
        sel = self.key_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一个密钥。")
            return
        kid = sel[0]
        used = [t for t in self.tickets if t["key_id"] == kid]
        if used:
            messagebox.showwarning("不能删", "还有 %d 注在用这个密钥推送，请先把那些注改到别的密钥或删掉，再来删密钥。" % len(used))
            return
        used_rem = [r for r in self.settings.get("buy_reminders", []) if r.get("key_id") == kid]
        if used_rem:
            messagebox.showwarning("不能删", "还有 %d 条购彩提醒在用这个密钥推送，请先把那些提醒改到别的密钥或删掉，再来删密钥。" % len(used_rem))
            return
        if not messagebox.askyesno("确认删除", "确定要删除这个密钥吗？"):
            return
        self.settings["bark_keys"] = [k for k in self.settings["bark_keys"] if k["id"] != kid]
        self.save_all()
        self.refresh_keys()
        self.log("删除 Bark 密钥一个（名称不记全称，密钥不记日志）。")

    def key_push_time(self, key_id):
        for k in self.settings["bark_keys"]:
            if k["id"] == key_id:
                return key_push_time_display(k.get("push_time"))
        return key_push_time_display(self.settings.get("push_time"))

    def on_test_selected_key(self):
        sel = self.key_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一个密钥。")
            return
        kid = sel[0]
        name = self.key_name(kid)
        key = self.key_value(kid)
        self.test_status_label.configure(text="测试状态：正在测试“%s”，请稍等，结果马上会在这里显示并弹出来……" % name)
        self.log("测试 Bark 密钥：%s，正在后台推送测试消息……" % name)
        threading.Thread(target=self._test_key_worker, args=(kid, name, key), daemon=True).start()

    def _test_key_worker(self, kid, name, key):
        ok = False
        err = ""
        is_cert = False
        method = ""
        try:
            ok = bool(bark_push(key, "彩票小软件测试", "Bark密钥测试成功，密钥名称：%s" % name))
            method = getattr(bark_push, "last_method", "")
            if not ok:
                err = "对方返回的不是成功结果"
        except Exception as e:
            cause = e.reason if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None) is not None else e
            is_cert = is_cert_error(cause)
            err = str(cause)
            method = getattr(bark_push, "last_method", "")
            # GET/POST/certifi 的原始错误只记日志文件，不弹界面、不记 key
            try:
                self.log_file_only("测试 Bark 密钥各招原始错误（只记文件）：%s，%s。" % (name, getattr(bark_push, "last_errors", {})))
            except Exception:
                pass
        # 只把结果放进队列，不在这里直接弹框；主线程轮询时会弹框。
        self.test_result_queue.put((name, ok, err, is_cert, method))

    def _test_key_done(self, name, ok, err, is_cert=False, method=""):
        stamp = fmt_time()
        if ok:
            method_txt = "（%s通了）" % method if method else ""
            status = "测试状态：%s，“%s”测试成功%s，手机应该收到一条测试推送。" % (stamp, name, method_txt)
            self.test_status_label.configure(text=status)
            self.log("测试 Bark 密钥：%s，结果=成功%s。" % (name, method_txt))
            messagebox.showinfo("测试成功", "测试成功%s，手机应该收到一条测试推送。" % method_txt)
        elif is_cert:
            status = "测试状态：%s，“%s”%s。" % (stamp, name, CERT_HUMAN)
            self.test_status_label.configure(text=status)
            self.log("测试 Bark 密钥：%s，结果=%s。" % (name, CERT_HUMAN))
            self.log_file_only("测试 Bark 密钥原始证书错误（只记文件，不弹界面）：%s，原始错误=%s。" % (name, err))
            messagebox.showerror("测试失败", CERT_HUMAN)
        else:
            reason = ("，原因：%s" % err) if err else ""
            status = "测试状态：%s，“%s”测试失败，请检查密钥是否填对%s。" % (stamp, name, reason)
            self.test_status_label.configure(text=status)
            self.log("测试 Bark 密钥：%s，结果=失败%s。" % (name, reason))
            messagebox.showerror("测试失败", "测试失败，请检查密钥是否填对%s。" % reason)

    # ----- 手动核对 -----
    def manual_draw_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("手动输入开奖号核对")
        win.geometry("720x650")
        game_var = tk.StringVar(value="双色球")
        issue_var = tk.StringVar(value="2025001")
        btn_frame = ttk.Frame(win)
        btn_frame.pack(side="bottom", fill="x", padx=8, pady=8)
        ttk.Button(btn_frame, text="取消", command=win.destroy).pack(side="right", padx=3)
        ttk.Button(btn_frame, text="核对并推送", command=lambda: on_go()).pack(side="right", padx=3)
        content = ttk.Frame(win)
        content.pack(side="top", fill="both", expand=True, padx=8, pady=4)
        ttk.Label(content, text="彩种（鼠标点选）").pack(anchor="w", pady=2)
        game_combo = ttk.Combobox(content, textvariable=game_var, values=list(GAMES.keys()), state="readonly")
        game_combo.pack(anchor="w", fill="x")
        ttk.Label(content, text="期号（上下箭头点选，不用打字）").pack(anchor="w", pady=2)
        tk.Spinbox(content, textvariable=issue_var, from_=1, to=9999999, width=20).pack(anchor="w", fill="x")
        picker_holder = ttk.Frame(content)
        picker_holder.pack(fill="both", expand=True)
        pickers = {}

        def rebuild():
            for child in picker_holder.winfo_children():
                child.destroy()
            cfg = GAMES[game_var.get()]
            pickers["main"] = BallPicker(picker_holder, "%s（点一下选中，再点取消，选满后不能再选）" % cfg["main_name"], cfg["main_lo"], cfg["main_hi"], cfg["main_count"], [])
            pickers["main"].pack(fill="x", pady=4)
            pickers["back"] = BallPicker(picker_holder, "%s（点一下选中，再点取消，选满后不能再选）" % cfg["back_name"], cfg["back_lo"], cfg["back_hi"], cfg["back_count"], [])
            pickers["back"].pack(fill="x", pady=4)

        rebuild()
        game_combo.bind("<<ComboboxSelected>>", lambda e: rebuild())
        ttk.Label(content, text="抓不到数据时的兜底：用小球点出当期开奖号，核对并推送一次（手动推送不等固定时间，直接推）。",
                  wraplength=680, justify="left").pack(anchor="w", pady=8)

        def on_go():
            game = game_var.get()
            issue = issue_var.get().strip()
            if not issue:
                messagebox.showerror("填错了", "期号不能为空，请用上下箭头选一期。")
                return
            cfg = GAMES[game]
            main = pickers["main"].get()
            back = pickers["back"].get()
            if len(main) != cfg["main_count"]:
                messagebox.showerror("填错了", "%s应选 %d 个，你只选了 %d 个，还差 %d 个，请点小球补齐再核对。" % (cfg["main_name"], cfg["main_count"], len(main), cfg["main_count"] - len(main)))
                return
            if len(back) != cfg["back_count"]:
                messagebox.showerror("填错了", "%s应选 %d 个，你只选了 %d 个，还差 %d 个，请点小球补齐再核对。" % (cfg["back_name"], cfg["back_count"], len(back), cfg["back_count"] - len(back)))
                return
            win.destroy()
            threading.Thread(target=self.process_draw, args=(game, issue, {"main": main, "back": back}, "手动输入", True), daemon=True).start()

    def manual_fetch(self):
        for game in GAMES:
            try:
                (issue, main, back, draw_date), source = fetch_latest(game)
                self.log("手动抓取：%s第%s期%s，开奖号：%s，来源：%s。" % (game, issue, "（开奖日期%s）" % draw_date if draw_date else "（无开奖日期）", draw_text(game, {"main": main, "back": back}), source))
                self.process_draw(game, issue, {"main": main, "back": back}, source, True, draw_date)
            except Exception as e:
                self.log("手动抓取失败：%s，原因：%s。可改用“手动输入开奖号核对”。" % (game, e))

    # ----- 核心处理：核对 + 推送（自动走推送时间，手动走立即推） -----
    def process_draw(self, game, issue, draw, source, manual=False, draw_date=None):
        key_processed = (game, issue)
        if not manual and key_processed in self.processed:
            return
        self.processed[key_processed] = draw
        self.latest_draw[game] = (issue, draw, source)
        try:
            self.latest_label.configure(text="最新开奖：%s第%s期%s %s（来源：%s，%s）" % (game, issue, " 开奖日期" + draw_date if draw_date else "", draw_text(game, draw), source, fmt_time()))
        except Exception:
            pass
        dstr = draw_text(game, draw)
        self.log("核对开奖：%s第%s期%s，开奖号：%s，来源：%s。" % (game, issue, "（开奖日期%s）" % draw_date if draw_date else "", dstr, source))
        # 按密钥分组
        groups = {}
        order = []
        for t in self.tickets:
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
            self.log("核对开奖：%s第%s期，本期没有监控中的票需要推送。" % (game, issue))
            return
        for key_id in order:
            tickets = groups[key_id]
            key = self.key_value(key_id)
            key_name = self.key_name(key_id)
            if not key:
                self.log("推送跳过：%s第%s期，密钥“%s”已不在，%d 注未推。" % (game, issue, key_name, len(tickets)))
                continue
            check_tickets = [t for t in tickets if t["mode"] == "check"]
            only_tickets = [t for t in tickets if t["mode"] == "only"]
            # 核对模式：一个密钥一组，只推一条汇总
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
                            self.log("只对x期已完成：%s第%s期票已跑满自动停止。" % (game, issue))
                    t.setdefault("pushed", []).append(issue)
                    self.log("核对结果：%s %s + %s，%s期，%s。" % (game, fmt_nums(t["main"]), fmt_nums(t["back"]), issue, tier if tier else "没中"))
                text, _win = build_check_texts(game, issue, dstr, results, draw_date)
                title = "彩票开奖：%s" % draw_prefix(game, issue, draw_date)
                self.deliver(game, issue, key_id, key, key_name, title, text, manual)
            # 仅推开奖号模式：一个密钥一组，只推一条开奖号，不报中没中
            if only_tickets:
                for t in only_tickets:
                    t["last_result"] = "%s期：已推开奖号" % issue
                    if t["monitor"] == "count":
                        remaining = max(0, int(t.get("remaining") if t.get("remaining") is not None else (t.get("count") or 1)) - 1)
                        t["remaining"] = remaining
                        if remaining == 0:
                            t["status"] = "stopped"
                            self.log("只对x期已完成：%s第%s期票已跑满自动停止。" % (game, issue))
                    t.setdefault("pushed", []).append(issue)
                text = build_only_draw_text(game, issue, dstr, draw_date)
                title = "彩票开奖：%s" % draw_prefix(game, issue, draw_date)
                self.deliver(game, issue, key_id, key, key_name, title, text, manual)
        self.save_all()
        self.refresh_tickets()

    def deliver(self, game, issue, key_id, key, key_name, title, text, manual):
        """manual=True 立即推；否则按该票所选密钥的推送时间：抓到就推立即推，固定时间做成待执行任务。"""
        push_time = self.key_push_time(key_id)
        if manual or push_time == "开奖后抓到就推":
            threading.Thread(target=self.do_push, args=(key, key_name, title, text, game, issue), daemon=True).start()
            return
        # 固定时间：做成待执行任务
        target = push_time
        today = now_bj().date()
        try:
            hh, mm = target.split(":")
            target_dt = datetime(today.year, today.month, today.day, int(hh), int(mm), tzinfo=BJ)
        except Exception:
            threading.Thread(target=self.do_push, args=(key, key_name, title, text, game, issue), daemon=True).start()
            return
        task = {"game": game, "issue": issue, "key_id": key_id, "key_name": key_name,
                "title": title, "text": text, "target": target_dt.strftime("%Y-%m-%d %H:%M:%S")}
        # 待执行任务里只存密钥编号，不存密钥原文；推送时再从设置文件里取密钥，日志里绝不写密钥
        self.pending.append(task)
        self.save_all()
        self.log("待执行推送：%s第%s期，推送到=%s，设定点=%s（北京时间），到了点再推；若数据是到了点之后才抓到的，会直接补推。" % (game, issue, key_name, target))

    def do_push(self, key, key_name, title, text, game, issue):
        try:
            ok = bark_push(key, title, text)
            method = getattr(bark_push, "last_method", "")
            method_txt = "（%s通了）" % method if method else ""
            self.log("Bark 推送：%s第%s期，推送到=%s，结果=%s%s，文本=%s。" % (game, issue, key_name, "成功" if ok else "失败", method_txt, text))
        except Exception as e:
            cause = e.reason if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None) is not None else e
            if is_cert_error(cause):
                self.log("Bark 推送失败：%s第%s期，推送到=%s，%s。" % (game, issue, key_name, CERT_HUMAN))
                self.log_file_only("Bark 推送原始证书错误（只记文件）：%s第%s期，原始错误=%s，各招原始错误=%s。" % (game, issue, cause, getattr(bark_push, "last_errors", {})))
            else:
                self.log("Bark 推送失败：%s第%s期，推送到=%s，原因：%s（下次再试，不卡界面）。" % (game, issue, key_name, cause))

    # ----- 后台轮询（v1.6：只在开奖日晚上抓，抓到就停，22:00 抓不到就停，22:10 推失败提醒） -----
    def _is_new_issue(self, game, issue):
        """这期是不是还没处理过的新一期。已在本轮处理过、或任何票里已推过这期，
        都算上一期（新一期还没出来），不能因为它就停，否则会错过今日新一期。"""
        if (game, issue) in self.processed:
            return False
        for t in self.tickets:
            if t["game"] == game and issue in (t.get("pushed") or []):
                return False
        return True

    def _do_fetch_failure_push(self, key, key_name, game):
        title = "彩票开奖%s" % game
        body = "%s获取今日开奖信息失败，请手动尝试。" % game
        try:
            ok = bark_push(key, title, body)
            self.log("开奖失败提醒：%s，推送到=%s，结果=%s，文本=%s。" % (game, key_name, "成功" if ok else "失败", body))
        except Exception as e:
            cause = e.reason if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None) is not None else e
            if is_cert_error(cause):
                self.log("开奖失败提醒失败：%s，推送到=%s，%s。" % (game, key_name, CERT_HUMAN))
                self.log_file_only("开奖失败提醒原始证书错误（只记文件）：%s，原始错误=%s，各招原始错误=%s。" % (game, cause, getattr(bark_push, "last_errors", {})))
            else:
                self.log("开奖失败提醒失败：%s，推送到=%s，原因：%s。" % (game, key_name, cause))

    def _push_fetch_failure(self, game, active):
        """22:10 当晚只推一次：按监控中的票分组，每把密钥推一条，不重复推。"""
        seen_keys = set()
        for t in active:
            key_id = t["key_id"]
            if key_id in seen_keys:
                continue
            seen_keys.add(key_id)
            key = self.key_value(key_id)
            if not key:
                continue
            threading.Thread(target=self._do_fetch_failure_push,
                             args=(key, self.key_name(key_id), game), daemon=True).start()

    def poll_loop(self):
        while not self.stop_flag:
            try:
                now = now_bj()
                weekday = now.weekday()
                date_str = now.strftime("%Y-%m-%d")
                # 只在开奖日晚上 21:15（晚上九点一刻，不是早上）到 22:00 抓，
                # 每 5 分钟整点一轮：21:15、21:20、……、22:00；其余时间一律不抓。
                # 22:10 只推一次失败提醒；抓到今日新一期后当晚就停，不再抓。
                for game, cfg in GAMES.items():
                    if weekday not in cfg["draw_weekdays"]:
                        continue
                    done_key = (game, date_str)
                    if done_key in self.auto_done:
                        continue
                    active = [t for t in self.tickets if t["game"] == game and t["status"] == "active"]
                    if not active:
                        continue
                    minute_of_day = now.hour * 60 + now.minute
                    # 22:10 失败提醒：到 22:00 还没抓到，且本轮还没推过，才推一次
                    if minute_of_day == 22 * 60 + 10 and done_key not in self.auto_fail_notified:
                        self.auto_fail_notified.add(done_key)
                        self.auto_done.add(done_key)
                        self.log("自动抓取：%s到 22:00 还没抓到今日开奖号，停止抓取，22:10 推失败提醒。" % game)
                        self._push_fetch_failure(game, active)
                        continue
                    if minute_of_day < 21 * 60 + 15 or minute_of_day > 22 * 60:
                        continue
                    if now.minute % 5 != 0:
                        continue
                    attempt_key = (game, date_str, "%02d:%02d" % (now.hour, now.minute))
                    if attempt_key in self.auto_attempted:
                        continue
                    self.auto_attempted.add(attempt_key)
                    try:
                        (issue, main, back, draw_date), source = fetch_latest(game)
                        # v1.8 日期校验：有开奖日期且不是今天，就是上一期，继续等、不推、
                        # 不记 auto_done（否则会错过今日真开奖）；没日期时按旧规则处理并记日志。
                        if draw_date and draw_date != date_str:
                            self.log("自动抓取：%s第%s期开奖日期是%s，不是今天（%s），是上一期，继续等下一轮。" % (game, issue, draw_date, date_str))
                            continue
                        if not draw_date:
                            self.log("自动抓取：%s第%s期（来源：%s）没有开奖日期，无法校验日期，按期号旧规则处理。" % (game, issue, source))
                        if not self._is_new_issue(game, issue):
                            self.log("自动抓取：%s第%s期是上一期（已处理过），今日新一期还没出来，继续等下一轮。" % (game, issue))
                            continue
                        self.log("自动抓取：%s第%s期%s，开奖号：%s，来源：%s。" % (game, issue, "（开奖日期%s）" % draw_date if draw_date else "", draw_text(game, {"main": main, "back": back}), source))
                        self.process_draw(game, issue, {"main": main, "back": back}, source, False, draw_date)
                        # 抓到今日新一期就停：当晚不再抓这个彩种
                        self.auto_done.add(done_key)
                        self.log("自动抓取：%s今日新一期已抓到，当晚停止抓取。" % game)
                    except Exception as e:
                        self.log("自动抓取失败：%s，原因：%s（下一轮 5 分钟后再试，不硬刷；到 22:00 还抓不到就停）。" % (game, e))
            except Exception:
                pass
            # 每 30 秒看一次表，才能卡住 21:15、21:20……和 22:10 这几个整点；不是每 30 秒抓一次
            for _ in range(30):
                if self.stop_flag:
                    return
                time.sleep(1)

    def pending_loop(self):
        while not self.stop_flag:
            try:
                now = now_bj()
                remaining = []
                for task in list(self.pending):
                    try:
                        target = datetime.strptime(task["target"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
                    except Exception:
                        continue
                    if now >= target:
                        key = self.key_value(task.get("key_id", ""))
                        if key:
                            threading.Thread(target=self.do_push, args=(key, task["key_name"], task["title"], task["text"], task["game"], task["issue"]), daemon=True).start()
                        else:
                            self.log("待执行推送跳过：%s第%s期，密钥已不在。" % (task["game"], task["issue"]))
                    else:
                        remaining.append(task)
                if len(remaining) != len(self.pending):
                    self.pending = remaining
                    save_json(PENDING_FILE, self.pending)
            except Exception:
                pass
            for _ in range(30):
                if self.stop_flag:
                    return
                time.sleep(1)

    def on_close(self):
        self.stop_flag = True
        self.save_all()
        self.root.destroy()


def main():
    if tk is None:
        raise RuntimeError("本机没有 tkinter，无法打开界面")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
