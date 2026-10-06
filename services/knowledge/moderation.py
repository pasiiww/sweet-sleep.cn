"""Message-free, idempotent moderation counts and sensitive-word review queue."""
import re

MAX_EXPANSIONS_PER_WORD = 256
MAX_EXPANSIONS_TOTAL = 2000


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS moderation_recall_events (
        event_hash TEXT NOT NULL, term TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(event_hash, term))''')
    c.execute('CREATE INDEX IF NOT EXISTS moderation_recall_term ON moderation_recall_events(term)')
    c.execute('''CREATE TABLE IF NOT EXISTS moderation_candidates (
        term TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
        hit_count INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        reviewed_at TEXT NOT NULL DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS moderation_candidate_events (
        event_hash TEXT NOT NULL, term TEXT NOT NULL,
        PRIMARY KEY(event_hash, term))''')
    c.execute('CREATE INDEX IF NOT EXISTS moderation_candidates_status ON moderation_candidates(status,last_seen)')


def validate_words(value, *, maximum=100):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'敏感词需为列表，最多{maximum}个')
    result, seen = [], set()
    for word in value:
        if not isinstance(word, str) or len(word.strip()) > 80:
            raise ValueError('每个敏感词最多80个字符')
        word = word.strip()
        if not word:
            continue
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            result.append(word)
    return result


def expand_word_pattern(pattern, *, maximum=MAX_EXPANSIONS_PER_WORD):
    """Expand escaped literal alternatives without invoking a regex engine."""
    parts = ['']
    literal = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == ')':
            literal.append(char)
            i += 1
            continue
        if char == '/':
            literal.append(char)
            i += 1
            continue
        if char == '\\' and i + 1 < len(pattern) and pattern[i + 1] in r'\\()/':
            literal.append(pattern[i + 1])
            i += 2
            continue
        if char != '(':
            literal.append(char)
            i += 1
            continue

        if literal:
            text = ''.join(literal)
            parts = [part + text for part in parts]
            literal.clear()
        options, option, end = [], [], i + 1
        has_separator = False
        nested = False
        while end < len(pattern):
            current = pattern[end]
            if current == '\\' and end + 1 < len(pattern) and pattern[end + 1] in r'\\()/':
                option.append(pattern[end + 1])
                end += 2
                continue
            if current == '(':
                nested = True
                option.append(current)
                end += 1
                continue
            if current == '/':
                has_separator = True
                options.append(''.join(option).strip())
                option.clear()
                end += 1
                continue
            if current == ')':
                options.append(''.join(option).strip())
                break
            option.append(current)
            end += 1
        if end >= len(pattern):
            literal.append('(')
            i += 1
            continue
        if (nested or not has_separator or len(options) < 2
                or options.count('') > 1 or not any(options)):
            literal.extend(pattern[i:end + 1])
            i = end + 1
            continue
        if len(parts) * len(options) > maximum:
            raise ValueError(f'单条敏感词展开后不能超过{maximum}种组合')
        parts = [prefix + option for prefix in parts for option in options]
        i = end + 1

    if literal:
        text = ''.join(literal)
        parts = [part + text for part in parts]
    return [part for part in dict.fromkeys(parts) if part]


def expand_sensitive_words(words, *, maximum_total=MAX_EXPANSIONS_TOTAL):
    """Return ordered literal variants aligned with the configured source rules."""
    expanded = []
    total = 0
    for word in words:
        variants = expand_word_pattern(word)
        total += len(variants)
        if total > maximum_total:
            raise ValueError(f'敏感词展开后总数不能超过{maximum_total}种')
        expanded.append(variants)
    return expanded


def record(c, data, created_at):
    event_hash = data.get('event_hash')
    if not isinstance(event_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', event_hash):
        raise ValueError('命中事件标识无效')
    terms = validate_words(data.get('terms'))
    candidates = validate_words(data.get('candidates', []), maximum=20)
    if not terms:
        raise ValueError('命中事件至少要包含一个词条')

    c.executemany('INSERT OR IGNORE INTO moderation_recall_events VALUES(?,?,?)',
                  [(event_hash, term, created_at) for term in terms])
    queued = 0
    for term in candidates:
        inserted = c.execute('INSERT OR IGNORE INTO moderation_candidate_events VALUES(?,?)',
                             (event_hash, term)).rowcount
        if not inserted:
            continue
        row = c.execute('SELECT status FROM moderation_candidates WHERE term=?', (term,)).fetchone()
        if row and row[0] != 'pending':
            continue
        c.execute('''INSERT INTO moderation_candidates(term,status,hit_count,first_seen,last_seen)
                     VALUES(?,'pending',1,?,?)
                     ON CONFLICT(term) DO UPDATE SET hit_count=hit_count+1,last_seen=excluded.last_seen''',
                  (term, created_at, created_at))
        queued += 1
    return {'recorded': True, 'candidates_queued': queued}


def stats(c):
    total = c.execute('SELECT count(DISTINCT event_hash) FROM moderation_recall_events').fetchone()[0]
    by_word = [dict(row) for row in c.execute('''
        SELECT term AS word, count(DISTINCT event_hash) AS count
        FROM moderation_recall_events GROUP BY term ORDER BY count DESC, term COLLATE NOCASE
    ''')]
    pending = c.execute("SELECT count(*) FROM moderation_candidates WHERE status='pending'").fetchone()[0]
    return {'total': total, 'by_word': by_word, 'pending_candidates': pending}


def pending_candidates(c, limit=100):
    rows = c.execute('''SELECT term,hit_count,first_seen,last_seen FROM moderation_candidates
                        WHERE status='pending' ORDER BY hit_count DESC,last_seen DESC,term COLLATE NOCASE LIMIT ?''',
                     (limit,)).fetchall()
    return {'items': [dict(row) for row in rows]}


def review_candidate(c, term, decision, reviewed_at):
    if not isinstance(term, str) or not term.strip() or len(term.strip()) > 80:
        raise ValueError('候选词格式无效')
    if decision not in ('approve', 'reject'):
        raise ValueError('decision 必须是 approve 或 reject')
    status = 'approved' if decision == 'approve' else 'rejected'
    changed = c.execute('''UPDATE moderation_candidates SET status=?,reviewed_at=?
                           WHERE term=? AND status='pending' ''',
                        (status, reviewed_at, term.strip())).rowcount
    if not changed:
        row = c.execute('SELECT status FROM moderation_candidates WHERE term=?', (term.strip(),)).fetchone()
        if not row:
            raise LookupError('候选词不存在')
        if row[0] == status:
            return {'term': term.strip(), 'status': status, 'changed': False}
        raise ValueError('候选词已审核，不能重复修改')
    return {'term': term.strip(), 'status': status, 'changed': True}
