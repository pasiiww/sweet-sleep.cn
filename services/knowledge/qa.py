"""Curated Q/A records: only Q participates in retrieval; A remains reference data."""

def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS qa_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kb_id TEXT NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
        question TEXT NOT NULL, answer TEXT NOT NULL, revision TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS qa_kb ON qa_entries(kb_id,id);
        CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(question);
        CREATE TRIGGER IF NOT EXISTS qa_delete AFTER DELETE ON qa_entries BEGIN
            DELETE FROM qa_fts WHERE rowid=old.id;
        END;
    ''')

    columns={r[1] for r in c.execute('PRAGMA table_info(qa_entries)')}
    for name,definition in [('publication',"TEXT NOT NULL DEFAULT 'active'"),('confidence','REAL'),('origin',"TEXT NOT NULL DEFAULT 'manual'"),('updated_by',"TEXT NOT NULL DEFAULT 'manual'"),('source_context',"TEXT NOT NULL DEFAULT '{}'"),('superseded_by','INTEGER REFERENCES qa_entries(id) ON DELETE SET NULL')]:
        if name not in columns:c.execute(f'ALTER TABLE qa_entries ADD COLUMN {name} {definition}')


def search(c, kb_id, query, groups, catalog, tokenize, limit):
    rankings = []
    search_groups=[[term] for term in dict.fromkeys(t for group in groups for t in group)] if groups else [None]
    for group in search_groups:
        conditions, args = [], []
        if group:
            expressions = []
            for term in group:
                variants, checks = [], []
                for variant in catalog.expand(term):
                    tokens = list(dict.fromkeys(tokenize(variant)))
                    if not tokens:
                        continue
                    variants.append('(' + ' AND '.join('"' + token + '"' for token in tokens) + ')')
                    checks.append('instr(lower(q.question),lower(?))>0')
                    args.append(variant)
                if not variants:
                    break
                expressions.append('(' + ' OR '.join(variants) + ')')
                conditions.append('(' + ' OR '.join(checks) + ')')
            if len(expressions) != len(group):
                continue
            match = ' AND '.join(expressions)
        else:
            expanded = [variant for name in catalog.referenced(query) for variant in catalog.expand(name)]
            tokens = list(dict.fromkeys(tokenize(query) + [token for variant in expanded for token in tokenize(variant)]))[:512]
            if not tokens:
                continue
            match = ' OR '.join('"' + token + '"' for token in tokens)
        predicate = ' AND ' + ' AND '.join(conditions) if conditions else ''
        rankings.append([(row['id'], -row['score']) for row in c.execute('''
            SELECT q.id,bm25(qa_fts) AS score FROM qa_fts JOIN qa_entries q ON q.id=qa_fts.rowid
            WHERE qa_fts MATCH ? AND q.kb_id=? AND q.superseded_by IS NULL AND q.publication='active' ''' + predicate + ' ORDER BY score,q.id LIMIT ?', [match,kb_id,*args,limit])])
    if not groups:
        return rankings[0] if rankings else []
    scores = {}
    for ranking in rankings:
        for rank,(qa_id,_) in enumerate(ranking,1):
            scores[qa_id] = scores.get(qa_id,0) + 1 + 1/(60+rank)
    return sorted(scores.items(),key=lambda pair:(-pair[1],pair[0]))[:limit]
