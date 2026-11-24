# -*- coding: utf-8 -*-
"""股吧共识 v2：爬东财股吧讨论热度 → 细分聚合（个股×申万行业）→ 专栏展示 + 推送 aihot
用法:
    python guba_consensus.py            # 爬+聚合，产出 consensus_latest.json + 快照
    python guba_consensus.py --push     # 同时把 TOP3 共识条目推给 aihot（hot_signal，零 LLM）
数据源: https://guba.eastmoney.com/list,<code>_<page>.html 内嵌 var article_list={re:[...]}
产物:
    consensus_latest.json   细分共识卡（个股+行业汇总+环比+关键词+热帖）→ 基金网页 /api/consensus 消费
    guba_snapshot.csv       历史快照（供环比）
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request

import requests

sys.stdout.reconfigure(encoding='utf-8')
import jieba  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_CSV = os.path.join(BASE, 'guba_snapshot.csv')
CONSENSUS_JSON = os.path.join(BASE, 'consensus_latest.json')
LLM_CACHE = os.path.join(BASE, 'llm_focus_cache.json')
STANCE_CACHE = os.path.join(BASE, 'stance_cache.json')
STOCK_META_BUNDLE = os.path.join(BASE, 'stock_meta.csv')  # 仓库内置个股名/行业（服务器回退）
# 路径/地址均可环境变量覆盖（本地默认值兼容 Windows 开发机）
AIHOT_ENV_FILE = os.environ.get('AIHOT_ENV_FILE', r'D:/quant_baibaibai/aihot-fin/.env')
AIHOT_INGEST = os.environ.get('AIHOT_INGEST_URL', 'http://127.0.0.1:3001/api/ingest/items')
STOCK_INFO = os.environ.get('STOCK_INFO_CSV', r'D:/quant_baibaibai/数据存放和更新/数据存放/stock_info.csv')
SOURCE_ID = 'guba-consensus'
SOURCE_NAME = '股吧共识（聚合）'

WATCHLIST = [
    # 银行
    '000001', '600036', '601398', '601988', '600000',
    # 非银金融
    '601318', '600030', '300059', '601601',
    # 食品饮料
    '600519', '000858', '000568', '600887', '603288',
    # 家用电器
    '000333', '000651', '600690',
    # 电力设备/新能源
    '300750', '601012', '300274', '600438',
    # 汽车
    '002594', '601127', '600104', '000625', '601633',
    # 电子
    '002475', '000725', '603501', '002371',
    # 计算机
    '002230', '600588', '688111', '600845',
    # 通信
    '300308', '600941', '601728', '000063', '300502',
    # 医药生物
    '600276', '603259', '300760', '000538',
    # 有色金属
    '601899', '603993', '600547', '002460',
    # 煤炭/石油石化
    '601088', '601225', '600938', '601857',
    # 机械设备/基础化工
    '600031', '600309',
    # 公用事业
    '600900', '600886',
]

BULL = ['利好', '利多', '大涨', '涨停', '突破', '新高', '加仓', '抄底', '起飞', '爆发', '超预期', '订单', '中标', '回购', '增持', '分红']
BEAR = ['利空', '大跌', '跌停', '破位', '减持', '质押', '爆仓', '割肉', '出货', '造假', '亏损', '下滑', '诉讼', '处罚', '退市']
STOP = set('的了是在和有我你们他她它们这那些什么怎么因为所以但是如果就都还也很太再把被让给对从到往上下中内外前后时间月日年号一个不没')
NOISE = {'明天', '今天', '昨天', '直接', '已经', '就是', '可以', '不会', '没有', '一直', '真的', '有点', '一下',
         '感觉', '各位', '兄弟', '姐妹', '是不是', '为什么', '时候', '开始', '还是', '这个', '那个', '垃圾', '起来', '还有', '现在', '稍后'}
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'}

# 操作立场词典（标题级；命中数最多者胜，并列→unknown 交小模型）
STANCE_LEX = {
    'buy': ['买入', '加仓', '抄底', '上车', '建仓', '梭哈', '看多', '定投', '补仓', '逢低吸', '起爆'],
    'sell': ['卖出', '清仓', '割肉', '减仓', '出逃', '跑路', '看空', '止盈', '落袋', '出货'],
    'hold': ['持有', '拿着', '死拿', '长拿', '躺平', '不动', '继续拿', '长期拿'],
    'watch': ['踏空', '观望', '等回调', '没上车', '错过', '再等等', '等企稳', '不敢买', '犹豫'],
}
STANCE_LABEL = {'buy': '买入', 'sell': '卖出', 'hold': '持有', 'watch': '观望'}


def classify_stance(title):
    """词典分类：命中数最多的立场，并列返回 None（交小模型）"""
    hits = {k: sum(title.count(w) for w in ws) for k, ws in STANCE_LEX.items()}
    top = max(hits.values())
    if top == 0:
        return None
    winners = [k for k, v in hits.items() if v == top]
    return winners[0] if len(winners) == 1 else None


def llm_classify_stances(titles):
    """未判定标题批量交硅基流动免费小模型（按标题缓存）"""
    if not titles:
        return {}
    cache = {}
    if os.path.exists(STANCE_CACHE):
        try:
            cache = json.load(open(STANCE_CACHE, encoding='utf-8'))
        except Exception:
            cache = {}
    todo = [t for t in dict.fromkeys(titles) if t not in cache]
    if not todo:
        return {t: cache[t] for t in titles if t in cache}
    sf_key = _sf_key()
    if not sf_key:
        return {t: cache[t] for t in titles if t in cache}
    BATCH = 15
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        lines = '\n'.join(f'{j+1}. {t}' for j, t in enumerate(batch))
        prompt = ('判断每条A股股吧标题的操作立场，四选一：买入/卖出/持有/观望（含踏空、等待）。\n'
                  f'标题：\n{lines}\n'
                  f'只输出JSON数组，与标题一一对应，如 ["买入","观望"]')
        body = {'model': 'Qwen/Qwen2.5-7B-Instruct',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0, 'max_tokens': 400}
        try:
            req = urllib.request.Request('https://api.siliconflow.cn/v1/chat/completions',
                                         data=json.dumps(body).encode('utf-8'), method='POST',
                                         headers={'content-type': 'application/json',
                                                  'authorization': 'Bearer ' + sf_key})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode('utf-8'))
            content = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
            m = re.search(r'\[.*\]', content, re.S)
            arr = json.loads(m.group(0)) if m else []
            inv = {v: k for k, v in STANCE_LABEL.items()}
            for t, s in zip(batch, arr):
                st = inv.get(str(s).strip())
                if st:
                    cache[t] = st
        except Exception as e:
            print(f'  [llm] 立场分类批次失败: {e}')
        time.sleep(0.5)
    json.dump(cache, open(STANCE_CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return {t: cache[t] for t in titles if t in cache}


def _load_token():
    tok = os.environ.get('AIHOT_INGEST_TOKEN', '')
    if tok:
        return tok
    env_path = AIHOT_ENV_FILE
    if os.path.exists(env_path):
        for line in open(env_path, encoding='utf-8'):
            if line.strip().startswith('INGEST_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"\'')
    return ''


AIHOT_TOKEN = _load_token()


def load_stock_meta():
    """个股名/行业映射：优先 stock_info.csv（开发机），缺失时用仓库内置 stock_meta.csv（服务器）"""
    meta = {}
    src = STOCK_INFO if os.path.exists(STOCK_INFO) else (STOCK_META_BUNDLE if os.path.exists(STOCK_META_BUNDLE) else None)
    if not src:
        return meta
    df = pd.read_csv(src, encoding='utf-8-sig', dtype=str)
    if 'stockid' in df.columns:  # stock_info.csv 原始格式
        df = df.sort_values('date').drop_duplicates('stockid', keep='last')
        for _, r in df.iterrows():
            sid = str(r['stockid'])
            code = sid.split('.')[0]
            if len(code) == 6:
                meta[code] = {'name': str(r.get('stock_name', '')), 'industry': str(r.get('申万一级行业', ''))}
    else:  # 内置精简格式：code,name,industry
        for _, r in df.iterrows():
            meta[str(r['code'])] = {'name': str(r.get('name', '')), 'industry': str(r.get('industry', ''))}
    return meta


import pandas as pd  # noqa: E402


def fetch_bar(code, pages=1, timeout=10, retries=2):
    """抓单个吧前 N 页帖子（每页约 80 条），反爬空壳页退避重试"""
    rows = []
    for p in range(1, pages + 1):
        url = f'https://guba.eastmoney.com/list,{code}_{p}.html'
        for attempt in range(retries + 1):
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            m = re.search(r'var article_list=(\{.*?\});', r.text, re.S)
            if m:
                rows += json.loads(m.group(1)).get('re') or []
                break
            if attempt < retries:
                wait = 45 * (attempt + 1)
                print(f'  [warn] {code} p{p} 反爬空壳页，{wait}s 后重试')
                time.sleep(wait)
            else:
                print(f'  [warn] {code} p{p} 仍无 article_list，放弃本页')
        time.sleep(2.5 + random.random() * 1.5)  # 礼貌间隔（限流敏感）
    return rows


def _sf_key():
    """从 aihot .env 读硅基流动 key（与立场分类同一通道，免费档）"""
    env_path = AIHOT_ENV_FILE
    if os.path.exists(env_path):
        for line in open(env_path, encoding='utf-8'):
            if line.strip().startswith('SF_API_KEY='):
                return line.split('=', 1)[1].strip()
    return ''


def llm_focus(name, titles, keywords, bull, bear):
    """免费 Qwen 生成讨论焦点+驱动消息（按输入哈希缓存，数据不变不重复调用）"""
    if not titles and not keywords:
        return None
    inp = '|'.join(titles[:10]) + '||' + ','.join(keywords)
    key = hashlib.sha256(inp.encode('utf-8')).hexdigest()[:16]
    cache = {}
    if os.path.exists(LLM_CACHE):
        try:
            cache = json.load(open(LLM_CACHE, encoding='utf-8'))
        except Exception:
            cache = {}
    if key in cache:
        return cache[key]
    sf_key = _sf_key()
    if not sf_key:
        return None
    lines = '\n'.join(f'{i+1}. {t}' for i, t in enumerate(titles[:10]))
    prompt = (f'你是A股散户舆情分析师。根据{name}股吧的帖子标题，判断散户的核心讨论焦点和驱动消息。\n'
              f'帖子标题（按热度）：\n{lines or "（无）"}\n'
              f'关键词：{"、".join(keywords) or "（无）"}\n'
              f'情绪词频：利多{bull}次，利空{bear}次\n'
              f'只输出JSON：{{"focus": "一句话概括讨论焦点(25字内)", "driver": "导致讨论热度的具体消息或事件(25字内，没有就写无明确驱动)"}}')
    body = {'model': 'Qwen/Qwen2.5-7B-Instruct',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
            'max_tokens': 400}
    try:
        req = urllib.request.Request('https://api.siliconflow.cn/v1/chat/completions',
                                     data=json.dumps(body).encode('utf-8'), method='POST',
                                     headers={'content-type': 'application/json',
                                              'authorization': 'Bearer ' + sf_key})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode('utf-8'))
        content = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        m = re.search(r'\{.*\}', content, re.S)
        out = json.loads(m.group(0)) if m else None
        if out and out.get('focus'):
            cache[key] = out
            json.dump(cache, open(LLM_CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            return out
    except Exception as e:
        print(f'  [llm] {name} 焦点生成失败: {e}')
    return None


def extract_keywords(titles, topn=5):
    """jieba 分词 → 去停用词 → 词频 topn（细分卡的关键词标签）"""
    cnt = {}
    for t in titles:
        for w in jieba.cut(t):
            w = w.strip()
            if len(w) < 2 or w in STOP or w in NOISE or re.fullmatch(r'[\d.\-]+', w):
                continue
            cnt[w] = cnt.get(w, 0) + 1
    return [w for w, _ in sorted(cnt.items(), key=lambda x: -x[1])[:topn]]


def aggregate(code, rows, since_hours=24):
    """单吧细分聚合：窗口内发帖/阅读/评论、多空词频、关键词、热帖TOP3"""
    now = datetime.datetime.now()
    cut = now - datetime.timedelta(hours=since_hours)
    posts = []
    authors = set()
    for r in rows:
        t = r.get('post_publish_time') or ''
        try:
            dt = datetime.datetime.strptime(t[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        if dt < cut:
            continue
        if r.get('user_id'):
            authors.add(str(r['user_id']))
        posts.append({
            'post_id': r.get('post_id'), 'title': r.get('post_title') or '', 'dt': dt,
            'click': r.get('post_click_count') or 0, 'comment': r.get('post_comment_count') or 0,
        })
    titles = [p['title'] for p in posts]
    joined = ' '.join(titles)
    bull = sum(joined.count(w) for w in BULL)
    bear = sum(joined.count(w) for w in BEAR)
    # 操作立场：词典先判，未判定标题攒起来交小模型
    stances = {k: 0 for k in STANCE_LABEL}
    unknown = []
    for t in titles:
        s = classify_stance(t)
        if s:
            stances[s] += 1
        else:
            unknown.append(t)
    hot = sorted(posts, key=lambda p: p['click'] + p['comment'] * 5, reverse=True)[:3]
    return {
        'code': code,
        'posts_24h': len(posts),
        'clicks_24h': sum(p['click'] for p in posts),
        'comments_24h': sum(p['comment'] for p in posts),
        'authors': len(authors),  # 去重发帖人数：共识的"多少人在说"
        'bull': bull, 'bear': bear,
        'stances': stances, 'stance_unknown': unknown, 'stance_titles': titles,
        'sentiment': '偏多' if bull > bear * 1.5 else ('偏空' if bear > bull * 1.5 else '中性'),
        'keywords': extract_keywords(titles),
        'hot_posts': [{'title': h['title'], 'url': f"https://guba.eastmoney.com/news,{code},{h['post_id']}.html",
                       'click': h['click'], 'comment': h['comment']} for h in hot],
    }


def _snapshot_rows(code=None):
    if not os.path.exists(SNAPSHOT_CSV):
        return []
    rows = list(csv.DictReader(open(SNAPSHOT_CSV, encoding='utf-8-sig')))
    return [r for r in rows if code is None or r['code'] == code]


def fallback_from_snapshot(code):
    """反爬降级：当前抓不到时，用该股最近一次有数据的快照顶上（stale=True），绝不写零"""
    rows = [r for r in _snapshot_rows(code) if int(float(r['posts_24h'] or 0)) > 0]
    if not rows:
        return None
    r = max(rows, key=lambda x: x['ts'])
    hot1 = (r.get('hot1') or '').strip()
    kw = [w for w in (r.get('kw') or '').split('|') if w]
    # 无原帖 URL 时退到吧列表页，保证链接可跳
    hot_url = (r.get('hot1_url') or '').strip() or f'https://guba.eastmoney.com/list,{code}_1.html'
    return {
        'code': code, 'posts_24h': int(float(r['posts_24h'])), 'clicks_24h': int(float(r['clicks_24h'])),
        'comments_24h': int(float(r['comments_24h'])), 'authors': None,  # 旧快照无作者列
        'bull': int(float(r['bull'])), 'bear': int(float(r['bear'])), 'sentiment': r['sentiment'],
        'keywords': kw,
        'hot_posts': [{'title': hot1, 'url': hot_url, 'click': 0, 'comment': 0}] if hot1 else
                     [{'title': '', 'url': f'https://guba.eastmoney.com/list,{code}_1.html', 'click': 0, 'comment': 0}],
        'stances': {k: 0 for k in STANCE_LABEL}, 'stance_unknown': [], 'stance_titles': [],
        'stale': True, 'stale_ts': r['ts'],
    }


def load_prev_snapshot(lookback_hours=24):
    """回看约 lookback_hours 前的快照 → {code: posts_24h}，供环比

    基线取"最接近 now-lookback 的历史快照"而非上一次跑批——一天内多次跑批时
    上一快照间隔仅几分钟，环比恒为 0；回看 24h 才与跑批频率解耦。
    """
    if not os.path.exists(SNAPSHOT_CSV):
        return {}
    rows = list(csv.DictReader(open(SNAPSHOT_CSV, encoding='utf-8-sig')))
    if not rows:
        return {}
    ts_set = sorted(set(r['ts'] for r in rows))
    target = datetime.datetime.now() - datetime.timedelta(hours=lookback_hours)
    target_s = target.strftime('%Y-%m-%d %H:%M')
    # 最接近目标时刻的快照
    ts = min(ts_set, key=lambda t: abs((datetime.datetime.strptime(t, '%Y-%m-%d %H:%M') - target).total_seconds()))
    return {r['code']: int(float(r['posts_24h'])) for r in rows if r['ts'] == ts}


def build_consensus(agg_list, meta):
    """细分共识 JSON：共识分（广度×激增×一致）+ 个股卡 + 行业汇总 + 异常

    共识分 0-100 = 0.4×参与广度 + 0.35×激增度 + 0.25×方向一致（组内百分位，非热度榜）
      广度 = 24h 去重发帖人数（多少人在说，防单帖爆款刷分）
      激增 = 讨论量环比（突然热起来 vs 一直热；无基线取组内中性值）
      一致 = max(利多,利空)/(利多+利空)，词频≥5 才算有方向证据，否则中性 50
    """
    prev = load_prev_snapshot()
    for a in agg_list:
        a.update(meta.get(a['code'], {'name': a['code'], 'industry': '其他'}))
        p = prev.get(a['code'])
        # p=0（限流空跑）无法算比率，须区分"缺失(None)"与"零"
        a['post_mom'] = round((a['posts_24h'] - p) / p, 2) if (p is not None and p > 0) else None
        a['keywords'] = [w for w in a['keywords'] if w not in a['name'] and a['name'] not in w]
        pol = a['bull'] + a['bear']
        a['score_agree'] = round(max(a['bull'], a['bear']) / pol * 100) if pol >= 5 else 50

    # 激增值：无基线用组内中性（可用环比的中位数），避免首跑全 0 失真
    avail = [a['post_mom'] for a in agg_list if a['post_mom'] is not None]
    neutral = float(pd.Series(avail).median()) if avail else 0.0
    surge_vals = [a['post_mom'] if a['post_mom'] is not None else neutral for a in agg_list]
    # 广度：作者数缺失（旧快照降级）用发帖数代理
    breadth_vals = [a['authors'] if a.get('authors') is not None else a['posts_24h'] for a in agg_list]
    breadth_pct = pd.Series(breadth_vals).rank(pct=True)
    surge_pct = pd.Series(surge_vals).rank(pct=True)
    for i, a in enumerate(agg_list):
        a['score_breadth'] = round(breadth_pct.iloc[i] * 100)
        a['score_surge'] = round(surge_pct.iloc[i] * 100)
        a['consensus_score'] = round(0.4 * a['score_breadth'] + 0.35 * a['score_surge'] + 0.25 * a['score_agree'])

    # 主排序 = 共识分；讨论热度名次保留作参考
    stocks = sorted(agg_list, key=lambda a: -a['consensus_score'])
    for i, a in enumerate(stocks, 1):
        a['rank'] = i
    eng = sorted(agg_list, key=lambda a: -(a['clicks_24h'] + a['comments_24h'] * 5))
    for i, a in enumerate(eng, 1):
        a['rank_engagement'] = i

    # 原因分析：LLM 生成讨论焦点+驱动消息（缓存，数据不变不重复调用）
    for a in stocks:
        titles = [h['title'] for h in a['hot_posts'] if h.get('title')]
        out = llm_focus(a['name'], titles, a['keywords'], a['bull'], a['bear'])
        a['focus'] = (out or {}).get('focus')
        a['driver'] = (out or {}).get('driver')

    # 操作立场：未判定标题批量交免费小模型，再汇总全市场力量
    all_unknown = [t for a in agg_list for t in a.get('stance_unknown', [])]
    llm_map = llm_classify_stances(all_unknown)
    market = {k: 0 for k in STANCE_LABEL}
    market_unknown = 0
    market_total = 0
    for a in agg_list:
        st = dict(a.get('stances') or {k: 0 for k in STANCE_LABEL})
        for t in a.get('stance_unknown', []) or []:
            s = llm_map.get(t)
            if s:
                st[s] = st.get(s, 0) + 1
            else:
                market_unknown += 1
        a['stance'] = {STANCE_LABEL[k]: st.get(k, 0) for k in STANCE_LABEL}
        a['stance_total'] = sum(st.values())
        for k in STANCE_LABEL:
            market[k] += st.get(k, 0)
        market_total += a['stance_total']
    decided = market['buy'] + market['sell'] + market['hold'] + market['watch']
    net = round((market['buy'] - market['sell']) / decided, 3) if decided else 0.0
    if net > 0.35:
        level = '过热'
    elif net > 0.15:
        level = '偏热'
    elif net >= -0.15:
        level = '中性'
    elif net >= -0.35:
        level = '偏冷'
    else:
        level = '过冷'
    market_panel = {
        'buy': market['buy'], 'sell': market['sell'], 'hold': market['hold'], 'watch': market['watch'],
        'unknown': market_unknown, 'total': market_total,
        'coverage': round(decided / market_total, 3) if market_total else 0,
        'net_force': net, 'heat_level': level,
        'pct': {STANCE_LABEL[k]: round(market[k] / decided, 3) if decided else 0 for k in STANCE_LABEL},
    }

    ind = {}
    for a in agg_list:
        k = a['industry'] or '其他'
        d = ind.setdefault(k, {'industry': k, 'stocks': 0, 'posts': 0, 'clicks': 0, 'comments': 0, 'bull': 0, 'bear': 0, 'scores': [], 'codes': [], 'members': []})
        d['stocks'] += 1
        d['posts'] += a['posts_24h']
        d['clicks'] += a['clicks_24h']
        d['comments'] += a['comments_24h']
        d['bull'] += a['bull']
        d['bear'] += a['bear']
        d['scores'].append(a['consensus_score'])
        d['codes'].append(a['code'])
        d['members'].append(a)
    industries = sorted(ind.values(), key=lambda d: -sum(d['scores']) / len(d['scores']))
    for d in industries:
        d['avg_score'] = round(sum(d['scores']) / len(d['scores']))
        # 行业第一原因：贡献最大的个股及其焦点
        top = sorted(d['members'], key=lambda m: -m['consensus_score'])[:3]
        d['top'] = [{'name': m['name'], 'score': m['consensus_score'], 'focus': m.get('focus')} for m in top]
        lead = top[0]
        d['reason'] = (f"{lead['name']}({lead['consensus_score']}分)"
                       + (f" 领跑，焦点「{lead['focus']}」" if lead.get('focus') else ''))
        del d['scores'], d['members']

    anomalies = [f"{a['code']} {a['name']}：讨论量环比 {a['post_mom']:+.0%}" for a in agg_list if a.get('post_mom') and abs(a['post_mom']) >= 0.5]
    return {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'window_hours': 24,
        'market': market_panel,
        'stocks': stocks,
        'industries': industries,
        'anomalies': anomalies,
    }


def build_items(agg_list, topn=3):
    """共识分 TOP3 条目推 aihot（标题以事件短语领衔，供聚簇匹配）"""
    ranked = sorted([a for a in agg_list if not a.get('stale')], key=lambda a: -a['consensus_score'])
    items = []
    for a in ranked[:topn]:
        h = a['hot_posts'][0] if a['hot_posts'] else None
        if not h:
            continue
        items.append({
            'title': f"{h['title'][:30]}（股吧共识·{a['code']} 共识分{a['consensus_score']}·{a['posts_24h']}帖/阅读{a['clicks_24h']}，情绪{a['sentiment']}）",
            'url': h['url'], 'author': '股吧共识聚合',
            'publishedAt': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'raw': {'kind': 'guba_consensus', 'metrics': {k: a[k] for k in ('code', 'name', 'industry', 'consensus_score', 'posts_24h', 'clicks_24h', 'comments_24h', 'authors', 'bull', 'bear', 'sentiment', 'keywords')}},
        })
    return items


def push(items):
    if not AIHOT_TOKEN:
        print('[error] INGEST_TOKEN 未找到')
        return
    body = {'sourceId': SOURCE_ID, 'sourceName': SOURCE_NAME, 'items': items}
    r = requests.post(AIHOT_INGEST, json=body,
                      headers={'Authorization': f'Bearer {AIHOT_TOKEN}', 'Content-Type': 'application/json'},
                      timeout=30)
    print(f'[push] {r.status_code} {r.text[:160]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--push', action='store_true', help='聚合后推送 TOP3 给 aihot')
    ap.add_argument('--pages', type=int, default=1)
    args = ap.parse_args()

    meta = load_stock_meta()
    agg_list = []
    stale_n = 0
    blocked = 0  # 连续全失败计数：≥2 判定 IP 被封，跳过剩余抓取直接降级
    for i, code in enumerate(WATCHLIST):
        if i:
            time.sleep(1.5 + random.random() * 2)  # 吧间歇，防连抓触发限流
        print(f'爬取 {code} ...')
        rows = [] if blocked >= 2 else fetch_bar(code, pages=args.pages)
        if not rows:
            blocked += 1
            if blocked == 2:
                print('[note] 连续 2 只全失败，判定 IP 被限流，剩余股票直接走降级')
            fb = fallback_from_snapshot(code)
            if fb:
                agg_list.append(fb)
                stale_n += 1
                print(f'  [stale] {code} 反爬降级，用 {fb["stale_ts"]} 快照')
            else:
                print(f'  [skip] {code} 无数据且无历史')
            continue
        a = aggregate(code, rows)
        agg_list.append(a)
        blocked = 0  # 抓到数据即复位封禁计数
        print(f"  24h 帖 {a['posts_24h']} | 阅读 {a['clicks_24h']} | {a['sentiment']} | 关键词 {a['keywords'][:3]}")
    if stale_n:
        print(f'[note] {stale_n}/{len(WATCHLIST)} 只走反爬降级（限流冷却后重跑即刷新）')

    consensus = build_consensus(agg_list, meta)
    with open(CONSENSUS_JSON, 'w', encoding='utf-8') as f:
        json.dump(consensus, f, ensure_ascii=False, indent=1)
    print(f"\n细分共识已写入 {CONSENSUS_JSON}（{len(consensus['stocks'])} 个股 / {len(consensus['industries'])} 行业）")

    with open(SNAPSHOT_CSV, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(['ts', 'code', 'posts_24h', 'clicks_24h', 'comments_24h', 'bull', 'bear', 'sentiment', 'authors', 'hot1', 'hot1_url', 'kw'])
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        for a in agg_list:
            if a.get('stale'):
                continue  # 降级数据不入快照，避免污染基线
            h = a['hot_posts'][0] if a['hot_posts'] else {}
            w.writerow([ts, a['code'], a['posts_24h'], a['clicks_24h'], a['comments_24h'], a['bull'], a['bear'],
                        a['sentiment'], a.get('authors', ''), h.get('title', ''), h.get('url', ''), '|'.join(a['keywords'])])

    items = build_items(agg_list)
    print(f'=== 生成 {len(items)} 条 aihot 共识条目 ===')
    for it in items:
        print('-', it['title'][:80])
    if args.push:
        push(items)


if __name__ == '__main__':
    main()
