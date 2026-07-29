#!/usr/bin/env python3
"""
memory.py — 轻量级文件记忆系统 v2.0 (零依赖, 主动召回)

替代 memory-lancedb-pro, 使用 JSON 文件存储 + 关键词搜索 + 自动召回。
设计目标: 跨会话持久、零配置、即用即走、预测前自动注入铁律。

v2.0 新增:
  - auto_recall(): 根据用户输入自动检索相关记忆
  - load_critical_rules(): 加载 CRITICAL_RULES.md 铁律
  - validate_match_id(): 验证体彩编号格式
  - get_prediction_context(): 预测前一键获取上下文

用法:
  # 存储记忆
  python memory.py store "sporttery固定奖金更新时丢失" --category error --importance 0.9

  # 搜索记忆
  python memory.py recall "固定奖金"

  # 自动召回 (根据用户输入文本自动匹配相关记忆)
  python memory.py auto-recall "预测260729001"

  # 加载铁律
  python memory.py rules

  # 验证编号
  python memory.py check 260729001

  # 预测前获取上下文
  python memory.py context "260729001" "预测"

  # 列出全部
  python memory.py list

  # 统计
  python memory.py stats

  # Python 内调用
  from memory import MemoryStore
  ms = MemoryStore()
  ms.store("某条记忆", category="fact", importance=0.8)
  results = ms.recall("搜索词")
  context = ms.get_prediction_context(match_ids=["260729001"], user_input="预测260729001")
"""

import json
import os
import re
import time
from datetime import datetime
from collections import Counter

# ============================================================
# 配置
# ============================================================
_WORKSPACE = os.environ.get('SPORTTERY_WORKSPACE') or os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(_WORKSPACE, 'predictions', 'memory_store.json')
CRITICAL_RULES_FILE = os.path.join(_WORKSPACE, 'CRITICAL_RULES.md')

# 6 分类体系 (与 memory-lancedb-pro 兼容)
CATEGORIES = ['preference', 'fact', 'decision', 'entity', 'reflection', 'other']

# 分类中文名映射
CATEGORY_CN = {
    'preference': '偏好',
    'fact': '事实',
    'decision': '决策',
    'entity': '实体',
    'reflection': '反思',
    'other': '其他',
}

# 体彩编号正则: 9位完整编号 (如 260729001) 或 6位日期+3位序号 (如 260729 001)
_MATCH_ID_PATTERN = re.compile(r'\b(\d{9})\b')
_PARTIAL_MATCH_PATTERN = re.compile(r'\b(\d{6})\s*(\d{3})\b')

# 自动召回关键词映射: 用户输入中出现这些关键词时, 自动搜索对应标签的记忆
_AUTO_RECALL_KEYWORDS = {
    '编号': ['编号格式', 'matchNumStr', '体彩编号'],
    'matchNum': ['编号格式', 'matchNumStr'],
    '预测': ['预测', 'predict', '铁律'],
    'predict': ['predict', '铁律'],
    '赔率': ['赔率', 'odds', 'HAD', 'HHAD'],
    'odds': ['odds', 'HAD', 'HHAD'],
    'xG': ['xG', 'PPDA', '联赛限制'],
    'ppda': ['PPDA', '压迫', '联赛限制'],
    '缓存': ['缓存', 'cache', 'TTL'],
    'cache': ['cache', 'TTL'],
    '回退': ['回退', 'fallback', '降级'],
    'fallback': ['fallback', '降级'],
    '联赛': ['联赛', 'league', '标定'],
    'league': ['league', '标定'],
    '盘口': ['盘口', 'goalLine', '让球'],
    'nowscore': ['nowscore', '端点', '降级'],
    'sporttery': ['sporttery', '体彩', '固定奖金'],
    '体彩': ['体彩', 'sporttery', '固定奖金'],
}


class MemoryStore:
    """文件记忆存储 — JSON 持久化 + 关键词搜索"""

    def __init__(self, path=None):
        self.path = path or MEMORY_FILE
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._cache = None

    def _load(self):
        """加载记忆库 (带内存缓存)"""
        if self._cache is not None:
            return self._cache
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._cache = {'memories': [], 'version': '1.0'}
        else:
            self._cache = {'memories': [], 'version': '1.0'}
        return self._cache

    def _save(self):
        """保存记忆库"""
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def store(self, text, category='other', importance=0.7, scope='global', tags=None):
        """存储一条记忆

        Args:
            text: 记忆内容
            category: 分类 (preference/fact/decision/entity/reflection/other)
            importance: 重要性 0-1
            scope: 作用域 (global / agent:xxx / project:xxx)
            tags: 标签列表 (可选)

        Returns:
            memory_id (8位短ID)
        """
        if category not in CATEGORIES:
            category = 'other'

        data = self._load()
        # 生成 ID: 时间戳 + 序号
        ts = int(time.time() * 1000)
        mid = f"mem_{ts:x}"[-12:]

        entry = {
            'id': mid,
            'text': text,
            'category': category,
            'category_cn': CATEGORY_CN.get(category, category),
            'importance': round(importance, 2),
            'scope': scope,
            'tags': tags or [],
            'timestamp': ts,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'access_count': 0,
        }

        # 去重检查: 相同文本不重复存储
        for existing in data['memories']:
            if existing['text'] == text:
                # 更新重要性和访问次数
                existing['importance'] = max(existing['importance'], importance)
                existing['access_count'] += 1
                existing['last_accessed'] = entry['created_at']
                self._save()
                return existing['id']

        data['memories'].append(entry)
        self._save()
        return mid

    def recall(self, query, limit=5, category=None, scope=None):
        """搜索记忆 (关键词匹配 + 重要性排序)

        Args:
            query: 搜索关键词
            limit: 最多返回条数
            category: 过滤分类 (可选)
            scope: 过滤作用域 (可选)

        Returns:
            匹配的记忆列表
        """
        data = self._load()
        query_lower = query.lower()
        query_terms = re.split(r'[\s,，、]+', query_lower)

        scored = []
        for mem in data['memories']:
            # 分类过滤
            if category and mem['category'] != category:
                continue
            if scope and mem['scope'] != scope:
                continue

            text_lower = mem['text'].lower()
            tags_lower = [t.lower() for t in mem.get('tags', [])]

            # 评分: 关键词命中数 × 重要性
            score = 0
            for term in query_terms:
                if term in text_lower:
                    score += 2  # 正文命中权重高
                if any(term in t for t in tags_lower):
                    score += 1  # 标签命中

            if score > 0:
                # 时间衰减: 30天半衰期
                age_days = (time.time() - mem['timestamp'] / 1000) / 86400
                recency = 0.5 ** (age_days / 30)
                # 综合分: 命中分 × 0.5 + 重要性 × 0.3 + 时间新鲜度 × 0.2
                final_score = score * 0.5 + mem['importance'] * 0.3 + recency * 0.2
                scored.append((final_score, mem))

        # 排序并取 top N
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [mem for _, mem in scored[:limit]]

        # 更新访问计数
        for mem in results:
            mem['access_count'] = mem.get('access_count', 0) + 1
            mem['last_accessed'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._save()

        return results

    def list(self, limit=20, category=None, scope=None):
        """列出记忆 (按时间倒序)"""
        data = self._load()
        mems = data['memories']
        if category:
            mems = [m for m in mems if m['category'] == category]
        if scope:
            mems = [m for m in mems if m['scope'] == scope]
        mems = sorted(mems, key=lambda m: m['timestamp'], reverse=True)
        return mems[:limit]

    def stats(self):
        """统计信息"""
        data = self._load()
        mems = data['memories']
        cat_count = Counter(m['category_cn'] for m in mems)
        avg_importance = sum(m['importance'] for m in mems) / len(mems) if mems else 0
        return {
            'total': len(mems),
            'by_category': dict(cat_count),
            'avg_importance': round(avg_importance, 2),
            'last_updated': mems[-1]['created_at'] if mems else 'N/A',
        }

    def forget(self, query=None, memory_id=None):
        """删除记忆 (按搜索词或ID)"""
        data = self._load()
        before = len(data['memories'])
        if memory_id:
            data['memories'] = [m for m in data['memories'] if not m['id'].startswith(memory_id)]
        elif query:
            query_lower = query.lower()
            data['memories'] = [
                m for m in data['memories']
                if query_lower not in m['text'].lower()
            ]
        self._save()
        return before - len(data['memories'])

    def update(self, memory_id, text=None, importance=None, category=None):
        """更新记忆"""
        data = self._load()
        for mem in data['memories']:
            if mem['id'].startswith(memory_id):
                if text:
                    mem['text'] = text
                if importance is not None:
                    mem['importance'] = round(importance, 2)
                if category and category in CATEGORIES:
                    mem['category'] = category
                    mem['category_cn'] = CATEGORY_CN.get(category, category)
                mem['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._save()
                return True
        return False

    # ============================================================
    # v2.0 新增: 主动召回机制
    # ============================================================

    def load_critical_rules(self):
        """加载 CRITICAL_RULES.md 铁律文件

        Returns:
            list[dict]: 铁律列表, 每条含 id, title, trigger, rule, action, violation
        """
        if not os.path.exists(CRITICAL_RULES_FILE):
            return []

        with open(CRITICAL_RULES_FILE, 'r', encoding='utf-8') as f:
            content = f.read()

        rules = []
        # 解析格式: ### RULE-XXX: 标题 后跟 - **触发**/- **规则**/- **行动**/- **违反后果**
        pattern = r'### (RULE-\d+): (.+?)\n((?:- \*\*.+?\*\*.+\n?)+)'
        for match in re.finditer(pattern, content):
            rule_id = match.group(1)
            title = match.group(2)
            body = match.group(3)

            rule_entry = {'id': rule_id, 'title': title}
            for line in body.strip().split('\n'):
                line = line.strip()
                if line.startswith('- **触发**'):
                    rule_entry['trigger'] = line.replace('- **触发**:', '').strip()
                elif line.startswith('- **规则**'):
                    rule_entry['rule'] = line.replace('- **规则**:', '').strip()
                elif line.startswith('- **行动**'):
                    rule_entry['action'] = line.replace('- **行动**:', '').strip()
                elif line.startswith('- **违反后果**'):
                    rule_entry['violation'] = line.replace('- **违反后果**:', '').strip()

            rules.append(rule_entry)

        return rules

    def auto_recall(self, user_input):
        """根据用户输入自动检索相关记忆 (模拟 memory-lancedb-pro 的 autoRecall)

        分析用户输入中的关键词和体彩编号, 自动搜索匹配的记忆和铁律。

        Args:
            user_input: 用户输入文本 (如 "预测260729001")

        Returns:
            dict: {
                'critical_rules': 铁律列表,
                'matched_memories': 匹配的记忆列表,
                'match_id_validation': 编号验证结果,
                'warnings': 警告列表,
            }
        """
        result = {
            'critical_rules': [],
            'matched_memories': [],
            'match_id_validation': {},
            'warnings': [],
        }

        # 1. 始终加载所有铁律
        result['critical_rules'] = self.load_critical_rules()

        # 2. 检测体彩编号
        match_ids = _MATCH_ID_PATTERN.findall(user_input)
        partial_ids = _PARTIAL_MATCH_PATTERN.findall(user_input)
        # 合并: 部分编号拼成完整编号
        for date_part, seq_part in partial_ids:
            full_id = date_part + seq_part
            if full_id not in match_ids:
                match_ids.append(full_id)

        for mid in match_ids:
            validation = self.validate_match_id(mid)
            result['match_id_validation'][mid] = validation
            if validation['warnings']:
                result['warnings'].extend(validation['warnings'])

        # 3. 关键词触发召回
        search_terms = set()
        input_lower = user_input.lower()
        for keyword, tags in _AUTO_RECALL_KEYWORDS.items():
            if keyword.lower() in input_lower:
                search_terms.update(tags)

        # 如果有编号, 也搜索编号相关记忆
        if match_ids:
            search_terms.update(['编号格式', 'matchNumStr', '体彩编号', '回退逻辑'])

        # 执行搜索
        for term in search_terms:
            memories = self.recall(term, limit=3)
            for mem in memories:
                if mem not in result['matched_memories']:
                    result['matched_memories'].append(mem)

        # 按重要性排序
        result['matched_memories'].sort(key=lambda m: m['importance'], reverse=True)
        result['matched_memories'] = result['matched_memories'][:10]

        return result

    def validate_match_id(self, match_id):
        """验证体彩编号格式

        体彩编号格式: YYMMDD + NNN (9位)
        例如: 260729001 = 2026年07月29日第001场

        Args:
            match_id: 9位体彩编号字符串

        Returns:
            dict: {
                'valid': bool,
                'date': 解析出的日期,
                'sequence': 序号,
                'warnings': 警告列表,
            }
        """
        warnings = []
        result = {'valid': True, 'date': '', 'sequence': '', 'warnings': warnings}

        if len(match_id) != 9 or not match_id.isdigit():
            result['valid'] = False
            warnings.append(f"编号格式错误: {match_id} 不是9位数字")
            return result

        # 解析日期部分: YYMMDD
        date_str = match_id[:6]
        seq_str = match_id[6:]
        result['sequence'] = seq_str

        try:
            year = 2000 + int(date_str[:2])
            month = int(date_str[2:4])
            day = int(date_str[4:6])
            result['date'] = f"{year}-{month:02d}-{day:02d}"

            if month < 1 or month > 12:
                warnings.append(f"月份无效: {month}")
                result['valid'] = False
            if day < 1 or day > 31:
                warnings.append(f"日期无效: {day}")
                result['valid'] = False
        except (ValueError, IndexError):
            result['valid'] = False
            warnings.append(f"日期解析失败: {date_str}")

        # 搜索记忆库中关于此编号的已有记录
        existing = self.recall(match_id, limit=3)
        for mem in existing:
            if match_id in mem['text']:
                warnings.append(f"记忆库已有此编号记录: {mem['text'][:100]}...")

        # 检查铁律中是否有编号相关规则
        rules = self.load_critical_rules()
        for rule in rules:
            if '编号' in rule.get('title', '') or 'matchNum' in rule.get('title', ''):
                warnings.append(f"铁律提醒 [{rule['id']}]: {rule['title']}")

        return result

    def get_prediction_context(self, match_ids=None, user_input=None):
        """预测前一键获取完整上下文 (铁律 + 相关记忆 + 编号验证)

        应在每次预测任务开始前调用, 将返回的上下文注入预测流程。

        Args:
            match_ids: 体彩编号列表 (可选)
            user_input: 用户原始输入 (可选, 用于关键词召回)

        Returns:
            str: 格式化的上下文字符串, 可直接注入预测流程
        """
        lines = []
        lines.append("=" * 60)
        lines.append("  记忆系统: 预测前上下文注入 (auto_recall v2.0)")
        lines.append("=" * 60)

        # 1. 加载铁律
        rules = self.load_critical_rules()
        if rules:
            lines.append("\n>>> 铁律 (必须遵守):")
            for rule in rules:
                lines.append(f"  [{rule['id']}] {rule['title']}")
                if 'rule' in rule:
                    lines.append(f"    规则: {rule['rule']}")
                if 'action' in rule:
                    lines.append(f"    行动: {rule['action']}")
                if 'violation' in rule:
                    lines.append(f"    违反后果: {rule['violation']}")
                lines.append("")

        # 2. 编号验证
        if match_ids:
            lines.append(">>> 编号验证:")
            for mid in match_ids:
                validation = self.validate_match_id(mid)
                status = "OK" if validation['valid'] else "ERROR"
                lines.append(f"  [{status}] {mid} -> 日期={validation['date']}, 序号={validation['sequence']}")
                for w in validation['warnings']:
                    lines.append(f"    WARNING: {w}")
            lines.append("")

        # 3. 关键词召回
        search_input = user_input or ' '.join(match_ids or [])
        if search_input:
            auto_result = self.auto_recall(search_input)
            if auto_result['matched_memories']:
                lines.append(">>> 相关记忆 (自动召回):")
                for mem in auto_result['matched_memories'][:5]:
                    lines.append(f"  [{mem['id']}] {mem['category_cn']} (importance={mem['importance']})")
                    # 截取前150字符
                    text_preview = mem['text'][:150].replace('\n', ' ')
                    lines.append(f"    {text_preview}...")
                    lines.append("")

        # 4. 全局警告
        all_warnings = []
        if match_ids:
            for mid in match_ids:
                v = self.validate_match_id(mid)
                all_warnings.extend(v['warnings'])
        if all_warnings:
            lines.append(">>> 警告汇总:")
            for w in all_warnings:
                lines.append(f"  ! {w}")
            lines.append("")

        lines.append("=" * 60)
        return '\n'.join(lines)


# ============================================================
# CLI 接口
# ============================================================
def _format_mem(mem):
    """格式化单条记忆用于终端输出"""
    stars = '★' * int(mem['importance'] * 5)
    tags = f" [{', '.join(mem.get('tags', []))}]" if mem.get('tags') else ''
    return (
        f"  [{mem['id']}] {mem['category_cn']}{tags} {stars}\n"
        f"    {mem['text']}\n"
        f"    创建: {mem['created_at']} | 访问: {mem.get('access_count', 0)}次"
    )


def main():
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    ms = MemoryStore()

    if cmd == 'store':
        text = sys.argv[2] if len(sys.argv) > 2 else ''
        if not text:
            print("用法: python memory.py store \"记忆内容\" [--category xxx] [--importance 0.8] [--tags a,b]")
            return
        category = 'other'
        importance = 0.7
        tags = []
        for i in range(3, len(sys.argv)):
            if sys.argv[i] == '--category' and i + 1 < len(sys.argv):
                category = sys.argv[i + 1]
            elif sys.argv[i] == '--importance' and i + 1 < len(sys.argv):
                importance = float(sys.argv[i + 1])
            elif sys.argv[i] == '--tags' and i + 1 < len(sys.argv):
                tags = sys.argv[i + 1].split(',')
        mid = ms.store(text, category=category, importance=importance, tags=tags)
        print(f"✅ 已存储: {mid}")
        print(f"   分类: {CATEGORY_CN.get(category, category)} | 重要性: {importance}")

    elif cmd == 'recall':
        query = sys.argv[2] if len(sys.argv) > 2 else ''
        limit = 5
        for i in range(3, len(sys.argv)):
            if sys.argv[i] == '--limit' and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        results = ms.recall(query, limit=limit)
        if results:
            print(f"🔍 找到 {len(results)} 条匹配记忆:\n")
            for mem in results:
                print(_format_mem(mem))
                print()
        else:
            print(f"❌ 未找到匹配 \"{query}\" 的记忆")

    elif cmd == 'list':
        limit = 20
        category = None
        for i in range(2, len(sys.argv)):
            if sys.argv[i] == '--limit' and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
            elif sys.argv[i] == '--category' and i + 1 < len(sys.argv):
                category = sys.argv[i + 1]
        results = ms.list(limit=limit, category=category)
        print(f"📋 共 {len(results)} 条记忆:\n")
        for mem in results:
            print(_format_mem(mem))
            print()

    elif cmd == 'stats':
        s = ms.stats()
        print(f"📊 记忆库统计:")
        print(f"  总数: {s['total']}")
        print(f"  平均重要性: {s['avg_importance']}")
        print(f"  分类分布:")
        for cat, cnt in s['by_category'].items():
            print(f"    {cat}: {cnt}")
        print(f"  最后更新: {s['last_updated']}")

    elif cmd == 'forget':
        query = sys.argv[2] if len(sys.argv) > 2 else ''
        deleted = ms.forget(query=query)
        print(f"🗑️ 删除 {deleted} 条记忆")

    elif cmd == 'init':
        """初始化: 导入 LEARNINGS.md 中的关键经验"""
        learnings_file = os.path.join(_WORKSPACE, 'LEARNINGS.md')
        if not os.path.exists(learnings_file):
            print("❌ LEARNINGS.md 不存在")
            return

        with open(learnings_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 解析 ### 标题块 (匹配 ERR-20260727-001 / DEC-001 / LRN-20260725-001 等)
        pattern = r'### (\w+-\d+(?:-\d+)?): (.+?)\n((?:- \*\*.+?\*\*.+\n)+)'
        imported = 0
        for match in re.finditer(pattern, content):
            eid = match.group(1)
            title = match.group(2)
            body = match.group(3)

            # 判断类型
            if eid.startswith('ERR'):
                category = 'fact'
                importance = 0.9
            elif eid.startswith('DEC'):
                category = 'decision'
                importance = 0.95
            elif eid.startswith('LRN'):
                category = 'reflection'
                importance = 0.8
            else:
                category = 'other'
                importance = 0.7

            text = f"[{eid}] {title}\n{body.strip()}"
            ms.store(text, category=category, importance=importance, tags=[eid])
            imported += 1

        print(f"✅ 从 LEARNINGS.md 导入 {imported} 条记忆")

    elif cmd == 'auto-recall':
        """自动召回: 根据用户输入自动匹配相关记忆和铁律"""
        text = sys.argv[2] if len(sys.argv) > 2 else ''
        if not text:
            print("用法: python memory.py auto-recall \"预测260729001\"")
            return
        result = ms.auto_recall(text)
        print(f"🧠 自动召回结果:\n")

        if result['critical_rules']:
            print(f"  铁律 ({len(result['critical_rules'])} 条):")
            for rule in result['critical_rules']:
                print(f"    [{rule['id']}] {rule['title']}")
                if 'rule' in rule:
                    print(f"      规则: {rule['rule']}")
            print()

        if result['match_id_validation']:
            print(f"  编号验证 ({len(result['match_id_validation'])} 个):")
            for mid, val in result['match_id_validation'].items():
                status = "OK" if val['valid'] else "ERROR"
                print(f"    [{status}] {mid} -> 日期={val['date']}, 序号={val['sequence']}")
                for w in val['warnings']:
                    print(f"      WARNING: {w}")
            print()

        if result['matched_memories']:
            print(f"  匹配记忆 ({len(result['matched_memories'])} 条):")
            for mem in result['matched_memories']:
                print(_format_mem(mem))
                print()
        else:
            print("  (无匹配记忆)\n")

        if result['warnings']:
            print(f"  警告 ({len(result['warnings'])} 条):")
            for w in result['warnings']:
                print(f"    ! {w}")

    elif cmd == 'rules':
        """加载并显示铁律"""
        rules = ms.load_critical_rules()
        if rules:
            print(f"📋 铁律列表 ({len(rules)} 条):\n")
            for rule in rules:
                print(f"  [{rule['id']}] {rule['title']}")
                for key in ['trigger', 'rule', 'action', 'violation']:
                    if key in rule:
                        cn_key = {'trigger': '触发', 'rule': '规则', 'action': '行动', 'violation': '违反后果'}
                        print(f"    {cn_key[key]}: {rule[key]}")
                print()
        else:
            print("❌ 无铁律文件 (CRITICAL_RULES.md 不存在)")

    elif cmd == 'check':
        """验证体彩编号"""
        mid = sys.argv[2] if len(sys.argv) > 2 else ''
        if not mid:
            print("用法: python memory.py check 260729001")
            return
        result = ms.validate_match_id(mid)
        status = "OK" if result['valid'] else "ERROR"
        print(f"编号: {mid}")
        print(f"状态: [{status}]")
        print(f"日期: {result['date']}")
        print(f"序号: {result['sequence']}")
        if result['warnings']:
            print(f"警告 ({len(result['warnings'])} 条):")
            for w in result['warnings']:
                print(f"  ! {w}")

    elif cmd == 'context':
        """预测前获取完整上下文"""
        match_ids = []
        user_input = ''
        for arg in sys.argv[2:]:
            if arg.isdigit() and len(arg) == 9:
                match_ids.append(arg)
            else:
                user_input += ' ' + arg
        user_input = user_input.strip()
        context = ms.get_prediction_context(match_ids=match_ids, user_input=user_input)
        print(context)

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: store, recall, list, stats, forget, init, auto-recall, rules, check, context")


if __name__ == '__main__':
    main()
