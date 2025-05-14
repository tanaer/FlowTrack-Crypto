import os
import re
import random
import json
from datetime import datetime
from typing import List, Dict
from ..data.binance_client import get_klines_data

SIM_RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'sim_results')
SIM_RESULTS_DIR = os.path.abspath(SIM_RESULTS_DIR)

if not os.path.exists(SIM_RESULTS_DIR):
    os.makedirs(SIM_RESULTS_DIR)

# 现货手续费（USDT/币对，挂单/吃单）
SPOT_MAKER_FEE = 0.075  # 单位: USDT
SPOT_TAKER_FEE = 0.075  # 单位: USDT
# U本位合约手续费（百分比）
FUT_MAKER_FEE = 0.0002  # 0.02%
FUT_TAKER_FEE = 0.0005  # 0.05%


def parse_ai_strategy(md_text: str) -> List[Dict]:
    """
    解析AI分析Markdown，提取每个交易对的策略建议。
    返回：[{symbol, type, direction, entry, stop, take, position, ...}]
    """
    # 只提取唯一锚点之间的表格
    table_match = re.search(r'\[STRATEGY_TABLE_BEGIN\](.*?)\[STRATEGY_TABLE_END\]', md_text, re.S)
    if not table_match:
        return []
    table = table_match.group(1)
    # 只提取标准表头后的内容
    pattern = re.compile(r'\|\s*([A-Z0-9]+)\s*\|\s*(现货|合约)\s*\|\s*(做多|做空|观望)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.\%]+)\s*\|')
    results = []
    for m in pattern.finditer(table):
        symbol, typ, direction, entry, stop, take, pos = m.groups()
        usdt_amt = random.randint(2500, 15000)
        results.append({
            'symbol': symbol,
            'type': typ,
            'direction': direction,
            'entry': float(entry),
            'stop': float(stop),
            'take': float(take),
            'position': pos,
            'usdt_amt': usdt_amt,
            'open_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'open',
            'close_time': None,
            'close_price': None,
            'pnl': None,
            'fee': None
        })
    return results


def save_sim_orders(orders: List[Dict]):
    """保存本轮模拟订单到 sim_results/ 目录"""
    filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '_orders.json'
    filepath = os.path.join(SIM_RESULTS_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)
    return filepath


def load_open_orders() -> List[Dict]:
    """加载所有未平仓模拟订单"""
    orders = []
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_orders.json'):
            with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                batch = json.load(f)
                for o in batch:
                    if o['status'] == 'open':
                        orders.append(o)
    return orders


def backtest_open_orders():
    """
    对所有未平仓模拟订单进行回测：
    - 每分钟调用一次，拉取最新价格，判断是否止盈/止损/到期
    - 平仓后记录盈亏、手续费、状态
    """
    open_orders = load_open_orders()
    closed_orders = []
    for order in open_orders:
        symbol = order['symbol']
        typ = order['type']
        direction = order['direction']
        entry = order['entry']
        stop = order['stop']
        take = order['take']
        usdt_amt = order['usdt_amt']
        # 获取最新价格（现货/合约均用最新收盘价）
        klines = get_klines_data(symbol, interval='1m', limit=2, is_futures=(typ == '合约'))
        if not klines:
            continue
        latest_price = klines[-1]['close']
        # 判断是否止盈/止损
        close_reason = None
        if direction == '做多':
            if latest_price >= take:
                close_price = take
                close_reason = '止盈'
            elif latest_price <= stop:
                close_price = stop
                close_reason = '止损'
            else:
                continue  # 未触发
        elif direction == '做空':
            if latest_price <= take:
                close_price = take
                close_reason = '止盈'
            elif latest_price >= stop:
                close_price = stop
                close_reason = '止损'
            else:
                continue
        else:
            continue  # 观望不下单
        # 计算盈亏
        if typ == '现货':
            qty = usdt_amt / entry
            pnl = (close_price - entry) * qty if direction == '做多' else (entry - close_price) * qty
            fee = SPOT_TAKER_FEE * 2  # 买卖各一次
        else:  # 合约
            qty = usdt_amt / entry
            pnl = (close_price - entry) * qty if direction == '做多' else (entry - close_price) * qty
            fee = usdt_amt * (FUT_TAKER_FEE * 2)  # 开平各一次
        order['status'] = 'closed'
        order['close_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        order['close_price'] = close_price
        order['pnl'] = round(pnl - fee, 4)
        order['fee'] = round(fee, 4)
        order['close_reason'] = close_reason
        closed_orders.append(order)
    # 保存已平仓订单
    if closed_orders:
        filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '_closed.json'
        filepath = os.path.join(SIM_RESULTS_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(closed_orders, f, ensure_ascii=False, indent=2)
    # 更新原订单文件（可选：实现订单状态更新）
    return closed_orders


def stat_sim_results():
    """统计所有已平仓订单的胜率、盈亏等"""
    win, total, profit, loss = 0, 0, 0, 0
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_closed.json'):
            with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                orders = json.load(f)
                for o in orders:
                    total += 1
                    if o['pnl'] > 0:
                        win += 1
                        profit += o['pnl']
                    else:
                        loss += o['pnl']
    win_rate = win / total if total else 0
    return {
        'total': total,
        'win': win,
        'win_rate': round(win_rate, 4),
        'profit': round(profit, 2),
        'loss': round(loss, 2),
        'net': round(profit + loss, 2)
    }


def get_sim_orders_24h() -> str:
    """获取最近24小时所有模拟订单（开/平仓），返回markdown文本"""
    now = datetime.now()
    orders = []
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_orders.json') or fname.endswith('_closed.json'):
            with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                batch = json.load(f)
                for o in batch:
                    t = o.get('open_time') or o.get('close_time')
                    if t:
                        t_dt = datetime.strptime(t, '%Y-%m-%d %H:%M:%S')
                        if (now - t_dt).total_seconds() <= 86400:
                            orders.append(o)
    if not orders:
        return '最近24小时无模拟订单记录。'
    # 按时间排序
    orders.sort(key=lambda x: x.get('open_time') or x.get('close_time'))
    # 输出为markdown表格
    md = ['\n**最近24小时模拟盘明细**\n',
          '| 时间 | 交易对 | 类型 | 方向 | 入场 | 止损 | 止盈 | 状态 | 平仓价 | 盈亏 | 原因 |',
          '|------|--------|------|------|------|------|------|------|--------|------|------|']
    for o in orders:
        md.append(f"| {o.get('open_time','')} | {o['symbol']} | {o['type']} | {o['direction']} | {o['entry']} | {o['stop']} | {o['take']} | {o['status']} | {o.get('close_price','')} | {o.get('pnl','')} | {o.get('close_reason','')} |")
    return '\n'.join(md)

# 预留：回测、平仓、统计等接口 