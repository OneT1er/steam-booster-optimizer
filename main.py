"""
Steam Booster Value Optimizer
输入 Steam 公开 ID，获取游戏卡牌补充包性价比列表
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

app = FastAPI(title="Steam Booster Value Optimizer")

templates = Jinja2Templates(directory="templates")

# 缓存
_gem_price_cache: dict[str, Any] = {}
CACHE_TTL = 300  # 5分钟


def parse_price(price_str: str) -> float:
    """解析 Steam 价格字符串，如 '¥60.00' -> 60.0"""
    if not price_str:
        return 0.0
    match = re.search(r"[\d.]+", price_str.replace(",", "."))
    return float(match.group()) if match else 0.0


async def get_steam_games(steam_id: str) -> tuple[str, list[dict]]:
    """获取用户游戏列表，返回 (username, games)"""
    url = f"https://steamcommunity.com/profiles/{steam_id}/games?tab=all&xml=1"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

    username_match = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", text)
    username = username_match.group(1) if username_match else "Unknown"

    games = []
    game_blocks = re.findall(r"<game>.*?</game>", text, re.DOTALL)
    for block in game_blocks:
        app_id = re.search(r"<appID>(\d+)</appID>", block)
        name = re.search(r"<name><!\[CDATA\[(.*?)\]\]></name>", block)
        if app_id and name:
            games.append({
                "app_id": int(app_id.group(1)),
                "game_name": name.group(1),
            })
    return username, games


async def get_booster_gems(app_id: int) -> int:
    """从 SteamGridDB 获取 booster 包所需宝石数"""
    url = f"https://www.steamgriddb.com/api/game/{app_id}/booster"
    headers = {"Authorization": "Bearer "}  # 免费接口，默认可用
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return 0
        data = resp.json()
        return data.get("data", {}).get("gem_cost", 0)


async def get_card_values(app_id: int) -> float:
    """从 SteamGridDB 获取卡牌列表，再查 Steam Market 累加价格"""
    # 先获取卡牌列表
    cards_url = f"https://www.steamgriddb.com/api/game/{app_id}/cards"
    headers = {"Authorization": "Bearer "}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(cards_url, headers=headers)
        if resp.status_code != 200:
            return 0.0
        cards = resp.json().get("data", [])
        if not cards:
            return 0.0

    # 批量查询市场价值
    total = 0.0
    semaphore = asyncio.Semaphore(5)

    async def fetch_card(card: dict) -> float:
        async with semaphore:
            name = card.get("name", "")
            market_hash = f"753-{app_id}-{name}"
            market_url = (
                "https://steamcommunity.com/market/priceoverview/"
                f"?country=CN&currency=23&appid=753&market_hash_name={httpx.utils.quote(market_hash)}"
            )
            async with httpx.AsyncClient(timeout=10.0) as c:
                try:
                    r = await c.get(market_url)
                    if r.status_code == 200:
                        d = r.json()
                        return parse_price(d.get("lowest_price", ""))
                except Exception:
                    pass
            return 0.0

    results = await asyncio.gather(*[fetch_card(c) for c in cards])
    return sum(results)


async def get_gem_price_cny() -> float:
    """获取 1000 宝石袋人民币价格，带缓存"""
    global _gem_price_cache
    now = datetime.now().timestamp()

    if _gem_price_cache and (now - _gem_price_cache.get("ts", 0)) < CACHE_TTL:
        return _gem_price_cache.get("price", 0.0)

    url = (
        "https://steamcommunity.com/market/priceoverview/"
        "?country=CN&currency=23&appid=753&market_hash_name=Gem%20Bundle"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return 0.0
        data = resp.json()
        price = parse_price(data.get("lowest_price", ""))
        _gem_price_cache = {"price": price, "ts": now}
        return price


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/calculate")
async def calculate(steam_id: str = Query(...)) -> dict[str, Any]:
    """聚合接口：获取游戏列表 → 查询 booster 信息 → 计算回报率"""
    try:
        username, games = await get_steam_games(steam_id)
    except Exception as e:
        return {"error": f"无法获取游戏列表: {str(e)}"}

    if not games:
        return {"error": "无法获取游戏列表，请确认 Steam ID 公开"}

    gem_price = await get_gem_price_cny()
    if gem_price == 0:
        return {"error": "无法获取宝石袋价格，请稍后重试"}

    # 并发查询每个游戏的 booster 信息
    semaphore = asyncio.Semaphore(10)

    async def process_game(game: dict) -> dict | None:
        async with semaphore:
            app_id = game["app_id"]
            booster_gems = await get_booster_gems(app_id)
            if booster_gems <= 0:
                return None

            card_value = await get_card_values(app_id)
            booster_cost = (booster_gems / 1000) * gem_price
            profit = card_value - booster_cost
            profit_rate = (profit / booster_cost * 100) if booster_cost > 0 else 0

            return {
                "app_id": app_id,
                "game_name": game["game_name"],
                "booster_gems": booster_gems,
                "card_total_value_cny": round(card_value, 2),
                "booster_cost_cny": round(booster_cost, 2),
                "profit_cny": round(profit, 2),
                "profit_rate": round(profit_rate, 2),
                "has_booster": True,
            }

    results = await asyncio.gather(*[process_game(g) for g in games])
    valid_results = [r for r in results if r is not None]

    # 按回报率降序
    valid_results.sort(key=lambda x: x["profit_rate"], reverse=True)

    return {
        "steam_id": steam_id,
        "username": username,
        "gem_price_cny": gem_price,
        "games": valid_results,
        "fetched_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
