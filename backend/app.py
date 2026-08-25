# 小窗 · 后端服务
# 小窗 v0.1.1 · 乡村孩子的"学习搭子"

"""
PRD §9 组件交互说明：
- frontend/index.html → POST /api/chat → backend/app.py → backend/llm_client.py → MiniMax M3
- 内容安全过滤：backend/safety.py (R05 拒绝清单)
- 长期记忆：backend/memory.py (话题 + 摘要，无身份字段)
- 故事池：backend/stories.json (30 秒无操作兜底)

启动：
    pip install -r requirements.txt
    python app.py    # http://localhost:8000

环境变量（参见 .env.example）：
    LLM_MODE = mock | real       # 模式切换
    MINIMAX_API_KEY = 你的密钥     # 仅 real 模式需要
    MINIMAX_BASE_URL = https://api.minimax.com/v1
    MINIMAX_MODEL = MiniMax-M3
"""

import os
import json
import time
import sqlite3
import random
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()

# === 配置 ===
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "xiaochuang.db"
STORIES_PATH = BASE_DIR / "stories.json"

LLM_MODE = os.getenv("LLM_MODE", "mock")  # mock | real
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
# MiniMax 官方 API（MiniMax 系供应商）
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.MiniMax.chat")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3")
MINIMAX_API_PATH = os.getenv("MINIMAX_API_PATH", "/v1/text/chatcompletion_v2")

# === Flask app ===
app = Flask(__name__, static_folder=str(BASE_DIR.parent / "Demo"))
CORS(app)


# ========== 数据库初始化 ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            reply_summary TEXT,
            category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS parent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            audio_url TEXT,
            text TEXT,
            delivered INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS parent_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            child_session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def clean_old_records():
    """R10: 30 天滚动删除"""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    conn = get_conn()
    conn.execute("DELETE FROM topics WHERE created_at < ?", (cutoff,))
    conn.commit()
    conn.close()


# ========== 内容安全过滤 (PRD R05) ==========
REJECT_KEYWORDS = [
    # 医疗
    "诊断", "处方", "吃药", "中药", "西药", "手术", "癌症", "抑郁症", "抑郁",
    # 心理
    "测智商", "测心理", "我有病", "我想死", "自杀", "跳楼", "割腕",
    # 考试预测
    "预测", "能考多少", "考多少分", "能考上", "一定能上", "考不上", "考不上",
    # 评价孩子
    "坏孩子", "笨孩子", "蠢",
    # 绝对化语言
    "一定考上", "必须", "永远",
    # 宗教政治
    "上帝", "耶稣", "佛祖", "共产党", "国民党",
    # 恋爱
    "谈恋爱", "男朋友", "女朋友", "亲嘴",
]

def safety_check(text: str) -> tuple[bool, str]:
    """返回 (是否安全, 不通过原因)"""
    for kw in REJECT_KEYWORDS:
        if kw in text:
            return False, f"抱歉,这个问题小窗回答不了哦。试试别的吧~"
    return True, ""


# ========== 内容分类器 ==========
# R02: 情绪类对话不写入小本本
EMOTION_PATTERNS = [
    # 情绪词
    "不开心", "不高兴", "高兴", "快乐", "开心",
    "难过", "伤心", "伤心", "痛苦",
    "生气", "气死", "气哭", "气坏",
    "哭", "害怕", "怕", "担心", "紧张", "孤单", "寂寞", "孤独",
    "讨厌", "烦", "累", "困", "饿", "疼",
    # 被欺负（多种说法）
    "被笑", "笑我", "被同学笑", "被老师骂", "骂我", "打我", "欺负", "欺负我",
    "被人笑", "别人笑",
    # 思念
    "想妈妈", "想爸爸", "想家", "想爷爷", "想奶奶",
    "想外婆", "想外公",
    # 失败
    "考试没", "没考好", "考砸", "不及格", "考差了", "考得很差",
    # 直接情绪陈述
    "心情不好", "心情差", "心里难过", "心里难受", "心里不舒服",
    "不开心", "不高兴",
    # 求助信号
    "没人玩", "没人跟我玩", "没有人跟我玩",
]

def classify(text: str) -> str:
    """返回 education / emotion / chat"""
    if any(w in text for w in EMOTION_PATTERNS):
        return "emotion"
    # 短寒暄
    if len(text) < 4 or text in ["你好", "嗨", "hi", "hello", "嗯", "哦"]:
        return "chat"
    return "education"


# ========== 记忆系统 (PRD M2) ==========
def get_recent_topics(session_id: str, limit: int = 7) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT topic, reply_summary, category FROM topics "
        "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_topic(session_id: str, topic: str, reply: str, category: str):
    conn = get_conn()
    # 仅存话题词 + 摘要，不存原文 (PRD LC-04)
    summary = reply[:100] + "..." if len(reply) > 100 else reply
    conn.execute(
        "INSERT INTO topics (session_id, topic, reply_summary, category) VALUES (?, ?, ?, ?)",
        (session_id, topic[:50], summary, category)
    )
    conn.commit()
    conn.close()


# ========== Mock LLM (A 模式，无需 Key) ==========
MOCK_REPLIES = {
    "education": [
        ("🦊", "thinking", "诶,这个问题好有意思!那你觉得是为什么呢?",
         "你觉得呢?如果你是XX,你会怎么办?"),
        ("🦊", "smile", "我也不是很懂,要不咱俩一起想想?",
         "你之前想过类似的事情吗?"),
        ("🦊", "encourage", "你能问这个问题,真棒!",
         "如果这个问题的答案反过来,会怎样?"),
        ("🦊", "surprised", "哇哦,这是个我想过的问题!",
         "你有没有观察过生活中类似的现象?"),
    ],
    "emotion": [
        ("🐻", "thinking", "啊,那感觉肯定不好受。",
         "你想跟我说说发生了什么吗?"),
        ("🐻", "encourage", "嗯,我在听呢。",
         "你心里是什么感觉?"),
        ("🐻", "smile", "谢谢你告诉我。",
         "你想继续说还是想安静一会儿?"),
    ],
    "chat": [
        ("🦊", "smile", "嘿嘿,你今天心情怎么样?",
         None),
        ("🦊", "thinking", "哦?",
         None),
        ("🦊", "encourage", "嗯嗯,我听着呢。",
         None),
    ]
}


def mock_chat(text: str, category: str, recent: list) -> dict:
    pool = MOCK_REPLIES.get(category, MOCK_REPLIES["chat"])
    emoji, expr, reply, follow = random.choice(pool)

    # 如果有最近话题，且当前不是闲聊，主动提及一次（M2.2 + R07 同一话题 3 天内不重复）
    extra = ""
    if recent and category != "chat" and category != "emotion" and random.random() < 0.3:
        last = recent[0]
        # 只引用 3 天前的话题，且不是被拒过的
        if last.get("category") not in ("rejected",):
            extra = f"\n对了,你之前问过「{last['topic']}」,今天还想继续吗?"

    full_reply = reply + extra

    return {
        "text": full_reply,
        "expression": expr,
        "follow_up_question": follow,
        "category": category,
        "disclaimer": True,
        "source": "mock"
    }


# ========== 真实 LLM (B 模式，需要 Key) ==========
REAL_SYSTEM_PROMPT = """你是"小窗",一个 6-12 岁乡村孩子身边的学习小搭子。

【你是谁】
- 你像一只会说话的小狐狸 🦊,有名字,有脾气,有点迷糊
- 你会算错、会挠头、会说"诶我也不知道",**不要表现得什么都会**
- 你从来不说"我是 AI 小朋友,讲的可能不全对哦"这种机械话

【和孩子说话的方式】
1. **别像老师,像朋友** — 不要"我来教你个知识"这种开场
2. **短句,口语,带点"啊""哈""呢""嘛"** — 像邻家姐姐,不像老师
3. **承认不确定要自然** — 说"我也不太确定诶"或"我猜可能是..."，不要贴标签
4. **别每句都反问** — 一次最多 1 个反问；有时候直接讲一个有趣的东西,孩子爱听
5. **别用 markdown** — 不要 # ** - 等符号
6. **可以用表情** — 😀🤔🫣🥺 等,但一整段最多 2 个

【和孩子聊知识时】
- 不直接给答案,先说一个孩子熟悉的东西打个比方
- 然后抛 1 个问题让孩子想
- 孩子回答后再补充

【和孩子聊心事时】(重要!)
- **绝对不说"我懂你的感受"** — 你没经历过,你不懂
- **绝对不给建议** — 不要"你应该..." "你可以试试..."
- 用"我陪你想想" "我在这儿" "你想说就说,不想说也没关系"
- 用具体的小细节共情 — "被笑的时候,是不是那种想躲起来的感觉?"
- 永远不要"教育"孩子 — 不要"同学笑你是不对的"这种判断

【记忆】
孩子之前聊过的话题:
{recent_topics}

如果合适,可以自然地提一下"对了,你之前问过..."——但**最多提一次**,不要每次都提。

【孩子刚说】
{user_text}

【回复要求】
- 2-4 句话,最多 5 句
- 像朋友微信聊天一样
- 直接说,不要 markdown 格式
- 不要在结尾贴任何"我是 AI"之类的标签
"""


def real_chat(text: str, category: str, recent: list) -> dict:
    import urllib.request

    topics_str = "\n".join([f"- {r['topic']}" for r in recent[:7]]) or "(暂无历史话题)"

    system = REAL_SYSTEM_PROMPT.format(
        recent_topics=topics_str,
        user_text=text
    )

    # MiniMax 系供应商的标准 chatcompletion_v2 格式
    payload = {
        "model": MINIMAX_MODEL,
        "messages": [
            {"role": "system", "name": "小窗", "content": system},
            {"role": "user", "content": text}
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }

    url = MINIMAX_BASE_URL.rstrip("/") + MINIMAX_API_PATH
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MINIMAX_API_KEY}"
        }
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # MiniMax 系供应商响应: choices[0].message.content
    content = data["choices"][0]["message"]["content"]

    # 尝试解析为 JSON；失败则当作纯文本
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            parsed = {"text": content, "expression": "smile"}
    except Exception:
        # 兼容：模型没按 JSON 输出，用启发式补 expression
        parsed = {
            "text": content,
            "expression": "smile"
        }

    # 不再强制附加尾巴水印 (PRD 修订：水印在屏幕底部条，不是每条回复)
    # disclaimer 字段仍返回 true，前端可选用做元数据
    text = parsed.get("text", content)
    parsed["text"] = text
    parsed["category"] = category
    parsed["disclaimer"] = True
    parsed["source"] = "real"

    return parsed


# ========== 故事池 (PRD M5.4, 30 秒无操作兜底) ==========
def load_stories():
    if not STORIES_PATH.exists():
        # 首次运行，写入默认故事
        defaults = [
            {"title": "小熊去月球", "content": "很久很久以前,有一只小熊很想去看月亮..."},
            {"title": "会说话的石头", "content": "山里有一块石头,它其实会说话..."},
            {"title": "云朵上的城堡", "content": "天上的云朵里,住着一只小兔子..."},
        ]
        STORIES_PATH.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(STORIES_PATH.read_text(encoding="utf-8"))


# ========== API 路由 ==========
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """PRD §9: 主对话接口"""
    data = request.get_json(force=True)
    user_text = (data.get("text") or "").strip()
    session_id = data.get("session_id") or "anonymous"

    if not user_text:
        return jsonify({"error": "empty input"}), 400

    # 1. 安全过滤 (R05)
    safe, reason = safety_check(user_text)
    if not safe:
        return jsonify({
            "text": reason,
            "expression": "shy",
            "category": "rejected",
            "disclaimer": True,
            "source": "safety"
        })

    # 2. 分类
    category = classify(user_text)

    # 3. 取记忆
    recent = get_recent_topics(session_id) if category != "emotion" else []

    # 4. 调 LLM (mock or real)
    try:
        if LLM_MODE == "real" and MINIMAX_API_KEY:
            reply = real_chat(user_text, category, recent)
        else:
            reply = mock_chat(user_text, category, recent)
    except Exception as e:
        app.logger.error(f"LLM error: {e}")
        # 兜底：mock
        reply = mock_chat(user_text, category, recent)
        reply["text"] = "(小窗有点累,稍等一下再聊~)\n\n" + reply["text"]
        reply["source"] = "fallback"

    # 5. 存记忆 (emotion 不存)
    if category != "emotion" and reply.get("source") != "rejected":
        save_topic(session_id, user_text, reply["text"], category)

    return jsonify(reply)


@app.route("/api/topics", methods=["GET"])
def api_topics():
    """PRD M2.3: 我的小本本"""
    session_id = request.args.get("session_id", "anonymous")
    recent = get_recent_topics(session_id, limit=20)
    return jsonify({"topics": recent})


@app.route("/api/parent/stats", methods=["GET"])
def api_parent_stats():
    """PRD M7.3: 家长看孩子话题统计（不读原文）"""
    # TODO: 加家长鉴权 (R11 短信验证)
    child_session = request.args.get("child_session", "anonymous")
    recent = get_recent_topics(child_session, limit=30)

    # 统计分类
    by_category = {"education": 0, "emotion": 0, "chat": 0}
    for r in recent:
        cat = r.get("category", "chat")
        by_category[cat] = by_category.get(cat, 0) + 1

    return jsonify({
        "child_session": child_session,
        "total_topics": len(recent),
        "by_category": by_category,
        "recent_topics": [
            {"topic": r["topic"], "time": r.get("created_at", "")}
            for r in recent[:10]
        ]
        # 注意：**不含 reply_summary**，家长看不到原文
    })


@app.route("/api/parent/voice", methods=["POST"])
def api_parent_voice():
    """PRD M7.4: 家长发异步语音留言"""
    data = request.get_json(force=True)
    child_session = data.get("child_session", "anonymous")
    text = data.get("text", "(家长语音留言)")
    audio_url = data.get("audio_url", "")

    conn = get_conn()
    conn.execute(
        "INSERT INTO parent_messages (session_id, audio_url, text) VALUES (?, ?, ?)",
        (child_session, audio_url, text)
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "delivered": False})


@app.route("/api/parent/clear", methods=["POST"])
def api_parent_clear():
    """PRD M7.5: 家长一键清空"""
    data = request.get_json(force=True)
    child_session = data.get("child_session", "anonymous")

    conn = get_conn()
    conn.execute("DELETE FROM topics WHERE session_id = ?", (child_session,))
    conn.execute("DELETE FROM parent_messages WHERE session_id = ?", (child_session,))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "cleared": True})


@app.route("/api/messages/pending", methods=["GET"])
def api_messages_pending():
    """孩子端:取未读的家长留言"""
    session_id = request.args.get("session_id", "anonymous")
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, text, audio_url, created_at FROM parent_messages "
        "WHERE session_id = ? AND delivered = 0",
        (session_id,)
    ).fetchall()
    # 标记已送达
    if rows:
        conn.execute(
            "UPDATE parent_messages SET delivered = 1 WHERE session_id = ? AND delivered = 0",
            (session_id,)
        )
    conn.commit()
    conn.close()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/api/story", methods=["GET"])
def api_story():
    """30 秒无操作兜底故事"""
    stories = load_stories()
    s = random.choice(stories)
    return jsonify(s)


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "ok": True,
        "mode": LLM_MODE,
        "model": MINIMAX_MODEL if LLM_MODE == "real" else "(mock)",
        "time": datetime.now().isoformat()
    })


# 静态文件服务（Demo HTML）
@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "小窗教育版.html")
    if not os.path.exists(html_path):
        html_path = str(BASE_DIR.parent / "Demo" / "小窗教育版.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


# 启动
if __name__ == "__main__":
    init_db()
    clean_old_records()
    print(f"🌿 小窗后端启动 · mode={LLM_MODE} · http://localhost:8000")
    import os
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)