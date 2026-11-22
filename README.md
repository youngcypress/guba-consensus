# 股吧共识（guba-consensus）

东财股吧散户讨论 → 细分共识打分 → 专栏展示 + 推送 [AIHOT 热点引擎](https://github.com/youngcypress/aihot-fin) 归组计热度。

单文件 Python 管线，零 Selenium、零数据库；展示层与 aihot 精选解耦，**天天有结果**。

## 它做什么

1. **抓**：观察池 30 只热门股的东财股吧列表页（服务端内嵌 `article_list`，requests+正则直抓）
2. **算**：24h 窗口发帖/阅读/评论、多空情绪、jieba 关键词、热帖 TOP3、相对 24h 前快照的环比
3. **打分**：共识分 = 0.4×参与广度 + 0.35×激增度 + 0.25×方向一致（组内百分位）
4. **写原因**：免费 Qwen 生成每卡「讨论焦点 + 驱动消息」（输入哈希缓存，数据不变 0 调用）
5. **两路输出**：
   - **主**：`consensus_latest.json` → 基金网页 `/api/consensus` 专栏
   - **旁路**：TOP3 共识条目 `POST /api/ingest/items`（`hot_signal`，零 LLM 分析）→ 向量归组进媒体事件、计热度

## 快速开始

```bash
# 依赖：requests、jieba、pandas
pip install requests jieba pandas

# 本地跑（爬+聚合，写 JSON/CSV，不推送）
python guba_consensus.py

# 同时把 TOP3 推给本机/服务器 aihot
python guba_consensus.py --push
```

### 环境变量（可选，默认兼容 Windows 开发机）

| 变量 | 含义 | 默认 |
|------|------|------|
| `AIHOT_ENV_FILE` | 读 `INGEST_TOKEN` / `SF_API_KEY` 的 .env 路径 | `D:/quant_baibaibai/aihot-fin/.env` |
| `AIHOT_INGEST_URL` | 推送地址 | `http://127.0.0.1:3001/api/ingest/items` |
| `STOCK_INFO_CSV` | 个股名/申万行业映射（缺省用仓库内 `stock_meta.csv`） | 本机数据路径 |

服务器 crontab 示例（每日 08:30）：

```bash
30 8 * * * cd /opt/guba-consensus && export AIHOT_ENV_FILE=/opt/aihot-fin/.env && git pull -q origin main && python3 guba_consensus.py --push >> run.log 2>&1
```

## 产物（均在仓库外或被 gitignore）

| 文件 | 用途 |
|------|------|
| `consensus_latest.json` | 细分共识卡（个股×行业），专栏主数据源 |
| `guba_snapshot.csv` | 环比基线快照（**降级数据不入表**，防污染） |
| `llm_focus_cache.json` | 焦点/驱动消息缓存 |
| `stance_cache.json` | 标题操作立场缓存 |

数据文件**不进 Git**；服务器用 scp 播种基线，之后每日 `git pull` 只更代码。

## 模型与降本（2026-11-22）

| 步骤 | 模型 |
|------|------|
| 立场分类、焦点/驱动消息 | 硅基流动免费 `Qwen/Qwen2.5-14B-Instruct`（读 `.env` 的 `SF_API_KEY`） |
| 向量（归组旁路，引擎侧） | `BAAI/bge-m3` 免费档 |

不再调用 MIMO。

## 反爬（重要）

东财股吧是 **IP 级频控**：短时间连抓多吧会回 2.8KB 空壳页。脚本已做：

- 吧间歇 1.5~3.5s + 页间 2.5~4s + 退避重试 45/90s
- **连续 2 只全失败 → 判定封禁，直接止损降级**（用上一快照，绝不写零）

**日更节奏安全**；短时间多轮连测会封数小时。

## 与 aihot 的关系

```
guba_consensus.py --push
    → POST /api/ingest/items  (Bearer INGEST_TOKEN)
    → 信源 guba-consensus，participation_mode=hot_signal
    → 零模型分析，只作热度证据与向量归组
```

- **展示不依赖 aihot 精选**：专栏直接读 `consensus_latest.json`
- **热度旁路依赖 embedding**：引擎需配 `EMBEDDING_*`，否则 signal 永远 unmatched 且不计热度
- 引擎仓库：[youngcypress/aihot-fin](https://github.com/youngcypress/aihot-fin)（基于 [KKKKhazix/AIHOT](https://github.com/KKKKhazix/AIHOT) 的财经改造）

## 文档

- 链路/频率/token 实测/上云清单：[`更新链路说明.md`](更新链路说明.md)
- 部署与降本细节：知识库《AIHOT本机部署踩坑与上云迁移清单》《股吧共识管线与原因分析》

## License

MIT（见仓库 `LICENSE` 若存在；无则按 MIT 使用，保留版权声明）。
