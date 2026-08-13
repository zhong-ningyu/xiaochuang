# 小窗后端 (Backend)

Flask + SQLite + MiniMax-M3 大模型。

## 启动

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env,设置 LLM_MODE 和 API Key
python app.py
```

## API 列表

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 返回前端 HTML |
| GET | `/api/health` | 健康检查 |
| POST | `/api/chat` | AI 对话 |
| GET | `/api/topics` | 话题列表 |
| GET | `/api/parent/stats` | 家长统计 |
| POST | `/api/parent/voice` | 家长留言 |
| POST | `/api/parent/clear` | 清空数据 |
| GET | `/api/messages/pending` | 待接收消息 |
| GET | `/api/story` | 故事接口 |

## 安全过滤

7 类敏感话题自动拦截：医疗诊断、心理测评、考试预测、绝对化评价、自杀倾向、暴力内容、未成年人保护。

详见 `app.py` 中的 `safety_check` 和 `detect_intent` 函数。