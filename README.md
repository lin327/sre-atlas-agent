# SRE Atlas Agent

自动采集 SRE / 运维知识的 Agent，从 RSS、GitHub Issues、官方文档中聚合内容，经 Claude API 生成结构化 wiki 页面，存入 PostgreSQL 去重追踪。

## 架构

```
数据源 → 采集器 → 去重 → LLM 生成 → 输出 .md 文件
                     ↓
              PostgreSQL（URL 去重 + 状态追踪）
```

## 项目结构

```
sre-atlas-agent/
├── agent/
│   ├── collectors/
│   │   ├── rss_collector.py      # RSS 采集（feedparser + 重试）
│   │   └── github_collector.py   # GitHub Issues/PR（REST API + 分页）
│   ├── generator.py              # Claude API 内容生成 + 质量门控
│   ├── dedup.py                  # PostgreSQL 去重
│   ├── scheduler.py              # 定时调度（默认 6 小时）
│   └── main.py                   # CLI 入口
├── config/
│   ├── sources.yaml              # 数据源配置（6 RSS + 3 GitHub + 2 Docs）
│   ├── settings.py               # 应用设置
│   └── database.sql              # PostgreSQL schema（3 张表）
├── requirements.txt
├── .env.example
└── README.md
```

## 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际凭证

# 4. 试运行（不写数据库）
python -m agent.main --once --dry-run

# 5. 正式运行
python -m agent.main --once

# 6. 持续运行（每 6 小时采集一次）
python -m agent.main
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | PostgreSQL 连接串 |
| `ANTHROPIC_API_KEY` | Claude API key |
| `GITHUB_TOKEN` | GitHub PAT（可选，提高 API 限流） |

## 数据源

在 `config/sources.yaml` 中配置：

| 类型 | 当前源 | 说明 |
|------|--------|------|
| RSS | Kubernetes Blog, Docker Blog, CNCF, Netflix Tech Blog, Google SRE 等 | feedparser 解析 |
| GitHub | kubernetes/kubernetes, containerd/containerd, etcd-io/etcd | Issues + PRs |
| Docs | Kubernetes Docs, Docker Docs | 文档站点 |

## 输出

- 生成的 wiki 页面写入 `output/` 目录
- 格式：带 frontmatter 的 Markdown（与 SRE Atlas 前端兼容）
- 包含 `[[wikilinks]]` 关联相关页面

## 配置项

`config/settings.py` 中的关键设置：

| 设置 | 默认值 | 说明 |
|------|--------|------|
| `MAX_ITEMS_PER_SOURCE` | 20 | 每个源每次最多采集条数 |
| `COLLECTION_INTERVAL_HOURS` | 6 | 采集间隔（小时） |
| `MIN_CONFIDENCE` | medium | 最低置信度阈值 |
| `MIN_CONTENT_LENGTH` | 200 | 最低内容长度（字符） |
| `CLAUDE_MODEL` | claude-sonnet-4-6 | 使用的 Claude 模型 |

## 数据库

PostgreSQL schema（`config/database.sql`）：

- `ingested_urls` — 已采集 URL 去重
- `wiki_pages` — 已生成页面追踪
- `source_health` — 数据源健康状态

## 相关仓库

- [sre-wiki](https://github.com/lin327/sre-wiki) — SRE Atlas 前端（Astro + React）
