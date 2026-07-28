#!/usr/bin/env python3
"""
memory.py — 轻量级文件记忆系统 (零依赖)

替代 memory-lancedb-pro, 使用 JSON 文件存储 + 关键词搜索。
设计目标: 跨会话持久、零配置、即用即走。

用法:
  # 存储记忆
  python memory.py store "sporttery固定奖金更新时丢失" --category error --importance 0.9
  
  # 搜索记忆
  python memory.py recall "固定奖金"
  
  # 列出全部
  python memory.py list
  
  # 统计
  python memory.py stats

  # Python 内调用
  from memory import MemoryStore
  ms = MemoryStore()
  ms.store("某条记忆", category="fact", importance=0.8)
  results = ms.recall("搜索词")
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

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: store, recall, list, stats, forget, init")


if __name__ == '__main__':
    main()
