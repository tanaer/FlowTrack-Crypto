import os
import re
import random
import json
import uuid
from datetime import datetime
from typing import List, Dict
from ..data.binance_client import get_klines_data

SIM_RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'sim_results')
SIM_RESULTS_DIR = os.path.abspath(SIM_RESULTS_DIR)

if not os.path.exists(SIM_RESULTS_DIR):
    os.makedirs(SIM_RESULTS_DIR)

# 现货手续费（百分比）
SPOT_MAKER_FEE = 0.075  # 0.075%
SPOT_TAKER_FEE = 0.075  # 0.075%
# 合约手续费（百分比）
FUT_MAKER_FEE = 0.0002  # 0.02%
FUT_TAKER_FEE = 0.0005  # 0.05%


def parse_ai_strategy(md_text: str) -> List[Dict]:
    """
    解析AI分析Markdown，提取每个交易对的策略建议。
    返回：[{symbol, type, direction, entry, stop, take, position, ..., order_id}]
    """
    # 提取表格内容（修正了正则表达式）
    table_match = re.search(r'\[STRATEGY_TABLE_BEGIN\](.*?)\[STRATEGY_TABLE_END\]', md_text, re.S)
    if not table_match:
        return []
    table = table_match.group(1)
    
    # 解析表格行（修正了正则表达式）
    pattern = re.compile(r'\|\s*([A-Z0-9]+)\s*\|\s*(现货|合约)\s*\|\s*(做多|做空|观望)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.]+)\s*\|\s*([\d\.\%]+)\s*\|')
    results = []
    for m in pattern.finditer(table):
        symbol, typ, direction, entry, stop, take, pos = m.groups()
        # 随机分配资金
        usdt_amt = random.randint(2500, 15000)
        results.append({
            'order_id': uuid.uuid4().hex,  # 添加唯一订单ID
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
            'fee': None,
            'close_reason': None
        })
    return results


def save_sim_orders(orders: List[Dict]):
    """保存本轮模拟订单到 sim_results/ 目录"""
    if not orders:
        return None
    filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '_orders.json'
    filepath = os.path.join(SIM_RESULTS_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)
    return filepath


def load_open_orders() -> List[Dict]:
    """
    加载所有未平仓模拟订单。
    通过订单ID过滤出未平仓的订单，避免重复处理已平仓订单。
    """
    # 获取所有已平仓订单的ID集合
    closed_order_ids = set()
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_closed.json'):
            try:
                with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                    closed_batch = json.load(f)
                    for closed_order in closed_batch:
                        if 'order_id' in closed_order:
                            closed_order_ids.add(closed_order['order_id'])
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error reading closed orders file {fname}: {e}")
                continue

    # 加载未平仓订单（排除已在closed_order_ids中的订单）
    open_orders = []
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_orders.json'):
            try:
                with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                    order_batch = json.load(f)
                    for order in order_batch:
                        # 只加载包含ID、状态为open且未被平仓的订单
                        if 'order_id' in order and \
                           order.get('status') == 'open' and \
                           order['order_id'] not in closed_order_ids:
                            open_orders.append(order)
                        # 处理早期无ID的订单（向后兼容）
                        elif 'order_id' not in order and order.get('status') == 'open':
                            # 对于无ID的订单，添加一个ID并添加到open_orders
                            order['order_id'] = uuid.uuid4().hex
                            open_orders.append(order)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error reading open orders file {fname}: {e}")
                continue
    return open_orders


def backtest_open_orders():
    """
    对所有未平仓模拟订单进行回测：
    - 获取每个交易对的最新K线数据（高低价）
    - 判断每个未平仓订单是否触发止盈/止损
    - 触发后立即平仓并记录时间、价格、盈亏
    """
    open_orders_list = load_open_orders()
    if not open_orders_list:
        return []
    
    closed_orders_batch = []
    
    # 按交易对和交易类型分组，减少API调用
    symbol_type_orders = {}
    for order in open_orders_list:
        symbol = order['symbol']
        typ = order['type']
        key = (symbol, typ == '合约')
        if key not in symbol_type_orders:
            symbol_type_orders[key] = []
        symbol_type_orders[key].append(order)
    
    # 对每个交易对获取最新K线并处理订单
    for (symbol, is_futures), orders_in_group in symbol_type_orders.items():
        # 获取最新的K线数据
        klines = get_klines_data(symbol, interval='1m', limit=5, is_futures=is_futures)
        if not klines:
            continue
        
        # 按时间排序，确保使用最新K线
        sorted_klines = sorted(klines, key=lambda x: x['timestamp'])
        latest_kline = sorted_klines[-1]
        high = latest_kline['high']
        low = latest_kline['low']
        kline_close_time = latest_kline['close_time']
        
        # 处理该交易对的所有订单
        for order in orders_in_group:
            direction = order['direction']
            if direction == '观望':
                continue
                
            entry = order['entry']
            stop = order['stop']
            take = order['take']
            usdt_amt = order['usdt_amt']
            
            # 判断是否触发止盈/止损
            close_price = None
            close_reason = None
            
            # 做多订单
            if direction == '做多':
                # 先检查止损，再检查止盈
                if low <= stop:
                    close_price = stop
                    close_reason = '止损'
                elif high >= take:
                    close_price = take
                    close_reason = '止盈'
            # 做空订单
            elif direction == '做空':
                # 先检查止损，再检查止盈
                if high >= stop:
                    close_price = stop
                    close_reason = '止损'
                elif low <= take:
                    close_price = take
                    close_reason = '止盈'
            
            # 如果未触发平仓条件，继续持有
            if not close_price:
                continue
                
            # 计算盈亏和手续费
            qty = usdt_amt / entry
            pnl = (close_price - entry) * qty if direction == '做多' else (entry - close_price) * qty
            
            # 计算手续费（修正了现货手续费计算）
            if is_futures:
                fee = usdt_amt * (FUT_TAKER_FEE * 2)  # 开平各一次，0.05% * 2
            else:  # 现货
                fee = usdt_amt * (SPOT_TAKER_FEE / 100) * 2  # 0.075% * 2
            
            # 更新订单信息
            order['status'] = 'closed'
            order['close_time'] = kline_close_time
            order['close_price'] = close_price
            order['pnl'] = round(pnl - fee, 4)
            order['fee'] = round(fee, 4)
            order['close_reason'] = close_reason
            closed_orders_batch.append(order)
    
    # 保存已平仓订单
    if closed_orders_batch:
        filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '_closed.json'
        filepath = os.path.join(SIM_RESULTS_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(closed_orders_batch, f, ensure_ascii=False, indent=2)
    
    return closed_orders_batch


def stat_sim_results():
    """统计所有已平仓订单的胜率、盈亏等"""
    win, total, profit, loss = 0, 0, 0, 0
    processed_order_ids = set()  # 避免重复统计
    
    for fname in os.listdir(SIM_RESULTS_DIR):
        if fname.endswith('_closed.json'):
            try:
                with open(os.path.join(SIM_RESULTS_DIR, fname), 'r', encoding='utf-8') as f:
                    orders = json.load(f)
                    for o in orders:
                        # 去重逻辑
                        order_id = o.get('order_id')
                        if order_id and order_id in processed_order_ids:
                            continue  # 已处理过的订单跳过
                        if order_id:
                            processed_order_ids.add(order_id)
                        
                        # 统计盈亏
                        total += 1
                        pnl = o.get('pnl', 0)
                        if pnl > 0:
                            win += 1
                            profit += pnl
                        else:
                            loss += pnl
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error reading statistics from file {fname}: {e}")
                continue
    
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
    all_orders_in_24h = []
    
    # 按时间收集所有24小时内的订单
    for fname in os.listdir(SIM_RESULTS_DIR):
        filepath = os.path.join(SIM_RESULTS_DIR, fname)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                batch = json.load(f)
                for o in batch:
                    # 获取事件时间（平仓或开仓时间）
                    event_time_str = None
                    if o.get('status') == 'closed' and o.get('close_time'):
                        event_time_str = o['close_time']
                    elif o.get('open_time'):
                        event_time_str = o['open_time']
                    
                    # 判断是否在24小时内
                    if event_time_str:
                        try:
                            event_dt = datetime.strptime(event_time_str, '%Y-%m-%d %H:%M:%S')
                            if (now - event_dt).total_seconds() <= 86400:  # 24小时
                                all_orders_in_24h.append(o)
                        except ValueError:
                            try:
                                # 尝试其他可能的时间格式
                                event_dt = datetime.fromisoformat(event_time_str)  
                                if (now - event_dt).total_seconds() <= 86400:
                                    all_orders_in_24h.append(o)
                            except ValueError:
                                print(f"Skipping order due to unparsed time {event_time_str} in {fname}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading 24h orders from file {fname}: {e}")
            continue
    
    if not all_orders_in_24h:
        return '\n**最近24小时无模拟订单记录。**'
    
    # 基于订单ID去重，优先保留已平仓订单
    deduped_orders = {}
    for o in all_orders_in_24h:
        order_id = o.get('order_id')
        if not order_id:
            # 为老订单生成ID
            order_id = f"legacy_{o.get('symbol', '')}_{o.get('open_time', '')}"
            o['order_id'] = order_id
        
        # 去重逻辑：如果订单ID已存在，只有新订单是closed状态而旧订单不是时才更新
        if order_id in deduped_orders:
            existing_order = deduped_orders[order_id]
            # 如果新订单是closed状态而旧订单不是，或者两者都是closed但新订单更晚关闭，则更新
            if (o.get('status') == 'closed' and existing_order.get('status') != 'closed') or \
               (o.get('status') == 'closed' and existing_order.get('status') == 'closed' and \
                o.get('close_time', '') > existing_order.get('close_time', '')):
                deduped_orders[order_id] = o
        else:
            deduped_orders[order_id] = o
    
    # 转换为列表并按时间排序
    final_list = list(deduped_orders.values())
    final_list.sort(key=lambda x: x.get('close_time') or x.get('open_time') or '')
    
    # 生成Markdown表格
    md = ['\n**最近24小时模拟盘明细**\n',
          '| Order ID | Symbol | Type | Direction | Entry | Stop | Take | USDT | Open Time | Status | Close Time | Close Price | PnL | Fee | Reason |',
          '|----------|--------|------|-----------|-------|------|------|------|-----------|--------|------------|-------------|-----|-----|--------|']
    
    for o in final_list:
        oid_part = o.get('order_id', 'N/A')[:8]  # 显示ID前8位
        md.append(f"| {oid_part} | {o.get('symbol','')} | {o.get('type','')} | {o.get('direction','')} | "
                  f"{o.get('entry','')} | {o.get('stop','')} | {o.get('take','')} | {o.get('usdt_amt','')} | "
                  f"{o.get('open_time','')} | {o.get('status','')} | {o.get('close_time','')} | "
                  f"{o.get('close_price','')} | {o.get('pnl','')} | {o.get('fee','')} | {o.get('close_reason','')}")
    
    return '\n'.join(md)

# Ensure this file ends with a newline 