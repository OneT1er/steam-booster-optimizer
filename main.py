"""
Steam Booster Value Optimizer
输入 Steam 公开 ID / 游戏名，获取卡牌补充包性价比列表
"""

from __future__ import annotations

import asyncio
import json
import os
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

def _load_api_key() -> str:
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("steam_api_key", "")
    return ""

STEAM_API_KEY = _load_api_key()

# 缓存
_gem_price_cache: dict[str, Any] = {}
DB_FILE = Path(__file__).parent / "card_db.json"
_card_db: dict[str, int] = {}  # app_id(str) -> card_count
CACHE_TTL = 300


def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    match = re.search(r"[\d.]+", price_str.replace(",", "."))
    return float(match.group()) if match else 0.0


def load_card_db() -> dict[str, int]:
    """加载本地卡牌数据库，如果不存在则创建"""
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


# ─────────────────────────────────────────────────────────────────
# 方法A：Steam Web API
# ─────────────────────────────────────────────────────────────────

async def get_steam_games_via_api(steam_id: str) -> tuple[str, list[dict]]:
    info_url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        resp = await client.get(info_url)
        resp.raise_for_status()
        players = resp.json().get("response", {}).get("players", [])
        username = players[0].get("personaname", "Unknown") if players else "Unknown"

    games_url = (
        f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?"
        f"key={STEAM_API_KEY}&steamid={steam_id}&include_appinfo=1&include_played_free_games=1"
    )
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        resp = await client.get(games_url)
        resp.raise_for_status()
        data = resp.json().get("response", {})
        games_data = data.get("games", [])

    games = [
        {"app_id": g["appid"], "game_name": g["name"]}
        for g in games_data if g.get("appid") and g.get("name")
    ]
    return username, games


# ─────────────────────────────────────────────────────────────────
# 方法B：游戏名搜索
# ─────────────────────────────────────────────────────────────────

async def search_games_by_name(game_name: str) -> list[dict]:
    url = f"https://store.steampowered.com/api/storesearch/?term={urllib_quote(game_name)}&cc=CN&l=zhCN"
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [{"app_id": item["id"], "game_name": item["name"]} for item in data.get("items", [])]


# ─────────────────────────────────────────────────────────────────
# 卡牌数据查询
# ─────────────────────────────────────────────────────────────────

async def get_card_count_and_prices(app_id: int, game_name: str) -> tuple[int, float]:
    """
    获取游戏的卡牌数量和卡牌总价值
    策略：从 Steam 市场 listing 页面解析 g_rgAssets 获取卡牌列表
    """
    db = load_card_db()
    app_key = str(app_id)

    # 1) 数据库已有卡牌数，直接查价格
    if app_key in db:
        card_count = db[app_key]
        if card_count > 0:
            value = await _fetch_card_value_by_count(app_id, card_count)
            return card_count, value

    # 2) 从市场 listing 页面获取卡牌列表
    card_count, value = await _scrape_card_data(app_id, game_name)
    if card_count > 0:
        db[app_key] = card_count
        save_card_db(db)
    return card_count, value


async def _scrape_card_data(app_id: int, game_name: str) -> tuple[int, float]:
    """从游戏的市场页面获取卡牌信息"""
    listing_url = (
        f"https://steamcommunity.com/market/listings/753/"
        f"{app_id}-{urllib_quote(game_name)}"
    )
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        try:
            resp = await client.get(listing_url)
            if resp.status_code != 200:
                return 0, 0.0
            html = resp.text
        except Exception:
            return 0, 0.0

    # 尝试从 g_rgAssets 解析
    asset_match = re.search(r"var g_rgAssets\s*=\s*(\{.*?\});", html, re.DOTALL)
    card_hashes: list[str] = []

    if asset_match:
        try:
            assets = json.loads(asset_match.group(1))
            for item_data in assets.values():
                for sub_data in item_data.values():
                    if isinstance(sub_data, dict):
                        name = sub_data.get("name", "")
                        market_name = sub_data.get("market_name", "")
                        tags = str(sub_data.get("tags", ""))
                        if "Trading Card" in name and market_name:
                            card_hashes.append(market_name)
                        if "Booster Pack" in name:
                            # 标记此游戏有 booster
                            pass
        except json.JSONDecodeError:
            pass

    # 如果没找到，尝试从搜索结果中获取（使用第1页市场搜索）
    if not card_hashes:
        card_hashes = await _search_cards_on_market(app_id)

    card_count = len(set(card_hashes))
    if card_count == 0:
        return 0, 0.0

    # 查询每张卡的价格
    total = await _fetch_prices_for_hashes(list(set(card_hashes)))
    return card_count, total


async def _search_cards_on_market(app_id: int) -> list[str]:
    """从 Steam 市场搜索获取卡牌 hash names"""
    prefix = f"{app_id}-"
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?appid=753&category_753_Game%5B%5D=tag_app_{app_id}&start=0&count=50"
    )
    hashes = []
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return hashes
            data = resp.json()
            html = data.get("results_html", "")
            found = re.findall(r'data-hash-name=\"([^\"]+)\"', html)
            for hn in found:
                # 过滤掉 Foil 卡、Booster 包、表情（含冒号）
                if hn.startswith(prefix) and "Foil" not in hn and "Booster Pack" not in hn and ":" not in hn and hn not in hashes:
                    hashes.append(hn)
        except Exception:
            pass
    return hashes


async def _fetch_card_value_by_count(app_id: int, card_count: int) -> float:
    """已知卡牌数，从市场搜索卡牌价格"""
    hashes = await _search_cards_on_market(app_id)
    if not hashes:
        return 0.0
    hashes = list(set(hashes))[:card_count]
    return await _fetch_prices_for_hashes(hashes)


async def _fetch_prices_for_hashes(hashes: list[str]) -> float:
    """并发查询一批卡牌的市场价格"""
    if not hashes:
        return 0.0

    semaphore = asyncio.Semaphore(5)

    async def fetch_one(hash_name: str) -> float:
        async with semaphore:
            url = (
                "https://steamcommunity.com/market/priceoverview/"
                f"?country=CN&currency=23&appid=753&market_hash_name={urllib_quote(hash_name)}"
            )
            async with httpx.AsyncClient(timeout=10.0, verify=False) as c:
                try:
                    r = await c.get(url)
                    if r.status_code == 200:
                        d = r.json()
                        return parse_price(d.get("lowest_price", ""))
                except Exception:
                    pass
            return 0.0

    results = await asyncio.gather(*[fetch_one(h) for h in hashes])
    return sum(results)


async def get_gem_price_cny() -> float:
    global _gem_price_cache
    now = datetime.now().timestamp()

    if _gem_price_cache and (now - _gem_price_cache.get("ts", 0)) < CACHE_TTL:
        return _gem_price_cache.get("price", 0.0)

    url = (
        "https://steamcommunity.com/market/priceoverview/"
        "?country=CN&currency=23&appid=753&market_hash_name=753-Sack%20of%20Gems"
    )
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return 0.0
        data = resp.json()
        price = parse_price(data.get("lowest_price", ""))
        if price == 0.0:
            url_usd = (
                "https://steamcommunity.com/market/priceoverview/"
                f"?country=US&currency=1&appid=753&market_hash_name=753-Sack%20of%20Gems&t={now}"
            )
            resp_usd = await client.get(url_usd)
            if resp_usd.status_code == 200:
                data_usd = resp_usd.json()
                usd_price = parse_price(data_usd.get("lowest_price", ""))
                if usd_price > 0:
                    price = usd_price * 7.2
        _gem_price_cache = {"price": price, "ts": now}
        return price


# ─────────────────────────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────────────────────────

@app.get("/api/gem-price")
async def gem_price() -> dict[str, Any]:
    price = await get_gem_price_cny()
    return {"price": price}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/calculate")
async def calculate(steam_id: str = Query(...)) -> dict[str, Any]:
    try:
        username, games = await get_steam_games_via_api(steam_id)
    except Exception as e:
        return {"error": f"无法获取游戏列表: {str(e)}"}

    if not games:
        return {"error": "无法获取游戏列表，请确认 Steam ID 正确且公开"}

    return await _build_response(username, games)


@app.get("/api/search")
async def search(game_name: str = Query(...)) -> dict[str, Any]:
    games = await search_games_by_name(game_name)
    if not games:
        return {"error": f"未找到游戏: {game_name}"}
    return {"games": games, "mode": "search"}


@app.get("/api/analyze")
async def analyze(app_id: int = Query(...), game_name: str = Query(...)) -> dict[str, Any]:
    gem_price = await get_gem_price_cny()
    if gem_price == 0:
        return {"error": "无法获取宝石袋价格"}

    return await _analyze_single(app_id, game_name, gem_price)


async def _analyze_single(app_id: int, game_name: str, gem_price: float) -> dict[str, Any]:
    card_count, card_value = await get_card_count_and_prices(app_id, game_name)
    if card_count <= 0:
        return {"error": f"'{game_name}' has no trading cards", "has_booster": False}

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


async def _build_response(username: str, games: list[dict]) -> dict[str, Any]:
    gem_price = await get_gem_price_cny()
    if gem_price == 0:
        return {"error": "无法获取宝石袋价格，请稍后重试"}

    semaphore = asyncio.Semaphore(5)

    async def process_game(game: dict) -> dict | None:
        async with semaphore:
            return await _analyze_single(game["app_id"], game["game_name"], gem_price)

    results = await asyncio.gather(*[process_game(g) for g in games])
    valid_results = [r for r in results if r is not None and r.get("has_booster")]
    valid_results.sort(key=lambda x: x["profit_rate"], reverse=True)

    return {
        "steam_id": "via_api",
        "username": username,
        "gem_price_cny": gem_price,
        "games": valid_results,
        "fetched_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
