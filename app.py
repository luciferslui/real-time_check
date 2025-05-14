from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import MetaTrader5 as mt5
import pandas as pd
import time
from datetime import datetime, timedelta
import logging
import numpy as np
import os
import threading
import json
import random
import string
import re
from websocket import create_connection, WebSocketConnectionClosedException
import requests
from cachetools import TTLCache

# 設定日誌記錄
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app)

# CME Group API 快取（5 分鐘 = 300 秒）
cme_cache = TTLCache(maxsize=1, ttl=86400)

# --- MT5 邏輯 ---

# 初始化 MT5 連線
server = "MingTakInternational-Server"
login = 880000843
password = "Q!Nb3pRy"

if not mt5.initialize(login=login, server=server, password=password):
    logger.error("MT5 初始化失敗，錯誤代碼 = %s", mt5.last_error())
    quit()

if not mt5.terminal_info().connected:
    logger.error("MT5 終端機未連接到伺服器，請檢查連線狀態")
    mt5.shutdown()
    quit()

symbol = "XAUUSD.demo"
if not mt5.symbol_select(symbol, True):
    logger.error("無法選擇 %s，錯誤代碼 = %s", symbol, mt5.last_error())
    mt5.shutdown()
    quit()

def get_yesterday_ohlcv():
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=2)
        
        max_retries = 3
        for attempt in range(max_retries):
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, start_time, end_time)
            if rates is not None and len(rates) > 0:
                break
            logger.warning(f"獲取數據失敗，重試中 ({attempt+1}/{max_retries})")
            time.sleep(1)
        else:
            logger.error("多次嘗試後仍無法獲取H1數據，錯誤代碼 = %s", mt5.last_error())
            return None, []

        logger.info("Raw rates length: %d", len(rates))
        df = pd.DataFrame(rates)
        if df.empty:
            logger.error("DataFrame is empty")
            return None, []

        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.sort_values('time').reset_index(drop=True)
        logger.info("DataFrame time range: %s to %s", df['time'].min(), df['time'].max())
        
        df['time_diff_hours'] = df['time'].diff().dt.total_seconds() / 3600
        
        calendar_yesterday_start = datetime.combine(end_time.date() - timedelta(days=1), datetime.min.time())
        calendar_yesterday_end = calendar_yesterday_start + timedelta(days=1)
        
        mask = (df['time'] >= (calendar_yesterday_start - timedelta(weeks=1))) & (df['time'] <= calendar_yesterday_end)
        df_relevant = df.loc[mask].copy()
        if df_relevant.empty:
            logger.error("Filtered DataFrame is empty for range %s to %s", 
                        calendar_yesterday_start - timedelta(weeks=1), calendar_yesterday_end)
            return None, []
        
        df_relevant['is_trading'] = df_relevant['time_diff_hours'].fillna(1) == 1
        for i in range(len(df_relevant)):
            if i > 0 and df_relevant.iloc[i]['time_diff_hours'] > 1:
                df_relevant.iloc[i, df_relevant.columns.get_loc('is_trading')] = True
        
        non_trading_times = []
        for i in range(1, len(df_relevant)):
            if df_relevant.iloc[i]['time_diff_hours'] > 1:
                start = df_relevant.iloc[i-1]['time']
                end = df_relevant.iloc[i]['time']
                current = start + timedelta(hours=1)
                while current < end:
                    if calendar_yesterday_start - timedelta(weeks=1) <= current < calendar_yesterday_end:
                        non_trading_times.append(current)
                    current += timedelta(hours=1)
        
        logger.info("非交易時段：%s", [t.strftime('%Y-%m-%d %H:%M') for t in non_trading_times])
        
        yesterday_start = None
        yesterday_end = None
        
        for non_trading_time in sorted(non_trading_times):
            if non_trading_time < calendar_yesterday_start:
                potential_start = non_trading_time + timedelta(hours=1)
                if potential_start in df_relevant['time'].values:
                    yesterday_start = potential_start
                    logger.info("找到 yesterday_start：%s", yesterday_start)
                    break
        
        for non_trading_time in sorted(non_trading_times):
            if calendar_yesterday_start <= non_trading_time < calendar_yesterday_end:
                potential_end = non_trading_time - timedelta(hours=1)
                if potential_end in df_relevant['time'].values:
                    yesterday_end = potential_end
                    logger.info("找到 yesterday_end：%s", yesterday_end)
                    break
        
        if yesterday_end is None:
            df_yesterday_trading = df_relevant[
                (df_relevant['time'] >= calendar_yesterday_start) &
                (df_relevant['time'] < calendar_yesterday_end) &
                (df_relevant['is_trading'])
            ]
            if not df_yesterday_trading.empty:
                yesterday_end = df_yesterday_trading['time'].max()
                logger.info("使用昨日最後一個交易小時作為 yesterday_end：%s", yesterday_end)
        
        if yesterday_start is None:
            yesterday_start = calendar_yesterday_start
            logger.warning("未找到前一天非交易時段後的交易開始時間，使用日歷開始時間")
        if yesterday_end is None:
            yesterday_end = calendar_yesterday_end - timedelta(hours=1)
            logger.warning("未找到昨日非交易時段前的交易結束時間，使用日歷結束時間前一小時")
        
        print(f"昨日交易時段數據範圍：{yesterday_start} 到 {yesterday_end}")
        
        df_yesterday = df_relevant[(df_relevant['time'] >= yesterday_start) & (df_relevant['time'] <= yesterday_end)].copy()
        df_yesterday['is_trading'] = df_yesterday['time'].isin(df_relevant[df_relevant['is_trading']]['time'])
        
        df_trading = df_yesterday[df_yesterday['is_trading']]
        if df_trading.empty:
            logger.info("昨日無有效交易數據")
            return None, sorted([t.strftime('%Y-%m-%d %H:%M') for t in non_trading_times])

        ohlcv = {
            'open': float(df_trading.iloc[0]['open']),
            'high': float(df_trading['high'].max()),
            'low': float(df_trading['low'].min()),
            'close': float(df_trading.iloc[-1]['close']),
            'volume': int(df_trading['tick_volume'].sum()),
            'candle_count': int(len(df_trading))
        }
        logger.info("OHLCV calculated: %s", ohlcv)
        return ohlcv, sorted([t.strftime('%Y-%m-%d %H:%M') for t in non_trading_times])

    except Exception as e:
        logger.error("Error in get_yesterday_ohlcv: %s", str(e), exc_info=True)
        return None, []

def get_current_d1_data():
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1)
        if rates is None or len(rates) == 0:
            logger.warning("無法獲取 D1 數據，錯誤代碼 = %s", mt5.last_error())
            return None
        rate = rates[0]
        return {
            'open': float(rate['open']),
            'high': float(rate['high']),
            'low': float(rate['low']),
            'close': float(rate['close']),
            'volume': int(rate['tick_volume']),
            'time': pd.to_datetime(rate['time'], unit='s').strftime('%Y-%m-%d')
        }
    except Exception as e:
        logger.error("Error in get_current_d1_data: %s", str(e))
        return None

yesterday_ohlcv, non_trading_times = get_yesterday_ohlcv()

@app.route('/api/yesterday_ohlcv')
def api_yesterday_ohlcv():
    try:
        if yesterday_ohlcv is None:
            logger.warning("No yesterday OHLCV data available")
            return jsonify({'error': 'No yesterday OHLCV data available'}), 200
        return jsonify(yesterday_ohlcv)
    except Exception as e:
        logger.error("Error in api_yesterday_ohlcv: %s", str(e), exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

def monitor_d1_data():
    csv_file = "XAUUSD_d1_ohlcv.csv"
    current_ohlcv = None
    current_day = None

    if os.path.exists(csv_file):
        os.remove(csv_file)

    df_d1 = pd.DataFrame(columns=['update_time', 'open', 'high', 'low', 'close', 'volume'])
    df_d1.to_csv(csv_file, index=False)

    logger.info("開始實時監控 D1 數據")
    try:
        while True:
            d1_data = get_current_d1_data()
            
            if d1_data is None:
                time.sleep(60)  # 日線數據更新頻率較低，等待 60 秒
                continue
            
            logger.debug("當前 D1 數據: time=%s, open=%s, high=%s, low=%s, close=%s, volume=%s",
                         d1_data['time'], d1_data['open'], d1_data['high'], d1_data['low'], d1_data['close'], d1_data['volume'])
            
            d1_day = pd.to_datetime(d1_data['time']).floor('D')
            if current_day is None or d1_day > current_day:
                logger.info("進入新交易日：%s，重置 OHLCV", d1_day)
                current_day = d1_day
                current_ohlcv = d1_data.copy()
                ohlcv_record = {
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'open': current_ohlcv['open'],
                    'high': current_ohlcv['high'],
                    'low': current_ohlcv['low'],
                    'close': current_ohlcv['close'],
                    'volume': current_ohlcv['volume']
                }
                socketio.emit('ohlcv_update', ohlcv_record)
                pd.DataFrame([ohlcv_record]).to_csv(csv_file, mode='a', header=False, index=False)
                logger.info("記錄新交易日初始 D1 OHLCV 數據：%s", ohlcv_record)
                continue
            
            prev_ohlcv = current_ohlcv.copy() if current_ohlcv else None
            current_ohlcv = d1_data.copy()
            
            has_changed = (
                prev_ohlcv is None or
                not np.isclose(prev_ohlcv['open'], current_ohlcv['open'], atol=1e-5) or
                not np.isclose(prev_ohlcv['high'], current_ohlcv['high'], atol=1e-5) or
                not np.isclose(prev_ohlcv['low'], current_ohlcv['low'], atol=1e-5) or
                not np.isclose(prev_ohlcv['close'], current_ohlcv['close'], atol=1e-5) or
                prev_ohlcv['volume'] != current_ohlcv['volume']
            )
            
            if has_changed:
                ohlcv_record = {
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'open': current_ohlcv['open'],
                    'high': current_ohlcv['high'],
                    'low': current_ohlcv['low'],
                    'close': current_ohlcv['close'],
                    'volume': current_ohlcv['volume']
                }
                socketio.emit('ohlcv_update', ohlcv_record)
                pd.DataFrame([ohlcv_record]).to_csv(csv_file, mode='a', header=False, index=False)
                logger.info("記錄更新 D1 OHLCV 數據：%s", ohlcv_record)
            
            time.sleep(60)  # 日線檢查間隔 60 秒

    except Exception as e:
        logger.error("監控錯誤: %s", str(e))

# --- TradingView WebSocket 邏輯 ---

def filter_raw_message(text):
    try:
        found = re.search('"m":"(.+?)",', text).group(1)
        found2 = re.search('"p":(.+?"}"])}', text).group(1)
        return found, found2
    except AttributeError:
        logger.debug("訊息解析失敗（非 JSON 訊息）")
        return None, None

def generateSession():
    stringLength = 12
    letters = string.ascii_lowercase
    random_string = ''.join(random.choice(letters) for i in range(stringLength))
    return "qs_" + random_string

def generateChartSession():
    stringLength = 12
    letters = string.ascii_lowercase
    random_string = ''.join(random.choice(letters) for i in range(stringLength))
    return "cs_" + random_string

def prependHeader(st):
    return "~m~" + str(len(st)) + "~m~" + st

def constructMessage(func, paramList):
    return json.dumps({"m": func, "p": paramList}, separators=(',', ':'))

def createMessage(func, paramList):
    return prependHeader(constructMessage(func, paramList))

def sendRepresentationMessage(ws, func, args):
    ws.send(createMessage(func, args))

def parse_du_message(message):
    if "~h~" in message:
        return None  # 跳過心跳訊息
    try:
        if message.startswith("~m~"):
            json_str = message.split("~m~", 2)[-1]
            data = json.loads(json_str)
            if data.get("m") == "du":
                kline = data["p"][1]["s1"]["s"][0]["v"]
                timestamp = kline[0]
                open_price = kline[1]
                high_price = kline[2]
                low_price = kline[3]
                close_price = kline[4]
                volume = kline[5]
                return {
                    "timestamp": datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d"),
                    "open": float(open_price),
                    "high": float(high_price),
                    "low": float(low_price),
                    "close": float(close_price),
                    "volume": int(volume)
                }
            elif data.get("m") == "qsd":
                lp = data["p"][1].get("v", {}).get("lp")
                if lp:
                    return {"lp": float(lp)}
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.debug(f"訊息解析失敗: {e}")
    return None

def connect_websocket():
    headers = {"Origin": "https://data.tradingview.com"}
    try:
        ws = create_connection("wss://data.tradingview.com/socket.io/websocket", headers=headers)
        logger.info("TradingView WebSocket 連線成功")
        return ws
    except Exception as e:
        logger.error(f"TradingView WebSocket 連線失敗: {e}")
        raise

def monitor_tradingview_data():
    symbol = "COMEX:GCM2025"
    processed_timestamps = set()
    while True:
        try:
            ws = connect_websocket()
            session = generateSession()
            chart_session = generateChartSession()
            logger.info(f"TradingView session: {session}, chart_session: {chart_session}")

            sendRepresentationMessage(ws, "set_auth_token", ["unauthorized_user_token"])
            sendRepresentationMessage(ws, "chart_create_session", [chart_session, ""])
            sendRepresentationMessage(ws, "quote_create_session", [session])
            sendRepresentationMessage(ws, "quote_set_fields", [session, "ch", "chp", "current_session", "description", 
                                                "local_description", "language", "exchange", "fractional", 
                                                "is_tradable", "lp", "lp_time", "minmov", "minmove2", 
                                                "original_name", "pricescale", "pro_name", "short_name", 
                                                "type", "update_mode", "volume", "currency_code", "rchp", "rtc"])
            sendRepresentationMessage(ws, "quote_add_symbols", [session, symbol, {"flags": ["force_permission"]}])
            sendRepresentationMessage(ws, "quote_fast_symbols", [session, symbol])
            sendRepresentationMessage(ws, "resolve_symbol", [chart_session, "symbol_1", f"={{\"symbol\":\"{symbol}\",\"adjustment\":\"splits\",\"session\":\"extended\"}}"])
            sendRepresentationMessage(ws, "create_series", [chart_session, "s1", "s1", "symbol_1", "D", 5000])

            while True:
                try:
                    result = ws.recv()
                    data = parse_du_message(result)
                    if data and "lp" not in data:
                        timestamp = data["timestamp"]
                        if timestamp not in processed_timestamps:
                            processed_timestamps.add(timestamp)
                            logger.info(f"New GCM2025 K-line: {data}")
                            socketio.emit('gcm_kline_update', data)
                        else:
                            logger.info(f"Updated GCM2025 K-line: {data}")
                            socketio.emit('gcm_kline_update', data)
                except WebSocketConnectionClosedException:
                    logger.warning("TradingView WebSocket 連線斷開，正在重連...")
                    break
                except Exception as e:
                    logger.error(f"TradingView 訊息處理錯誤: {e}")
                    time.sleep(1)

        except Exception as e:
            logger.error(f"TradingView WebSocket 錯誤: {e}")
            time.sleep(5)

# --- CME Group API 邏輯 ---

@app.route('/api/cme_settlements')
def api_cme_settlements():
    try:
        # 檢查快取
        if 'jun25_data' in cme_cache:
            logger.info("從快取返回 CME JUN 25 數據")
            return jsonify(cme_cache['jun25_data'])

        # 請求 CME Group API
        url = "https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/437/FUT?strategy=DEFAULT&tradeDate=05/13/2025"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # 過濾 JUN 25 數據
        jun25_data = next((item for item in data['settlements'] if item['month'] == 'JUN 25'), None)
        if not jun25_data:
            logger.warning("未找到 JUN 25 數據")
            return jsonify({'error': 'No JUN 25 data available'}), 200

        # 添加更新時間
        jun25_data['updateTime'] = data.get('updateTime', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        # 存入快取
        cme_cache['jun25_data'] = jun25_data
        logger.info("成功獲取並快取 CME JUN 25 數據: %s", jun25_data)

        return jsonify(jun25_data)

    except requests.RequestException as e:
        logger.error("CME API 請求失敗: %s", str(e))
        return jsonify({'error': 'Failed to fetch CME data'}), 500
    except Exception as e:
        logger.error("處理 CME 數據錯誤: %s", str(e))
        return jsonify({'error': 'Internal server error'}), 500

# 啟動監控線程
mt5_monitor_thread = threading.Thread(target=monitor_d1_data, daemon=True)
mt5_monitor_thread.start()

tradingview_monitor_thread = threading.Thread(target=monitor_tradingview_data, daemon=True)
tradingview_monitor_thread.start()

# 渲染主頁
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    try:
        socketio.run(app, debug=True, use_reloader=False)
    finally:
        mt5.shutdown()
        logger.info("MT5 連線已關閉")