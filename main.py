"""
Steam Booster Value Optimizer
输入 Steam 公开 ID / 游戏名，获取卡牌补充包性价比列表
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote as urllib_quote

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

app = FastAPI(title="Steam Booster Value Optimizer")

templates = Jinja2Templates(directory="templates")

# ── 配置 ──
CONFIG_PATH = Path(__file__).parent / "config.json"
DB_FILE = Path(__file__).parent / "card_db.json"

STEAM_API_KEY: str = ""
_gem_price_cache: dict[str, Any] = {}
_card_db: dict[str, int] = {}
_market_semaphore = asyncio.Semaphore(3)  # 降低并发避免限流
_last_request_time = 0.0
_request_lock = asyncio.Lock()
CACHE_TTL = 600

# ── 浏览器级请求头，避免被 Akamai 拦截 ──
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://steamcommunity.com/market/",
    "Origin": "https://steamcommunity.com",
}


def _load_settings() -> str:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("steam_api_key", "")
    return ""


STEAM_API_KEY = _load_settings()


def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    match = re.search(r"[\d.]+", price_str.replace(",", "."))
    return float(match.group()) if match else 0.0


async def _rate_limit() -> None:
    """确保请求间隔至少 1.5 秒，避免触发 Akamai 限流"""
    global _last_request_time
    async with _request_lock:
        now = datetime.now().timestamp()
        wait = 1.5 - (now - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = datetime.now().timestamp()


async def _market_get(client: httpx.AsyncClient, url: str, retries: int = 3) -> httpx.Response:
    """带重试和限流的市场 API 请求"""
    for attempt in range(retries):
        await _rate_limit()
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 403:
                wait = (3 ** attempt) + random.uniform(1, 3)
                await asyncio.sleep(wait)
                continue
            return resp
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    raise Exception("Max retries exceeded")


def _create_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=15.0,
        verify=False,
        headers=HEADERS,
        follow_redirects=True,
        http2=False,
    )


async def _warmup_client(client: httpx.AsyncClient) -> None:
    """先访问 Steam 社区主站获取 session cookie，避免 Akamai 拦截"""
    await _rate_limit()
    await client.get("https://steamcommunity.com/market/")


# ── 数据库操作 ──

def load_card_db() -> dict[str, int]:
    if _card_db:
        return _card_db
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f:
            _card_db.update(json.load(f))
    return _card_db


def save_card_db(data: dict[str, int]) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _card_db.update(data)


# ── Steam Web API（官方 API，无 Akamai 限制）──

async def get_steam_games_via_api(steam_id: str) -> tuple[str, list[dict]]:
    async with _create_client() as client:
        info_url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
        resp = await client.get(info_url)
        resp.raise_for_status()
        players = resp.json().get("response", {}).get("players", [])
        username = players[0].get("personaname", "Unknown") if players else "Unknown"

        games_url = (
            f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
            f"key={STEAM_API_KEY}&steamid={steam_id}&include_appinfo=1&include_played_free_games=1"
        )
        resp = await client.get(games_url)
        resp.raise_for_status()
        data = resp.json().get("response", {})
        games_data = data.get("games", [])

    return username, [
        {"app_id": g["appid"], "game_name": g["name"]}
        for g in games_data if g.get("appid") and g.get("name")
    ]


# ── 游戏搜索 ──

async def search_games_by_name(game_name: str) -> list[dict]:
    url = f"https://store.steampowered.com/api/storesearch/?term={urllib_quote(game_name)}&cc=CN&l=zhCN"
    async with _create_client() as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [{"app_id": item["id"], "game_name": item["name"]} for item in data.get("items", [])]


# ── 市场数据查询 ──

async def _search_cards_on_market(app_id: int, client: httpx.AsyncClient) -> list[str]:
    """从 Steam 市场搜索获取卡牌 hash names"""
    prefix = f"{app_id}-"
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?appid=753&category_753_Game%5B%5D=tag_app_{app_id}&start=0&count=50"
    )
    hashes = []
    resp = await _market_get(client, url)
    if resp.status_code == 200:
        data = resp.json()
        html = data.get("results_html", "")
        found = re.findall(r'data-hash-name=\"([^\"]+)\"', html)
        for hn in found:
            if (hn.startswith(prefix) and "Foil" not in hn
                    and "Booster Pack" not in hn and ":" not in hn
                    and hn not in hashes):
                hashes.append(hn)
    return hashes


async def _fetch_prices(hashes: list[str], client: httpx.AsyncClient) -> float:
    """顺序查询卡牌价格（带限流，避免并发触发封锁）"""
    total = 0.0
    for hn in hashes:
        url = (
            "https://steamcommunity.com/market/priceoverview/"
            f"?country=CN&currency=23&appid=753&market_hash_name={urllib_quote(hn)}"
        )
        resp = await _market_get(client, url, retries=2)
        if resp.status_code == 200:
            d = resp.json()
            total += parse_price(d.get("lowest_price", ""))
        await asyncio.sleep(0.3)  # 卡牌价格查询间隔
    return total


async def _analyze_game(app_id: int, game_name: str, gem_price: float, client: httpx.AsyncClient) -> dict | None:
    """分析单个游戏"""
    db = load_card_db()
    app_key = str(app_id)

    # 获取卡牌数
    if app_key in db:
        card_count = db[app_key]
    else:
        hashes = await _search_cards_on_market(app_id, client)
        card_count = len(set(hashes))
        if card_count > 0:
            db[app_key] = card_count
            save_card_db(db)

    if card_count <= 0:
        return None

    # 获取卡牌价格
    hashes = await _search_cards_on_market(app_id, client)
    if not hashes:
        return None
    hashes = list({h for h in hashes if "Foil" not in h and "Booster Pack" not in h and ":" not in h})[:card_count]
    if not hashes:
        return None

    card_value = await _fetch_prices(hashes, client)

    booster_gems = (card_count - 1) * 100
    booster_cost = (booster_gems / 1000) * gem_price
    profit = card_value - booster_cost
    profit_rate = (profit / booster_cost * 100) if booster_cost > 0 else 0

    return {
        "app_id": app_id,
        "game_name": game_name,
        "booster_gems": booster_gems,
        "card_count": card_count,
        "card_total_value_cny": round(card_value, 2),
        "booster_cost_cny": round(booster_cost, 2),
        "profit_cny": round(profit, 2),
        "profit_rate": round(profit_rate, 2),
        "has_booster": True,
    }


async def get_gem_price_cny() -> float:
    global _gem_price_cache
    now = datetime.now().timestamp()

    if _gem_price_cache and (now - _gem_price_cache.get("ts", 0)) < CACHE_TTL:
        return _gem_price_cache.get("price", 0.0)

    price = 0.0
    async with _create_client() as client:
        await _warmup_client(client)
        url = (
            "https://steamcommunity.com/market/priceoverview/"
            "?country=CN&currency=23&appid=753&market_hash_name=753-Sack%20of%20Gems"
        )
        resp = await _market_get(client, url)
        if resp.status_code == 200:
            price = parse_price(resp.json().get("lowest_price", ""))

        if price == 0.0:
            url_usd = (
                "https://steamcommunity.com/market/priceoverview/"
                f"?country=US&currency=1&appid=753&market_hash_name=753-Sack%20of%20Gems&t={now}"
            )
            resp = await _market_get(client, url_usd)
            if resp.status_code == 200:
                usd_price = parse_price(resp.json().get("lowest_price", ""))
                if usd_price > 0:
                    price = usd_price * 7.2

    _gem_price_cache = {"price": price, "ts": now}
    return price


# ── API 路由 ──

@app.get("/api/gem-price")
async def api_gem_price() -> dict:
    return {"price": await get_gem_price_cny()}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/calculate")
async def calculate(steam_id: str = Query(...)) -> dict[str, Any]:
    """方式A：Steam ID 批量查询"""
    try:
        username, games = await get_steam_games_via_api(steam_id)
    except Exception as e:
        return {"error": f"无法获取游戏列表: {str(e)}"}

    if not games:
        return {"error": "无法获取游戏列表，请确认 Steam ID 正确且公开"}

    gem_price = await get_gem_price_cny()
    if gem_price == 0:
        return {"error": "无法获取宝石袋价格，请稍后重试"}

    valid_results = []
    async with _create_client() as client:
        await _warmup_client(client)
        # 顺序处理每个游戏，避免并发触发 Akamai 封锁
        for game in games:
            result = await _analyze_game(game["app_id"], game["game_name"], gem_price, client)
            if result:
                valid_results.append(result)

    valid_results.sort(key=lambda x: x["profit_rate"], reverse=True)

    return {
        "steam_id": "via_api",
        "username": username,
        "gem_price_cny": gem_price,
        "games": valid_results,
        "fetched_at": datetime.now().isoformat(),
    }


@app.get("/api/search")
async def search(game_name: str = Query(...)) -> dict[str, Any]:
    """方式B：按游戏名搜索"""
    games = await search_games_by_name(game_name)
    if not games:
        return {"error": f"未找到游戏: {game_name}"}
    return {"games": games, "mode": "search"}


@app.get("/api/analyze")
async def analyze(app_id: int = Query(...), game_name: str = Query(...)) -> dict[str, Any]:
    """方式B：分析单个游戏"""
    gem_price = await get_gem_price_cny()
    if gem_price == 0:
        return {"error": "无法获取宝石袋价格"}

    async with _create_client() as client:
        await _warmup_client(client)
        result = await _analyze_game(app_id, game_name, gem_price, client)

    if result is None:
        return {"error": f"'{game_name}' has no trading cards", "has_booster": False}
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
