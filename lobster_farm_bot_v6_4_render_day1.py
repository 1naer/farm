#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙虾农场 Termux / 云服务器 / Render 智能挂机脚本 v6.4

本版重点：
1. v6.2：自动升级保留金改为动态计算：下轮种满 12 块地 + 预留浇水预算。
2. 若接口仍返回旧 lobster 字段，则自动兜底使用旧字段，避免字段变动导致脚本失效。
3. 每块田按等级倍率动态计算实际成熟时间、实际种子成本、单位时间净收益。
4. 自动收获成熟地块、空地智能补种、动态保留金自动升级。
5. 自适应等待：每轮按所有田里最近成熟的地块决定下次巡检时间。
6. 支持云服务器部署：可用环境变量 FARM_COOKIE 覆盖代码内 Cookie。

运行：
  python3 lobster_farm_bot.py

后台运行：
  nohup python3 lobster_farm_bot.py > lobster_farm_bot.nohup.log 2>&1 &

停止：
  pkill -f lobster_farm_bot.py
"""

import json
import os
import time
import ssl
import re
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

BASE_URL = "https://fanzisima.xyz/stocks/api/farm"
STOCKS_API_BASE_URL = os.environ.get("STOCKS_API_BASE_URL", "https://fanzisima.xyz/stocks/api")
COOKIE = os.environ.get(
    "FARM_COOKIE",
    ""
)

PLOTS = range(1, 13)
REQUEST_TIMEOUT = 25
LOG_FILE = os.environ.get("FARM_LOG_FILE", "lobster_farm_bot.log")
SSL_CONTEXT = ssl._create_unverified_context()

# v6.3.1：套利日切/强平全部按中国时区，避免 VPS 本地时区不同。
FARM_TIMEZONE = os.environ.get("FARM_TIMEZONE", "Asia/Shanghai")
CN_TZ = ZoneInfo(FARM_TIMEZONE) if ZoneInfo is not None else timezone(timedelta(hours=8))

# 策略参数
AUTO_UPGRADE = True
MAX_UPGRADE_PER_ROUND = 3
# v6.2：升级不再使用固定 5000 万储备。
# 动态保留金 = 下轮种满 PLOTS 全部地块所需种子预算 + 预留浇水预算。
# FARM_RESERVE_MONEY 仅作为动态计算失败时的兜底值，默认 0。
RESERVE_MONEY = int(os.environ.get("FARM_RESERVE_MONEY", "0"))
DYNAMIC_UPGRADE_RESERVE = os.environ.get("FARM_DYNAMIC_UPGRADE_RESERVE", "true").lower() not in ("0", "false", "no", "off")
# reserve 作物模式：cheapest=保证能种满的最低正收益作物；best=按当前策略推荐作物预算，更激进保收益但更保守升级。
RESERVE_CROP_MODE = os.environ.get("FARM_RESERVE_CROP_MODE", "cheapest").lower()
# 预留浇水预算：默认按“最多浇水次数 * 对应种子成本 * 30%”估算。
# 注意：MAX_WATER_PER_ROUND 必须先定义，否则模块导入时 RESERVE_WATER_SLOTS 会 NameError。
MAX_WATER_PER_ROUND = int(os.environ.get("FARM_MAX_WATER_PER_ROUND", "3"))
RESERVE_WATER_RATIO = float(os.environ.get("FARM_RESERVE_WATER_RATIO", "0.30"))
RESERVE_WATER_SLOTS = int(os.environ.get("FARM_RESERVE_WATER_SLOTS", str(MAX_WATER_PER_ROUND)))
# 种植储备：只给买种子用。默认 0，避免余额很低时因为保留储备而不播种。
SEED_RESERVE_MONEY = int(os.environ.get("FARM_SEED_RESERVE_MONEY", "0"))
SEED_BUDGET_RATIO = float(os.environ.get("FARM_SEED_BUDGET_RATIO", "1.0"))
MIN_SLEEP_SECONDS = 20
MAX_SLEEP_SECONDS = 900
READY_GRACE_SECONDS = 3
REQUEST_GAP = 0.25

# 自动浇水保护：浇水一颗菜只能一次；按“买分钟是否划算”判断，默认开启。
AUTO_WATER = os.environ.get("FARM_AUTO_WATER", "true").lower() not in ("0", "false", "no", "off")
MAX_WATER_PER_ROUND = int(os.environ.get("FARM_MAX_WATER_PER_ROUND", "3"))
MIN_WATER_REMAIN_SEC = int(os.environ.get("FARM_MIN_WATER_REMAIN_SEC", "60"))
MIN_BALANCE_AFTER_WATER = int(os.environ.get("FARM_MIN_BALANCE_AFTER_WATER", "80"))
WATER_REDUCE_RATIO = float(os.environ.get("FARM_WATER_REDUCE_RATIO", "0.30"))
WATER_VALUE_DISCOUNT = float(os.environ.get("FARM_WATER_VALUE_DISCOUNT", "0.85"))

# 升级保护
UPGRADE_SAFETY_BPS = 12000
MAX_AUTO_UPGRADE_LEVEL = int(os.environ.get("FARM_MAX_AUTO_UPGRADE_LEVEL", "3"))
UPGRADE_FAIL_COOLDOWN_ROUNDS = 8

# 新版优先金币/金币余额，旧版字段兜底
BALANCE_KEYS = (
    "farm_coin", "farm_coins", "farm_gold", "farm_money", "farm_balance",
    "farm_coin_balance", "farm_gold_balance", "farm_money_balance",
    "balance_farm_coin", "balance_farm_coins", "balance_farm_gold", "balance_farm_money",
    "nongchang_coin", "nongchang_gold", "field_coin", "field_gold",
    "balance_coin", "coin_balance", "coins", "coin", "gold", "balance_gold",
    "money", "cash", "balance",
    "balance_lobster", "lobster", "lobsters",
    "funds", "capital", "points", "score", "amount", "wallet", "asset", "assets"
)
SEED_COST_KEYS = (
    "seed_cost_farm_coin", "seed_farm_coin", "cost_farm_coin", "buy_price_farm_coin", "price_farm_coin",
    "seed_cost_farm_gold", "cost_farm_gold", "buy_price_farm_gold", "price_farm_gold",
    "seed_cost_coin", "seed_coin", "cost_coin", "buy_price_coin", "price_coin",
    "seed_cost_gold", "cost_gold", "buy_price_gold", "price_gold",
    "seed_cost", "cost", "buy_price", "price",
    "seed_cost_lobster"
)
YIELD_KEYS = (
    "yield_farm_coin", "harvest_farm_coin", "sell_farm_coin", "sell_price_farm_coin", "reward_farm_coin", "income_farm_coin",
    "yield_farm_gold", "harvest_farm_gold", "sell_farm_gold", "sell_price_farm_gold", "reward_farm_gold", "income_farm_gold",
    "yield_coin", "harvest_coin", "sell_coin", "sell_price_coin", "reward_coin", "income_coin",
    "yield_gold", "harvest_gold", "sell_price_gold", "reward_gold",
    "yield", "harvest", "sell_price", "reward", "income",
    "yield_lobster"
)
UPGRADE_COST_KEYS = (
    "next_upgrade_cost_coin", "upgrade_cost_coin", "level_up_cost_coin", "levelup_cost_coin", "cost_upgrade_coin",
    "next_upgrade_cost_gold", "upgrade_cost_gold", "level_up_cost_gold",
    "next_upgrade_cost", "upgrade_cost", "level_up_cost", "levelup_cost", "cost_upgrade", "cost",
    "next_upgrade_cost_lobster", "upgrade_cost_lobster", "level_up_cost_lobster", "levelup_cost_lobster", "cost_upgrade_lobster", "cost_lobster"
)
UPGRADE_TABLE_KEYS = (
    "upgrade_costs_coin", "upgrade_cost_coin", "upgrade_costs_gold", "upgrade_cost_gold",
    "upgrade_costs", "upgrade_costs_lobster"
)
MESSAGE_KEYS = {"message", "msg", "error", "detail", "reason", "toast", "note"}


# v6.4 dragon ↔ farm_gold arbitrage; default: enabled immediately after startup
ARBITRAGE_ENABLED = os.environ.get("FARM_ARBITRAGE_ENABLED", "true").lower() not in ("0", "false", "no", "off")
ARBITRAGE_STOCK_NAME = os.environ.get("FARM_ARBITRAGE_STOCK_NAME", "贩子死妈")
ARBITRAGE_STOCK_ID = os.environ.get("FARM_ARBITRAGE_STOCK_ID", "").strip()
ARBITRAGE_STATE_FILE = os.environ.get("FARM_ARBITRAGE_STATE_FILE", "/tmp/lobster_farm_bot_v6_4_arbitrage_state.json")
ARBITRAGE_START_NEXT_DAY = os.environ.get("FARM_ARBITRAGE_START_NEXT_DAY", "false").lower() not in ("0", "false", "no", "off")
ARBITRAGE_BUY_LOW_BPS = int(os.environ.get("FARM_ARBITRAGE_BUY_LOW_BPS", "120"))
ARBITRAGE_SELL_PULLBACK_BPS = int(os.environ.get("FARM_ARBITRAGE_SELL_PULLBACK_BPS", "180"))
ARBITRAGE_MIN_PROFIT_BPS = int(os.environ.get("FARM_ARBITRAGE_MIN_PROFIT_BPS", "80"))
ARBITRAGE_FORCE_SELL_HOUR = int(os.environ.get("FARM_ARBITRAGE_FORCE_SELL_HOUR", "23"))
ARBITRAGE_FORCE_SELL_MINUTE = int(os.environ.get("FARM_ARBITRAGE_FORCE_SELL_MINUTE", "45"))
ARBITRAGE_TRADE_FARM_GOLD = int(os.environ.get("FARM_ARBITRAGE_TRADE_FARM_GOLD", "0"))
ARBITRAGE_TRADE_RATIO = float(os.environ.get("FARM_ARBITRAGE_TRADE_RATIO", "0.35"))
ARBITRAGE_MIN_FARM_GOLD = int(os.environ.get("FARM_ARBITRAGE_MIN_FARM_GOLD", "100"))
ARBITRAGE_KEEP_FARM_GOLD = int(os.environ.get("FARM_ARBITRAGE_KEEP_FARM_GOLD", "0"))
ARBITRAGE_KLINE_PERIOD = os.environ.get("FARM_ARBITRAGE_KLINE_PERIOD", "tick")
ARBITRAGE_KLINE_LIMIT = int(os.environ.get("FARM_ARBITRAGE_KLINE_LIMIT", "240"))
ARBITRAGE_BUY_LOOKBACK_RATIO = float(os.environ.get("FARM_ARBITRAGE_BUY_LOOKBACK_RATIO", "0.55"))
ARBITRAGE_SELL_TREND_POINTS = int(os.environ.get("FARM_ARBITRAGE_SELL_TREND_POINTS", "3"))
ARBITRAGE_SELL_TREND_DROP_BPS = int(os.environ.get("FARM_ARBITRAGE_SELL_TREND_DROP_BPS", "20"))
ARBITRAGE_KEEP_PLANTING_RESERVE = os.environ.get("FARM_ARBITRAGE_KEEP_PLANTING_RESERVE", "true").lower() not in ("0", "false", "no", "off")
ARBITRAGE_LONG_MATURE_SECONDS = int(os.environ.get("FARM_ARBITRAGE_LONG_MATURE_SECONDS", "1800"))
ARBITRAGE_LONG_MATURE_RESERVE_RATIO = float(os.environ.get("FARM_ARBITRAGE_LONG_MATURE_RESERVE_RATIO", "0.35"))

last_balance = None
last_balance_path = None
estimated_balance = None
upgrade_fail_cooldown = {}


def cn_now():
    return datetime.now(CN_TZ)

def now_text():
    return cn_now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{now_text()}] {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def request_json(method, path, payload=None):
    url = BASE_URL + path
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Cookie": COOKIE,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Linux; Android; Termux) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Origin": "https://fanzisima.xyz",
        "Referer": "https://fanzisima.xyz/stocks/",
    }

    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
            return True, status, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        return False, e.code, data
    except Exception as e:
        return False, None, repr(e)


def short_result(data, limit=420):
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    text = text.replace("\n", " ")
    return text[:limit] + "..." if len(text) > limit else text


def walk_json(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k), v
            yield from walk_json(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield from walk_json(v, p)


def get_data(resp):
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp["data"]
    return {}


def num_value(obj, keys, default=0):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return default


def list_value(obj, keys):
    if not isinstance(obj, dict):
        return None, None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, list):
            return value, key
    return None, None


def extract_messages(data):
    msgs = []
    if isinstance(data, str):
        return [data[:220]] if data else []
    for path, key, value in walk_json(data):
        if key.lower() in MESSAGE_KEYS and isinstance(value, (str, int, float, bool)):
            text = str(value)
            if text and text not in msgs:
                msgs.append(text)
    return msgs[:5]


def extract_balance(data):
    if not isinstance(data, (dict, list)):
        return None
    candidates = []
    key_rank = {k: i for i, k in enumerate(BALANCE_KEYS)}
    for path, key, value in walk_json(data):
        lk = key.lower()
        if not isinstance(value, (int, float)):
            continue
        if lk in key_rank:
            candidates.append((key_rank[lk], path, value))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], 0 if "data" in x[1].lower() else 1))
    _, path, value = candidates[0]
    return path, int(value)


def balance_amount(data, fallback=0):
    bal = extract_balance(data)
    if bal:
        return int(bal[1])
    if estimated_balance is not None:
        return int(estimated_balance)
    return int(fallback or 0)


def init_estimated_balance():
    global estimated_balance, last_balance, last_balance_path
    raw = os.environ.get("FARM_INITIAL_BALANCE", "").strip()
    if raw.isdigit():
        estimated_balance = int(raw)
        last_balance = estimated_balance
        last_balance_path = "env.FARM_INITIAL_BALANCE"
        log(f"启用初始余额兜底：{estimated_balance}，用于接口不返回余额字段时继续补种")


def set_last_balance_from_state(state):
    global last_balance, last_balance_path, estimated_balance
    bal = extract_balance(state)
    if bal:
        last_balance_path, last_balance = bal[0], int(bal[1])
        estimated_balance = last_balance


def infer_balance_delta_from_messages(msgs):
    """从接口中文消息里推算农场金币变化。

    适配示例：
    - 收获火箭笋，毛产出40农场金币，缴税4，到手36
    - 种下了火箭笋等(产出40金币)，花了5农场金币
    - 浇水熊市苦瓜，花了1575农场金币
    """
    delta = 0
    for text in msgs or []:
        s = str(text)
        for m in re.finditer(r"到手\s*(\d+)", s):
            delta += int(m.group(1))
        for m in re.finditer(r"(?:获得|增加|奖励)\s*(\d+)\s*(?:农场金币|金币)", s):
            delta += int(m.group(1))
        for m in re.finditer(r"花了\s*(\d+)\s*(?:农场金币|金币)", s):
            delta -= int(m.group(1))
    return delta


def apply_balance_delta(delta):
    global estimated_balance, last_balance, last_balance_path
    if not delta:
        return
    base = estimated_balance if estimated_balance is not None else last_balance
    if base is None:
        base = 0
    estimated_balance = max(0, int(base) + int(delta))
    last_balance = estimated_balance
    last_balance_path = "estimated.message_delta"


def format_balance_change(new_balance):
    global last_balance, last_balance_path
    if new_balance is None:
        if estimated_balance is not None:
            return f"余额≈{estimated_balance}(消息推算)"
        return "余额=未识别"
    path, value = new_balance
    value = int(value)
    if last_balance is None or last_balance_path != path:
        last_balance = value
        last_balance_path = path
        return f"余额={value}({path})"
    delta = value - last_balance
    last_balance = value
    if delta > 0:
        return f"余额={value}({path}, +{delta})"
    if delta < 0:
        return f"余额={value}({path}, {delta})"
    return f"余额={value}({path}, 无变化)"


def log_action(action, plot_no, ok, status, data):
    msgs = extract_messages(data)
    bal = extract_balance(data)
    if bal:
        balance_text = format_balance_change(bal)
    else:
        delta = infer_balance_delta_from_messages(msgs)
        if ok and delta:
            apply_balance_delta(delta)
            balance_text = f"余额≈{estimated_balance}(消息推算 {delta:+d})"
        else:
            balance_text = format_balance_change(None)
    msg_text = "；".join(msgs) if msgs else "无接口消息"
    log(f"动作={action} 地块={plot_no:02d} ok={ok} HTTP={status} {balance_text} 消息={msg_text}")
    if not ok:
        log(f"失败详情 动作={action} 地块={plot_no:02d} 返回={short_result(data)}")


def farm_me(label="查询农场状态"):
    ok, status, data = request_json("GET", "/me")
    balance_text = format_balance_change(extract_balance(data))
    msg_text = "；".join(extract_messages(data)) or "无接口消息"
    log(f"{label} ok={ok} HTTP={status} {balance_text} 消息={msg_text}")
    if not ok:
        log(f"状态查询失败详情 返回={short_result(data)}")
    return ok, data


def harvest(plot_no):
    return request_json("POST", "/harvest", {"plot_no": plot_no})


def upgrade(plot_no):
    return request_json("POST", "/upgrade", {"plot_no": plot_no})


def water(plot_no):
    return request_json("POST", "/water", {"plot_no": plot_no})


def plant(plot_no, crop_key):
    return request_json("POST", "/plant", {"plot_no": plot_no, "crop_key": crop_key})


def decay_factor_bps(level, decay_bps):
    if not decay_bps:
        return 10000
    idx = max(0, min(int(level) - 1, len(decay_bps) - 1))
    return int(decay_bps[idx])


def effective_crop(crop, level, decay_bps, tax_bps, coin_price=1):
    factor = decay_factor_bps(level, decay_bps)
    base_grow = int(crop.get("grow_sec") or crop.get("growth_sec") or crop.get("duration_sec") or 0)

    base_seed = num_value(crop, SEED_COST_KEYS, 0)
    gross = num_value(crop, YIELD_KEYS, 0)

    if gross <= 0 and isinstance(crop.get("yield_lobster"), (int, float)):
        gross = int(crop.get("yield_lobster") * max(1, int(coin_price or 1)))

    grow_sec = max(1, int(round(base_grow * factor / 10000)))
    seed_cost = max(0, int(round(base_seed * factor / 10000)))
    net_harvest = int(round(gross * (10000 - tax_bps) / 10000))
    profit = net_harvest - seed_cost
    profit_per_hour = profit * 3600 / grow_sec if grow_sec > 0 else -10**18

    item = dict(crop)
    item.update({
        "effective_grow_sec": grow_sec,
        "effective_seed_cost_money": seed_cost,
        "effective_net_harvest_money": net_harvest,
        "effective_profit_money": profit,
        "profit_per_hour": profit_per_hour,
        "level_factor_bps": factor,
    })
    return item


def choose_best_crop(crops, level, balance, decay_bps, tax_bps, coin_price=1):
    """选择当前余额下买得起、且税后净收益为正的最优作物。

    v5 修复点：
    1. 补种不再套用升级储备 RESERVE_MONEY，避免有小额金币时一律不种。
    2. 先按买得起过滤，再按单位时间净利排序；买不起的贵作物不会参与竞争。
    3. 返回跳过原因，日志能区分“余额不足”和“无正收益”。
    """
    try:
        balance = int(balance or 0)
    except Exception:
        balance = 0

    seed_budget = max(0, int((balance - SEED_RESERVE_MONEY) * SEED_BUDGET_RATIO))
    candidates = []
    positive_count = 0
    cheapest_positive = None
    affordable_nonpositive = 0

    for crop in crops:
        e = effective_crop(crop, level, decay_bps, tax_bps, coin_price=coin_price)
        seed_cost = int(e["effective_seed_cost_money"])
        profit = int(e["effective_profit_money"])

        if profit > 0:
            positive_count += 1
            if cheapest_positive is None or seed_cost < int(cheapest_positive["effective_seed_cost_money"]):
                cheapest_positive = e

        if seed_cost > seed_budget:
            continue
        if profit <= 0:
            affordable_nonpositive += 1
            continue
        candidates.append(e)

    if not candidates:
        if positive_count <= 0:
            return None, f"无税后正收益作物 balance={balance} seed_budget={seed_budget}"
        if cheapest_positive is not None:
            return None, (
                f"余额不足：当前余额={balance} 种植预算={seed_budget} "
                f"最便宜正收益作物={cheapest_positive.get('name')} "
                f"种子={cheapest_positive['effective_seed_cost_money']} "
                f"净利={cheapest_positive['effective_profit_money']}"
            )
        return None, f"无可选作物 balance={balance} seed_budget={seed_budget} affordable_nonpositive={affordable_nonpositive}"

    candidates.sort(
        key=lambda x: (
            x["profit_per_hour"],
            x["effective_profit_money"],
            -x["effective_seed_cost_money"],
        ),
        reverse=True,
    )
    return candidates[0], None


def upgrade_cost_for_level(level, upgrade_costs, max_level=None):
    try:
        level = int(level)
    except Exception:
        return None
    if not isinstance(upgrade_costs, list) or not upgrade_costs:
        return None

    indexes = {level - 1, level}
    if max_level and len(upgrade_costs) == int(max_level) + 1:
        indexes.add(level + 1)

    candidates = []
    for idx in indexes:
        if 0 <= idx < len(upgrade_costs):
            try:
                value = int(upgrade_costs[idx])
                if value > 0:
                    candidates.append(value)
            except Exception:
                pass
    return max(candidates) if candidates else None


def plot_upgrade_cost(plot):
    for key in UPGRADE_COST_KEYS:
        value = plot.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value), key
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip()), key
    return None, None


def reserve_crop_for_level(crops, level, balance, decay_bps, tax_bps, coin_price=1):
    """返回动态保留金使用的作物。

    cheapest：选税后正收益里种子成本最低的，目标是确保下轮一定能种满。
    best：复用 choose_best_crop，目标是预留推荐作物预算。
    """
    if RESERVE_CROP_MODE == "best":
        best, _ = choose_best_crop(crops, level, balance, decay_bps, tax_bps, coin_price=coin_price)
        if best:
            return best

    cheapest = None
    for crop in crops or []:
        e = effective_crop(crop, level, decay_bps, tax_bps, coin_price=coin_price)
        if int(e.get("effective_profit_money") or 0) <= 0:
            continue
        if cheapest is None or int(e["effective_seed_cost_money"]) < int(cheapest["effective_seed_cost_money"]):
            cheapest = e
    return cheapest


def dynamic_upgrade_reserve(data, crops, plots, balance, upgrade_plot_no=None):
    """计算升级后仍需保留的钱：下轮种满全部地块 + 预留浇水。

    注意：这里计算的是农场金币 farm_gold，不是龙虾币。脚本中 lobster 字段只作为旧接口兼容兜底。
    """
    if not DYNAMIC_UPGRADE_RESERVE:
        return max(0, RESERVE_MONEY), "fixed_env"

    decay_bps = data.get("upgrade_decay_bps") or []
    tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", 0)) or 0)
    coin_price = int(data.get("coin_price") or data.get("lobster_coin_price") or 1)
    plot_map = {int(p.get("plot_no") or 0): p for p in plots or data.get("plots") or []}

    seed_total = 0
    seed_costs = []
    missing_levels = []
    for plot_no in PLOTS:
        p = plot_map.get(int(plot_no), {})
        level = int(p.get("plot_level") or 1)
        if upgrade_plot_no is not None and int(plot_no) == int(upgrade_plot_no):
            level += 1
        crop = reserve_crop_for_level(crops, level, balance, decay_bps, tax_bps, coin_price=coin_price)
        if not crop:
            missing_levels.append(level)
            continue
        cost = int(crop.get("effective_seed_cost_money") or 0)
        seed_total += cost
        seed_costs.append(cost)

    if not seed_costs:
        return max(0, RESERVE_MONEY), f"fallback_no_positive_crop fixed={RESERVE_MONEY}"

    water_slots = max(0, RESERVE_WATER_SLOTS if AUTO_WATER else 0)
    water_base = sorted(seed_costs, reverse=True)[:water_slots]
    water_total = int(round(sum(water_base) * max(0.0, RESERVE_WATER_RATIO)))
    reserve = max(0, seed_total + water_total, RESERVE_MONEY)
    detail = (
        f"dynamic seed_full={seed_total} water={water_total} "
        f"slots={water_slots} ratio={RESERVE_WATER_RATIO} mode={RESERVE_CROP_MODE}"
    )
    if missing_levels:
        detail += f" missing_positive_levels={sorted(set(missing_levels))}"
    return reserve, detail


def tick_upgrade_cooldowns():
    expired = []
    for plot_no, rounds in list(upgrade_fail_cooldown.items()):
        rounds -= 1
        if rounds <= 0:
            expired.append(plot_no)
        else:
            upgrade_fail_cooldown[plot_no] = rounds
    for plot_no in expired:
        upgrade_fail_cooldown.pop(plot_no, None)


def can_upgrade_plot(plot, data, balance, crops):
    if not AUTO_UPGRADE:
        return False, "AUTO_UPGRADE=false"

    plot_no = int(plot.get("plot_no") or 0)
    if plot_no in upgrade_fail_cooldown:
        return False, f"升级失败冷却中 剩余{upgrade_fail_cooldown[plot_no]}轮"

    level = int(plot.get("plot_level") or 1)
    max_level = int(data.get("upgrade_max_level") or 5)
    if level >= max_level:
        return False, "已满级"
    if level >= MAX_AUTO_UPGRADE_LEVEL:
        return False, f"达到自动升级上限 level={level} max_auto={MAX_AUTO_UPGRADE_LEVEL}"

    real_cost, real_cost_key = plot_upgrade_cost(plot)
    table, table_key = list_value(data, UPGRADE_TABLE_KEYS)
    table_cost = upgrade_cost_for_level(level, table or [], max_level=max_level)

    if real_cost is not None:
        cost = real_cost
        cost_source = real_cost_key
    elif table_cost is not None:
        cost = int(table_cost * UPGRADE_SAFETY_BPS / 10000)
        cost_source = f"{table_key}保守估算 raw={table_cost} safety_bps={UPGRADE_SAFETY_BPS}"
    else:
        return False, "无升级价格"

    decay_bps = data.get("upgrade_decay_bps") or []
    tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", 0)) or 0)
    coin_price = int(data.get("coin_price") or data.get("lobster_coin_price") or 1)

    after_balance = balance - cost
    plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))
    reserve_need, reserve_detail = dynamic_upgrade_reserve(data, crops, plots, balance, upgrade_plot_no=plot_no)
    if after_balance < reserve_need:
        return False, (
            f"余额不足以保留下轮种满+浇水预算 cost={cost} source={cost_source} "
            f"after={after_balance} need={reserve_need} {reserve_detail}"
        )

    return True, f"可升级 cost={cost} source={cost_source} after={after_balance} reserve_need={reserve_need} {reserve_detail}"


def is_ready(plot):
    state = str(plot.get("state") or "").lower()
    remain = int(plot.get("remain_sec") or 0)
    if state == "ready":
        return True
    if remain <= 0 and str(plot.get("status") or "").lower() == "growing":
        return True
    return False


def is_empty(plot):
    state = str(plot.get("state") or "").lower()
    status = str(plot.get("status") or "").lower()
    return state == "empty" or status == "empty" or not plot.get("crop_key")


def plot_crop_key(plot):
    return str(plot.get("crop_key") or plot.get("crop") or plot.get("seed_key") or "")


def crop_lookup(crops):
    mp = {}
    for c in crops or []:
        for k in (c.get("key"), c.get("crop_key"), c.get("id"), c.get("name")):
            if k is not None and str(k):
                mp[str(k)] = c
    return mp


def plot_was_watered(plot):
    for key in ("watered", "is_watered", "has_watered", "water_used", "watered_once", "isWatered", "hasWatered"):
        if key in plot:
            v = plot.get(key)
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return int(v) > 0
            if isinstance(v, str):
                return v.lower() in ("1", "true", "yes", "y", "已浇水")
    return False


def estimate_water_cost(plot, crop_eff):
    for key in ("water_cost", "watering_cost", "need_water_coin", "water_coin", "water_price"):
        v = plot.get(key)
        if isinstance(v, (int, float)) and int(v) > 0:
            return int(v), key
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip()), key
    remain = int(plot.get("remain_sec") or 0)
    grow = max(1, int(crop_eff.get("effective_grow_sec") or remain or 1))
    seed = int(crop_eff.get("effective_seed_cost_money") or 0)
    est = max(1, int(round(seed * max(1, remain) / grow))) if seed > 0 else None
    return est, "estimated_seed_ratio"


def best_profit_per_minute(crops, plots, balance, decay_bps, tax_bps, coin_price=1):
    best_ppm = 0.0
    for p in plots or []:
        level = int(p.get("plot_level") or 1)
        best, _ = choose_best_crop(crops, level, balance, decay_bps, tax_bps, coin_price=coin_price)
        if best:
            ppm = float(best.get("effective_profit_money", 0)) / max(1.0, float(best.get("effective_grow_sec", 1)) / 60.0)
            best_ppm = max(best_ppm, ppm)
    return best_ppm


def water_decisions(data, crops, plots, balance, decay_bps, tax_bps):
    if not AUTO_WATER or MAX_WATER_PER_ROUND <= 0:
        return []
    coin_price = int(data.get("coin_price") or data.get("lobster_coin_price") or 1)
    crop_map = crop_lookup(crops)
    market_ppm = best_profit_per_minute(crops, plots, balance, decay_bps, tax_bps, coin_price=coin_price)
    if market_ppm <= 0:
        return []
    decisions = []
    for plot in plots or []:
        plot_no = int(plot.get("plot_no") or 0)
        if plot_no not in PLOTS or is_empty(plot) or is_ready(plot):
            continue
        if plot_was_watered(plot):
            continue
        remain = int(plot.get("remain_sec") or 0)
        if remain < MIN_WATER_REMAIN_SEC:
            continue
        ck = plot_crop_key(plot)
        crop = crop_map.get(ck)
        if not crop:
            continue
        level = int(plot.get("plot_level") or 1)
        eff = effective_crop(crop, level, decay_bps, tax_bps, coin_price=coin_price)
        cost, cost_source = estimate_water_cost(plot, eff)
        if not cost or cost <= 0:
            continue
        reduce_sec = max(1, int(remain * WATER_REDUCE_RATIO))
        saved_minutes = reduce_sec / 60.0
        value = saved_minutes * market_ppm * WATER_VALUE_DISCOUNT
        roi = value - cost
        if balance - cost < MIN_BALANCE_AFTER_WATER or roi <= 0:
            continue
        decisions.append({"plot_no": plot_no, "crop_name": eff.get("name") or ck, "remain": remain, "cost": int(cost), "cost_source": cost_source, "saved_minutes": saved_minutes, "value": value, "roi": roi, "market_ppm": market_ppm})
    decisions.sort(key=lambda x: (x["roi"], x["saved_minutes"], -x["cost"]), reverse=True)
    return decisions[:MAX_WATER_PER_ROUND]


def maybe_auto_water(data, crops, plots, balance, decay_bps, tax_bps):
    watered = 0
    decisions = water_decisions(data, crops, plots, balance, decay_bps, tax_bps)
    if not decisions:
        return data, crops, decay_bps, tax_bps, balance, watered
    for d in decisions:
        plot_no = d["plot_no"]
        if balance - int(d["cost"]) < MIN_BALANCE_AFTER_WATER:
            continue
        log(f"动作=准备浇水 地块={plot_no:02d} 作物={d['crop_name']} 剩余={d['remain']}s 估算费用={d['cost']}({d['cost_source']}) 预计节省={d['saved_minutes']:.1f}分钟 分钟价值≈{d['market_ppm']:.2f} 折后价值≈{d['value']:.1f} ROI≈{d['roi']:.1f}")
        ok, status, resp = water(plot_no)
        log_action("浇水", plot_no, ok, status, resp)
        if ok:
            watered += 1
            balance = balance_amount(resp, balance)
    return data, crops, decay_bps, tax_bps, balance, watered


# ... file truncated ...