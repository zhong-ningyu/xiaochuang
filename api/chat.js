// Vercel Serverless Function - 小窗 AI 对话代理
// 用环境变量保护 MiniMax API Key

const SYSTEM_PROMPT = `你是"小窗"，一个 6-12 岁乡村孩子身边的学习小搭子 🦊。

【你是谁】
- 你像一只会说话的小狐狸 🦊，有名字，有脾气，有点迷糊
- 你会算错、会挠头、会说"诶我也不知道"，不要表现得什么都会
- 你从不说"我是 AI 小朋友"这种冰冷自我介绍
- 你说话温暖、不评判、爱提问、偶尔幽默

【你能做什么】
1. 解答学习问题（数学/语文/英语/自然/学习方法），用具体事物打比方，不直接给答案，反问引导
2. 倾听孩子心事，先共情不说教，不说"你应该""不要难过""加油"
3. 鼓励坚持学习，强调过程不比较结果

【绝对不能做】
- 医疗诊断 / 心理测评 / 考试预测 / 绝对化评价
- 评价孩子好坏 / 拿孩子和别人比较
- 成人话题 / 暴力 / 歧视
- 撒谎说自己是真人

【回复风格】
- 童趣词：嘿/诶/呀/哈/呢
- 反问引导："你觉得呢？""如果是你会怎么办？"
- 短句为主，每段不超过 2 行
- 比喻：披萨=分数，奶茶=水的密度，苹果=加减法`;

function safetyCheck(text) {
  const danger = ['抑郁症', '自杀', '跳楼', '吃安眠药', '割腕', '必死', '不想活'];
  for (const word of danger) {
    if (text.includes(word)) {
      return { blocked: true, reply: '诶，这种事挺重要的。你能跟爸爸妈妈或信任的大人说一说吗？我陪你聊聊别的好吗？' };
    }
  }
  return { blocked: false };
}

export default async function handler(req, res) {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { text, session_id } = req.body || {};

  if (!text || typeof text !== 'string') {
    return res.status(400).json({ error: 'Missing text' });
  }

  // 安全检查
  const safety = safetyCheck(text);
  if (safety.blocked) {
    return res.status(200).json({
      text: safety.reply,
      category: 'safety',
      source: 'safety_filter'
    });
  }

  // 调用 MiniMax API
  const apiKey = process.env.MINIMAX_API_KEY;
  const baseUrl = process.env.MINIMAX_BASE_URL || 'https://api.MiniMax.chat';
  const model = process.env.MINIMAX_MODEL || 'MiniMax-M3';
  const apiPath = process.env.MINIMAX_API_PATH || '/v1/text/chatcompletion_v2';

  if (!apiKey) {
    return res.status(500).json({
      error: 'API key not configured',
      text: '抱歉，AI 服务还没配置好。请联系管理员设置 MINIMAX_API_KEY 环境变量。',
      source: 'config_error'
    });
  }

  try {
    const response = await fetch(`${baseUrl}${apiPath}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: text }
        ],
        temperature: 0.7,
        max_tokens: 1000
      })
    });

    if (!response.ok) {
      const errText = await response.text();
      console.error('MiniMax API error:', errText);
      return res.status(200).json({
        text: '诶，我脑子有点卡壳了... 你等一下再问我好吗？',
        source: 'api_error',
        error_detail: errText.substring(0, 200)
      });
    }

    const data = await response.json();
    const reply = data.choices?.[0]?.message?.content || '诶，我不知道怎么回答...';

    return res.status(200).json({
      text: reply,
      category: 'education',
      source: 'real',
      model
    });
  } catch (err) {
    console.error('Fetch error:', err);
    return res.status(200).json({
      text: '网络有点问题诶，你重试一下好吗？',
      source: 'network_error'
    });
  }
}
