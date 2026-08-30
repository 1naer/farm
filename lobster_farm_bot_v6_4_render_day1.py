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
        # 兑换、奖励等如果直接写获得/增加，也计入。
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
        # 接口完全不给余额时，至少从本轮收获开始建立保守估算。
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

    # 新版优先金币成本/收益；旧版 lobster 字段兜底。
    base_seed = num_value(crop, SEED_COST_KEYS, 0)
    gross = num_value(crop, YIELD_KEYS, 0)

    # 如果只给了旧 lobster 收益且有 coin_price，可粗略折算成金币；如果新版已有 coin 字段则不会走这里。
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

    # 主目标：单位时间净收益；副目标：总净利；再副目标：便宜优先，避免余额少时挑过贵作物。
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
        time.sleep(REQUEST_GAP)
        ok2, state2 = farm_me(f"浇水后刷新 地块={plot_no:02d}")
        if ok2:
            data = get_data(state2)
            plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))
            crops, decay_bps, tax_bps, balance = refresh_runtime_values(data, crops, decay_bps, tax_bps, balance)
        if ok:
            watered += 1
        if watered >= MAX_WATER_PER_ROUND:
            break
    return data, crops, decay_bps, tax_bps, balance, watered


def summarize_strategy(data):
    crops = data.get("crops") or []
    decay_bps = data.get("upgrade_decay_bps") or []
    tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", 0)) or 0)
    balance = balance_amount(data)
    coin_price = int(data.get("coin_price") or data.get("lobster_coin_price") or 1)
    log(f"策略参数：余额={balance} 税率bps={tax_bps} coin_price={coin_price} 等级倍率={decay_bps} 自动升级={AUTO_UPGRADE} 动态升级保留={DYNAMIC_UPGRADE_RESERVE} 兜底储备={RESERVE_MONEY} 保留作物模式={RESERVE_CROP_MODE} 浇水预留={RESERVE_WATER_SLOTS}*{RESERVE_WATER_RATIO} 自动浇水={AUTO_WATER}")
    for level in sorted({int(p.get("plot_level") or 1) for p in data.get("plots", [])}):
        best, reason = choose_best_crop(crops, level, balance, decay_bps, tax_bps, coin_price=coin_price)
        if best:
            log(
                f"等级{level}推荐：{best.get('emoji','')}{best.get('name')}({best.get('key')}) "
                f"成熟={best['effective_grow_sec']}s 种子={best['effective_seed_cost_money']} "
                f"净收={best['effective_net_harvest_money']} 净利={best['effective_profit_money']} "
                f"约={int(best['profit_per_hour'])}/小时"
            )
        else:
            log(f"等级{level}暂无推荐：{reason}")


def refresh_runtime_values(data, old_crops=None, old_decay=None, old_tax=0, old_balance=0):
    crops = data.get("crops") or old_crops or []
    decay_bps = data.get("upgrade_decay_bps") or old_decay or []
    tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", old_tax)) or old_tax or 0)
    balance = balance_amount(data, fallback=old_balance)
    return crops, decay_bps, tax_bps, balance




def request_stocks_json(method, path, payload=None, query=None):
    if query:
        path = path + ("&" if "?" in path else "?") + urllib.parse.urlencode(query)
    url = STOCKS_API_BASE_URL + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Cookie": COOKIE, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0 (Linux; Android; Termux) AppleWebKit/537.36 Chrome/126 Safari/537.36", "Origin": "https://fanzisima.xyz", "Referer": "https://fanzisima.xyz/stocks/"}
    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try: data = json.loads(raw)
            except Exception: data = raw
            return True, resp.getcode(), data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try: data = json.loads(raw)
        except Exception: data = raw
        return False, e.code, data
    except Exception as e:
        return False, None, repr(e)

def arb_today():
    return cn_now().strftime("%Y-%m-%d")

def arb_num(v, default=0):
    try:
        if v is None or v == "": return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default

def arb_find(obj, keys):
    keys=set(keys)
    if isinstance(obj, dict):
        for k,v in obj.items():
            if k in keys:
                n=arb_num(v, None)
                if n is not None: return n
        for v in obj.values():
            n=arb_find(v, keys)
            if n is not None: return n
    elif isinstance(obj, list):
        for v in obj:
            n=arb_find(v, keys)
            if n is not None: return n
    return None

def load_arbitrage_state():
    st={"initialized_on": arb_today(), "phase": "wait_buy", "active_date": None, "completed_date": None, "stock_id": ARBITRAGE_STOCK_ID or None, "buy_price": None, "peak_price": None, "dragon_amount": 0}
    try:
        p=Path(ARBITRAGE_STATE_FILE)
        if p.exists():
            old=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict): st.update(old)
    except Exception as e:
        log(f"套利状态读取失败：{e}")
    return st

def save_arbitrage_state(st):
    try: Path(ARBITRAGE_STATE_FILE).write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e: log(f"套利状态保存失败：{e}")

def resolve_arbitrage_stock_id(st):
    if st.get("stock_id"): return st.get("stock_id")
    ok,status,resp=request_stocks_json("GET", "/market")
    if not ok:
        log(f"套利=跳过 获取市场失败 status={status} resp={short_result(resp)}"); return None
    data=get_data(resp)
    items=data if isinstance(data, list) else (data.get("items") or data.get("stocks") or data.get("market") or [])
    for item in items or []:
        if not isinstance(item, dict): continue
        name=str(item.get("name") or item.get("symbol") or item.get("code") or "")
        if ARBITRAGE_STOCK_NAME in name or name in ARBITRAGE_STOCK_NAME:
            sid=item.get("id") or item.get("stock_id")
            if sid:
                st["stock_id"]=sid; save_arbitrage_state(st); log(f"套利=已解析股票 {ARBITRAGE_STOCK_NAME} stock_id={sid}"); return sid
    log(f"套利=跳过 未找到股票名 {ARBITRAGE_STOCK_NAME}，可设置 FARM_ARBITRAGE_STOCK_ID"); return None

def fetch_arbitrage_prices(stock_id):
    ok,status,resp=request_stocks_json("GET", "/klines", query={"stock_id": stock_id, "period": ARBITRAGE_KLINE_PERIOD, "limit": ARBITRAGE_KLINE_LIMIT})
    if not ok:
        log(f"套利=跳过 获取K线失败 status={status} resp={short_result(resp)}"); return None
    data=get_data(resp)
    rows=data if isinstance(data, list) else (data.get("data") or data.get("items") or data.get("klines") or [])
    prices=[]
    for r in rows or []:
        p = arb_num((r.get("close") or r.get("price") or r.get("last") or r.get("c")) if isinstance(r, dict) else (r[-1] if isinstance(r,(list,tuple)) and r else None), None)
        if p and p>0: prices.append(p)
    if not prices: return None
    low=min(prices)
    low_idx=prices.index(low)
    if low_idx>0 and low_idx < len(prices)-1:
        prev_price=prices[low_idx-1]
        post_prices=prices[low_idx+1:]
        if post_prices:
            rebound=max(post_prices)
            drawdown_bps=(prev_price-low)*10000.0/max(prev_price,1)
            stable_after=post_prices[:min(len(post_prices), max(1, int(ARBITRAGE_SELL_TREND_POINTS)))]
            if drawdown_bps >= max(1, int(os.environ.get("FARM_ARBITRAGE_SETTLEMENT_DIP_DROP_BPS", "800"))) and all(x>=low for x in stable_after):
                return {"current": prices[-1], "low": low, "settlement_low": low, "settlement_idx": low_idx, "high": max(prices), "prices": prices}
    split=max(1, int(len(prices)*ARBITRAGE_BUY_LOOKBACK_RATIO))
    prev_window=prices[:split] if len(prices)>1 else prices
    return {"current": prices[-1], "low": low, "prev_low": min(prev_window), "high": max(prices), "prices": prices}

def wallet_status():
    ok,status,resp=request_stocks_json("GET", "/wallet")
    if not ok: return False,status,resp,0,0
    farm_gold=arb_find(resp, ("farm_gold","farm_gold_balance","farm_gold_units","balance_farm_gold")) or 0
    dragon=arb_find(resp, ("dragon","dragon_units","dragon_balance_units","dragon_available_units")) or 0
    return True,status,resp,farm_gold,dragon

def exchange_currency(source, target, amount):
    import uuid
    payload={"source_currency": source, "target_currency": target, "amount": str(int(amount))}
    ok,status,quote=request_stocks_json("POST", "/wallet/exchange/quote", payload=payload)
    if not ok:
        log(f"套利兑换=报价失败 {source}->{target} amount={amount} status={status} resp={short_result(quote)}"); return False, quote
    
    qid = quote.get("data", {}).get("quote_id")
    if not qid:
        log(f"套利兑换=未获取到quote_id {source}->{target} resp={short_result(quote)}"); return False, quote
        
    idem_key = str(uuid.uuid4())
    commit_payload = {"quote_id": qid, "idempotency_key": idem_key}
    
    ok2,status2,resp2=request_stocks_json("POST", "/wallet/exchange/commit", payload=commit_payload)
    if not ok2:
        log(f"套利兑换=提交失败 {source}->{target} amount={amount} status={status2} resp={short_result(resp2)}"); return False, resp2
    log(f"套利兑换=成功 {source}->{target} amount={amount} resp={short_result(resp2)}"); return True, resp2

def should_force_sell_now():
    n=cn_now(); return (n.hour,n.minute) >= (ARBITRAGE_FORCE_SELL_HOUR, ARBITRAGE_FORCE_SELL_MINUTE)

def arbitrage_planting_reserve(data=None):
    if not ARBITRAGE_KEEP_PLANTING_RESERVE:
        return max(0, int(ARBITRAGE_KEEP_FARM_GOLD or 0)), "fixed_only"
    try:
        if not data:
            ok, state = farm_me("套利种菜储备检查")
            data = get_data(state) if ok else {}
        plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))
        crops = data.get("crops") or []
        decay_bps = data.get("upgrade_decay_bps") or []
        tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", 0)) or 0)
        bal = balance_amount(data)
        reserve, detail = dynamic_upgrade_reserve(data, crops, plots, bal)
        remains=[int(p.get("remain_sec") or 0) for p in plots if int(p.get("plot_no") or 0) in PLOTS and (not is_empty(p)) and (not is_ready(p)) and int(p.get("remain_sec") or 0)>0]
        next_remain=min(remains) if remains else 0
        if next_remain >= ARBITRAGE_LONG_MATURE_SECONDS:
            reserve=int(reserve*ARBITRAGE_LONG_MATURE_RESERVE_RATIO)
            detail += f" long_mature={next_remain}s reserve_ratio={ARBITRAGE_LONG_MATURE_RESERVE_RATIO}"
        reserve=max(int(ARBITRAGE_KEEP_FARM_GOLD or 0), int(reserve))
        return reserve, detail
    except Exception as e:
        reserve=max(int(ARBITRAGE_KEEP_FARM_GOLD or 0), int(RESERVE_MONEY or 0))
        return reserve, f"fallback err={e}"

def is_recent_downtrend(prices):
    n=max(2, int(ARBITRAGE_SELL_TREND_POINTS))
    if not prices or len(prices)<n:
        return False
    recent=prices[-n:]
    total_drop_bps=(recent[0]-recent[-1])*10000.0/max(recent[0],1)
    non_increasing=all(recent[i] >= recent[i+1] for i in range(len(recent)-1))
    return non_increasing and total_drop_bps >= ARBITRAGE_SELL_TREND_DROP_BPS

def maybe_run_arbitrage(stage="round"):
    if not ARBITRAGE_ENABLED: return
    st=load_arbitrage_state(); today=arb_today()
    if ARBITRAGE_START_NEXT_DAY and st.get("initialized_on")==today and not st.get("active_date"):
        log("套利=等待 从启动后的第二个自然日开始执行；如需启动即启用，设置 FARM_ARBITRAGE_START_NEXT_DAY=false"); save_arbitrage_state(st); return
    if st.get("completed_date")==today:
        log("套利=跳过 今日完整买卖已完成"); return
    if st.get("active_date")!=today:
        st.update({"active_date": today, "phase": "wait_buy", "buy_price": None, "peak_price": None, "dragon_amount": 0}); save_arbitrage_state(st)
    sid=resolve_arbitrage_stock_id(st)
    if not sid: return
    prices=fetch_arbitrage_prices(sid)
    if not prices: return
    cur,low,high=prices["current"],prices["low"],prices["high"]
    prev_low=prices.get("prev_low", low)
    settlement_low=prices.get("settlement_low")
    price_list=prices.get("prices") or []
    phase=st.get("phase","wait_buy")
    log(f"套利观察 stage={stage} phase={phase} stock_id={sid} cur={cur:.6g} low={low:.6g} prev_low={prev_low:.6g} settlement_low={settlement_low} high={high:.6g} cn_time={now_text()}")
    ok,status,wallet,farm_gold,dragon=wallet_status()
    if not ok:
        log(f"套利=跳过 钱包失败 status={status} resp={short_result(wallet)}"); return
    if phase=="wait_buy":
        buy_anchor=settlement_low if settlement_low else prev_low
        buy_line=buy_anchor*(1+ARBITRAGE_BUY_LOW_BPS/10000.0)
        if cur<=buy_line and not should_force_sell_now():
            keep_gold, keep_detail = arbitrage_planting_reserve()
            spend=ARBITRAGE_TRADE_FARM_GOLD or int(max(0,farm_gold-keep_gold)*ARBITRAGE_TRADE_RATIO)
            spend=min(spend, int(max(0,farm_gold-keep_gold)))
            log(f"套利买入预算 farm_gold={farm_gold} keep_gold={keep_gold} spend={spend} reserve={keep_detail}")
            if spend>=ARBITRAGE_MIN_FARM_GOLD:
                ok2,_=exchange_currency("farm_gold","dragon",spend)
                if ok2:
                    st.update({"phase":"holding_dragon","buy_price":cur,"peak_price":cur})
                    ok3,_,_,_,d=wallet_status()
                    if ok3: st["dragon_amount"]=int(d)
                    save_arbitrage_state(st)
            else: log(f"套利=跳过 买入金额不足 spend={spend} farm_gold={farm_gold} keep_gold={keep_gold}")
        else: log(f"套利=等待低点 当前={cur:.6g} 买线={buy_line:.6g}")
    elif phase=="holding_dragon":
        peak=max(arb_num(st.get("peak_price"),cur),cur); st["peak_price"]=peak
        buy_price=arb_num(st.get("buy_price"),cur)
        pullback_line=peak*(1-ARBITRAGE_SELL_PULLBACK_BPS/10000.0); profit_line=buy_price*(1+ARBITRAGE_MIN_PROFIT_BPS/10000.0); force=should_force_sell_now()
        downtrend=is_recent_downtrend(price_list)
        if force or (cur<=pullback_line and cur>=profit_line and downtrend):
            sell_amount=int(dragon or st.get("dragon_amount") or 0)
            if sell_amount>0:
                ok2,_=exchange_currency("dragon","farm_gold",sell_amount)
                if ok2: st.update({"phase":"completed","completed_date":today}); save_arbitrage_state(st)
            else:
                log("套利=持仓状态但钱包无 dragon，标记今日完成"); st.update({"phase":"completed","completed_date":today}); save_arbitrage_state(st)
        else:
            save_arbitrage_state(st); log(f"套利=持仓等待 回落线={pullback_line:.6g} 最低止盈线={profit_line:.6g} downtrend={downtrend} force={force}")

def run_one_round(round_no):
    log(f"========== 第 {round_no} 轮开始 ==========")
    tick_upgrade_cooldowns()
    ok, state = farm_me("轮前状态")
    if not ok:
        return MAX_SLEEP_SECONDS

    data = get_data(state)
    set_last_balance_from_state(state)
    crops = data.get("crops") or []
    plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))
    decay_bps = data.get("upgrade_decay_bps") or []
    tax_bps = int(data.get("favor_effective_tax_bps", data.get("harvest_tax_bps", 0)) or 0)
    balance = balance_amount(data)
    upgraded_count = 0

    summarize_strategy(data)
    maybe_run_arbitrage("round_start")

    # 1) 只收成熟地
    for plot in plots:
        plot_no = int(plot.get("plot_no") or 0)
        if plot_no not in PLOTS:
            continue
        if is_ready(plot):
            ok, status, resp = harvest(plot_no)
            log_action("收获", plot_no, ok, status, resp)
            time.sleep(REQUEST_GAP)
            ok2, state2 = farm_me(f"收获后刷新 地块={plot_no:02d}")
            if ok2:
                data = get_data(state2)
                crops, decay_bps, tax_bps, balance = refresh_runtime_values(data, crops, decay_bps, tax_bps, balance)

    # 2) 重新获取状态，决定升级和播种
    ok, state = farm_me("收获阶段后状态")
    if ok:
        data = get_data(state)
        plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))
        crops, decay_bps, tax_bps, balance = refresh_runtime_values(data, crops, decay_bps, tax_bps, balance)

    # 3) 对仍在生长的作物做 ROI 自动浇水：一菜一次，按“买到的分钟是否比当前最优种植收益更划算”判断。
    data, crops, decay_bps, tax_bps, balance, watered_count = maybe_auto_water(data, crops, plots, balance, decay_bps, tax_bps)
    if watered_count:
        plots = sorted(data.get("plots") or [], key=lambda p: int(p.get("plot_no") or 0))

    # 4) 空地升级/补种
    for plot in plots:
        plot_no = int(plot.get("plot_no") or 0)
        if plot_no not in PLOTS:
            continue
        if not is_empty(plot):
            continue

        if upgraded_count < MAX_UPGRADE_PER_ROUND:
            allowed, reason = can_upgrade_plot(plot, data, balance, crops)
            if allowed:
                ok, status, resp = upgrade(plot_no)
                log_action("升级", plot_no, ok, status, resp)
                time.sleep(REQUEST_GAP)
                if ok:
                    upgraded_count += 1
                else:
                    upgrade_fail_cooldown[plot_no] = UPGRADE_FAIL_COOLDOWN_ROUNDS
                ok2, state2 = farm_me(f"升级后刷新 地块={plot_no:02d}")
                if ok2:
                    data = get_data(state2)
                    plots_now = {int(p.get("plot_no") or 0): p for p in data.get("plots") or []}
                    plot = plots_now.get(plot_no, plot)
                    crops, decay_bps, tax_bps, balance = refresh_runtime_values(data, crops, decay_bps, tax_bps, balance)
            elif "已满级" not in reason:
                log(f"动作=跳过升级 地块={plot_no:02d} 原因={reason}")

        level = int(plot.get("plot_level") or 1)
        coin_price = int(data.get("coin_price") or data.get("lobster_coin_price") or 1)
        best, skip_reason = choose_best_crop(crops, level, balance, decay_bps, tax_bps, coin_price=coin_price)
        if not best:
            log(f"动作=跳过补种 地块={plot_no:02d} 等级={level} 原因={skip_reason} 当前余额={balance}")
            continue

        ok, status, resp = plant(plot_no, best["key"])
        log_action(
            f"补种-{best.get('name')}({best['key']}) 等级={level} 预计成熟={best['effective_grow_sec']}s 预计净利={best['effective_profit_money']}",
            plot_no,
            ok,
            status,
            resp,
        )
        time.sleep(REQUEST_GAP)
        ok2, state2 = farm_me(f"补种后刷新 地块={plot_no:02d}")
        if ok2:
            data = get_data(state2)
            crops, decay_bps, tax_bps, balance = refresh_runtime_values(data, crops, decay_bps, tax_bps, balance)

    ok, final_state = farm_me("轮后状态")
    final_data = get_data(final_state) if ok else data
    maybe_run_arbitrage("round_end")
    sleep_seconds = calc_next_sleep(final_data)
    log(f"========== 第 {round_no} 轮结束；下次巡检约 {sleep_seconds}s 后 ==========")
    return sleep_seconds


def calc_next_sleep(data):
    remains = []
    for p in data.get("plots") or []:
        if int(p.get("plot_no") or 0) not in PLOTS:
            continue
        if is_empty(p) or is_ready(p):
            return MIN_SLEEP_SECONDS
        remain = int(p.get("remain_sec") or 0)
        if remain > 0:
            remains.append(remain)

    if not remains:
        return MIN_SLEEP_SECONDS
    return max(MIN_SLEEP_SECONDS, min(min(remains) + READY_GRACE_SECONDS, MAX_SLEEP_SECONDS))


def main():
    log("龙虾农场智能挂机脚本 v6.4 启动：动态升级保留金/ROI自动浇水/启动即启用低买高位回落套利/Render适配")
    init_estimated_balance()
    log(
        f"配置：地块=1~12 自动升级={AUTO_UPGRADE} 每轮最多升级={MAX_UPGRADE_PER_ROUND} "
        f"自动升级上限={MAX_AUTO_UPGRADE_LEVEL} 动态升级保留={DYNAMIC_UPGRADE_RESERVE} 兜底储备={RESERVE_MONEY} 种植储备={SEED_RESERVE_MONEY} "
        f"自动浇水={AUTO_WATER} 每轮最多浇水={MAX_WATER_PER_ROUND} 浇水后最低余额={MIN_BALANCE_AFTER_WATER} 日志={LOG_FILE} 时区={FARM_TIMEZONE}"
    )
    if not COOKIE or "fz_lottery=" not in COOKIE:
        log("警告：COOKIE 看起来未设置。云服务器建议设置环境变量 FARM_COOKIE。")

    round_no = 1
    while True:
        try:
            sleep_seconds = run_one_round(round_no)
        except KeyboardInterrupt:
            log("收到 Ctrl+C，脚本退出")
            break
        except Exception as e:
            log(f"本轮发生未捕获异常：{repr(e)}")
            sleep_seconds = 120

        round_no += 1
        log(f"等待 {sleep_seconds} 秒后进入下一轮")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
