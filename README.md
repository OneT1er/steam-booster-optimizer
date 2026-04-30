# Steam 卡牌补充包价值优化工具

输入 Steam 公开 ID 或游戏名，自动分析卡牌补充包的宝石兑换成本与市场价值，找出最划算的兑换组合。

## 功能

- **方式A**：输入 Steam ID，批量查询账号所有游戏，按回报率排序
- **方式B**：搜索游戏名，逐个分析指定的游戏
- 所有金额统一为**人民币**
- 绿色 = 盈利，红色 = 亏损

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
cp config.example.json config.json
# 编辑 config.json，填入你的 Steam API Key
# 申请地址: https://steamcommunity.com/dev/apikey

# 3. 启动
python main.py
# 打开 http://127.0.0.1:8000
```

## 计算逻辑

1. 获取游戏卡牌数量（内置数据库 + Steam 市场搜索）
2. 计算补充包宝石成本：`(卡牌数 - 1) × 100` 宝石
3. 查询 1000 宝石袋市场价 → 折算为人民币
4. 查询补包内卡牌总市场价 → 折算为人民币
5. 回报率 = (卡牌价值 - 宝石成本) / 宝石成本 × 100%

## 数据来源

- Steam Web API（游戏列表）
- Steam Market（卡牌价格、宝石袋价格）
- 本地 `card_db.json`（已知游戏卡牌数量，可持续扩充）

## 项目结构

```
steam-booster-optimizer/
├── main.py              # FastAPI 后端
├── templates/
│   └── index.html       # Web 界面
├── config.example.json  # 配置模板
├── config.json          # 实际配置（不提交）
├── card_db.json         # 卡牌数据库
├── requirements.txt
└── README.md
```

## 注意事项

- Steam ID 需在社区隐私设置中设为**公开**
- 市场数据实时波动，结果仅供参考
- 单次批量查询 170 款游戏约需 3-5 分钟（含请求间隔防限流）
