
import os, time, statistics, requests
from collections import defaultdict, deque
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "90"))
MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "5"))
TOP_CANDIDATES = int(os.getenv("TOP_CANDIDATES", "20"))

BITUNIX = "https://fapi.bitunix.com"
TOOBIT = "https://api.toobit.com"
s = requests.Session()
s.headers.update({"User-Agent":"early-pump-dump-scanner/2.0"})

last_alert = {}
alert_times = deque(maxlen=100)
oi_history = {}
price_history = defaultdict(lambda: deque(maxlen=20))

def get(url, params=None):
    r=s.get(url,params=params,timeout=8); r.raise_for_status(); return r.json()

def send(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg); return
    r=s.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
             json={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=8)
    r.raise_for_status()

def pct(a,b):
    return 0 if not a else (b/a-1)*100

def vol_ratio(k):
    if len(k)<7:return 1
    vals=[]
    for x in k[:-1]:
        try: vals.append(float(x[7] if len(x)>7 else x[5]))
        except: pass
    try: cur=float(k[-1][7] if len(k[-1])>7 else k[-1][5])
    except: return 1
    base=vals[-5:]
    return cur/(statistics.mean(base) or cur)

def move5(k):
    return pct(float(k[-6][1]),float(k[-1][4])) if len(k)>=6 else 0

def depth_ratio(book):
    bids=book.get("bids",[]) or []; asks=book.get("asks",[]) or []
    b=sum(float(x[1]) for x in bids[:10]); a=sum(float(x[1]) for x in asks[:10])
    return b/a if a else 1

def score(kind, vr, mv, dr, oi_change=None, funding=None):
    # Conservative: detect early acceleration, penalize already-extended moves.
    score=0
    if vr>=5: score+=30
    elif vr>=3.5: score+=25
    elif vr>=2.5: score+=20
    elif vr>=1.8: score+=12

    if kind=="PUMP":
        if 0.5<=mv<=3.5: score+=22
        elif 3.5<mv<=5: score+=10
        elif mv>5: score-=15
        elif mv<0: score-=12
        if dr>=1.8: score+=20
        elif dr>=1.4: score+=14
        elif dr>=1.15: score+=8
        elif dr<0.8: score-=10
        if oi_change is not None:
            if oi_change>=2.5: score+=22
            elif oi_change>=1.2: score+=16
            elif oi_change>=0.5: score+=8
            elif oi_change<=-2: score-=10
        if funding is not None and funding>0.0015: score-=8
    else:
        if -3.5<=mv<=-0.5: score+=22
        elif -5<=mv<-3.5: score+=10
        elif mv<-5: score-=15
        elif mv>0: score-=12
        if dr<=0.55: score+=20
        elif dr<=0.72: score+=14
        elif dr<=0.87: score+=8
        elif dr>1.25: score-=10
        if oi_change is not None:
            if oi_change>=2.5: score+=20
            elif oi_change>=1.2: score+=14
            elif oi_change>=0.5: score+=7
            elif oi_change<=-2: score-=10
        if funding is not None and funding<-0.0015: score-=8
    if vr<1.8: score=min(score,59)
    return max(0,min(100,int(score)))

def allowed(key):
    now=time.time()
    if now-last_alert.get(key,0)<COOLDOWN_MIN*60:return False
    while alert_times and now-alert_times[0]>3600: alert_times.popleft()
    return len(alert_times)<MAX_ALERTS_PER_HOUR

def alert(exchange,symbol,kind,sc,vr,mv,dr,oic,fr):
    key=f"{exchange}:{symbol}:{kind}"
    if sc<MIN_SCORE or not allowed(key): return
    last_alert[key]=time.time(); alert_times.append(time.time())
    oi="n/a" if oic is None else f"{oic:+.2f}%"
    fund="n/a" if fr is None else f"{fr*100:+.3f}%"
    icon="🟢" if kind=="PUMP" else "🔴"
    title="EARLY PUMP" if kind=="PUMP" else "EARLY DUMP"
    send(f"{icon} {title} ALERT\n\nExchange: {exchange}\nContract: {symbol}\nScore: {sc}/100\n\n📊 Volume: {vr:.1f}x recent avg\n📈 5m price: {mv:+.2f}%\n🔥 OI: {oi}\n📚 Bid/Ask depth: {dr:.2f}\n💰 Funding: {fund}\n\n⚠️ Early-momentum signal, NOT a guaranteed pump/dump.\nCheck liquidity and chart before trading.")

def bitunix():
    data=get(f"{BITUNIX}/api/v1/futures/market/tickers").get("data",[])
    arr=[]
    for x in data:
        try:
            sym=x["symbol"]
            if sym.endswith("USDT"): arr.append((float(x.get("quoteVol",0)),sym))
        except: pass
    for _,sym in sorted(arr,reverse=True)[:TOP_CANDIDATES]:
        try:
            k=get(f"{BITUNIX}/api/v1/futures/market/kline",{"symbol":sym,"interval":"1m","limit":20}).get("data",[])
            d=get(f"{BITUNIX}/api/v1/futures/market/depth",{"symbol":sym,"limit":15}).get("data",{})
            vr=vol_ratio(k); mv=move5(k); dr=depth_ratio(d)
            # Bitunix public ticker endpoints vary by contract; this version intentionally
            # avoids requiring private API keys. OI can be added if the endpoint is enabled.
            for kind in ("PUMP","DUMP"):
                alert("Bitunix",sym,kind,score(kind,vr,mv,dr),vr,mv,dr,None,None)
        except Exception as e: print("Bitunix",sym,e)

def toobit():
    data=get(f"{TOOBIT}/quote/v1/contract/ticker/24hr")
    arr=[]
    for x in data:
        try:
            sym=x["s"]
            if sym.endswith("-SWAP-USDT"): arr.append((float(x.get("qv",0)),sym))
        except: pass
    for _,sym in sorted(arr,reverse=True)[:TOP_CANDIDATES]:
        try:
            k=get(f"{TOOBIT}/quote/v1/klines",{"symbol":sym,"interval":"1m","limit":20})
            d=get(f"{TOOBIT}/quote/v1/depth",{"symbol":sym,"limit":20})
            try:
                oi_data=get(f"{TOOBIT}/quote/v1/openInterest",{"symbol":sym}).get("openInterestList",[])
                oi=float(oi_data[0]["size"]) if oi_data else None
            except: oi=None
            try:
                fr_data=get(f"{TOOBIT}/api/v1/futures/fundingRate",{"symbol":sym})
                fr=float(fr_data[0]["rate"]) if fr_data else None
            except: fr=None
            vr=vol_ratio(k); mv=move5(k); dr=depth_ratio(d)
            prev=oi_history.get(sym)
            oi_history[sym]=oi
            oic=pct(prev,oi) if prev and oi is not None else None
            for kind in ("PUMP","DUMP"):
                alert("Toobit",sym,kind,score(kind,vr,mv,dr,oic,fr),vr,mv,dr,oic,fr)
        except Exception as e: print("Toobit",sym,e)

def main():
    print("Early Pump + Dump Scanner V2 — CONSERVATIVE")
    print("Public market data only; no trade execution.")
    while True:
        t=time.time()
        try: bitunix()
        except Exception as e: print("Bitunix scan error:",e)
        try: toobit()
        except Exception as e: print("Toobit scan error:",e)
        time.sleep(max(5,SCAN_SECONDS-(time.time()-t)))

if __name__=="__main__": main()
