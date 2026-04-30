# Steam 卡牌补充包价值优化工具

## 1. 项目概述

**项目名称：** Steam Booster Value Optimizer
**类型：** 本地 Web 应用（Python FastAPI + HTML）
**核心功能：** 输入 Steam 公开 ID，自动获取账户游戏列表，查询每个游戏的卡牌补充包兑换成本和卡牌市场价值，计算回报率，找出最划算的兑换组合。

---

## 2. 技术架构

```
┌─────────────────────────────────────────────────────────┐
│  前端 (HTML/JS)                                         │
│  - Steam ID 输入框                                      │
│  - 游戏列表表格（排序/筛选）                             │
│  - 加载状态展示                                         │
└─────────────────┬───────────────────────────────────────┘
                  │ HTTP /json
┌─────────────────▼───────────────────────────────────────┐
│  后端 (Python FastAPI)                                  │
│  - GET /api/calculate?steam_id={id}                     │
└─────────────────┬───────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────┐
│  数据源                                                 │
│  - Steam API (游戏列表、商店价格)                       │
│  - Steam Market API (宝石袋价格、卡牌价格)              │
│  - SteamGridDB API (booster 包兑换宝石数)               │
│  - 汇率换算 (Steam 商店 USD→CNY)                       │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 核心数据模型

### GameBoosterInfo
| 字段 | 类型 | 说明 |
|------|------|------|
| app_id | int | 游戏 App ID |
| game_name | string | 游戏名称 |
| booster_gems | int | 兑换单个 booster 包所需宝石数 |
| card_total_value_cny | float | 包内卡牌总价值（人民币） |
| booster_cost_cny | float | 兑换成本 = (booster_gems / 1000) × 1000宝石袋价格 |
| profit_cny | float | 利润 = card_total_value_cny - booster_cost_cny |
| profit_rate | float | 回报率 = profit_cny / booster_cost_cny × 100% |

---

## 4. API 设计

### GET /api/calculate?steam_id={steam_id}
返回用户所有"值得研究"的游戏列表（利润 > 0 或有卡牌数据）。

**响应：**
```json
{
  "steam_id": "123456789",
  "username": "UserName",
  "gem_price_cny": 6.50,
  "games": [
    {
      "app_id": 440,
      "game_name": "Team Fortress 2",
      "booster_gems": 700,
      "card_total_value_cny": 0.85,
      "booster_cost_cny": 0.595,
      "profit_cny": 0.255,
      "profit_rate": 42.86,
      "has_booster": true
    }
  ],
  "fetched_at": "2026-04-30T12:00:00Z"
}
```

---

## 5. 数据获取策略

### 5.1 Steam 公开游戏列表
- 端点：`https://steamcommunity.com/profiles/{steam_id}/games?tab=all&xml=1`
- 解析 XML 获取 app_id 和游戏名称

### 5.2 Booster 包宝石兑换数（SteamGridDB）
- 端点：`https://www.steamgriddb.com/api/game/{app_id}/booster`
- 返回 `gem_cost` 字段

### 5.3 1000 宝石袋价格（Steam Market）
- 端点：`https://steamcommunity.com/market/priceoverview/?country=CN&currency=23&appid=753&market_hash_name=Gem%20Bundle`
- currency=23 对应 CNY
- 解析 `lowest_price` 字段

### 5.4 包内卡牌总价值
- 从 SteamGridDB 获取该游戏的卡牌列表（`/api/game/{app_id}/cards`）
- 遍历每张卡，在 Steam Market 查询 `priceoverview`
- 累加所有卡牌价值

### 5.5 汇率
- 使用 Steam 商店人民币汇率（从商店首页或估算 1 USD ≈ 7.2 CNY）

---

## 6. 过滤与排序规则

### 过滤条件
- 只展示 `has_booster == true` 且 `booster_gems > 0` 的游戏
- 可选：只展示 `profit_cny > 0` 的游戏（盈利游戏）

### 排序
- 默认按 `profit_rate` 降序排列
- 用户可切换为按 `profit_cny` 或 `game_name` 排序

---

## 7. 错误处理

| 场景 | 处理方式 |
|------|---------|
| Steam ID 无效/私有 | 返回友好错误："无法获取游戏列表，请确认ID是否公开" |
| SteamGridDB 无某游戏数据 | 跳过该游戏，不展示 |
| Steam Market 价格获取失败 | 该游戏卡牌价值显示为"查不到" |
| 宝石袋价格获取失败 | 返回错误，不展示任何结果 |

---

## 8. 依赖

```
fastapi
uvicorn
httpx（异步 HTTP）
```
